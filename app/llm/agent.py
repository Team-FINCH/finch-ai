"""Ask My Portfolio의 도구 사용 루프.

제품에서 유일하게 모델이 다음 행동을 고르는 지점이다. 나머지 기능은 무엇을
불러올지 서버가 미리 정해 둔 단방향 파이프라인이다.

흐름은 두 단계로 갈린다.

1. **수집** — 모델이 도구를 고르고, 여기서 실행해 결과를 되돌려 준다.
   한 턴에서 부른 도구는 전부 동시에 돌리고 하나의 user 메시지로 묶어 보낸다.
   나눠 보내면 모델이 병렬 호출을 그만두는 쪽으로 학습된다.
2. **서술** — 모아 둔 근거만 들고 `generate_section`을 부른다. 수집 턴의 대화는
   버린다. 수치가 모델의 기억을 타고 본문으로 새는 경로를 없애기 위해서다 —
   숫자는 `ToolContext.values`를 거쳐 자리표시자 치환(§3)으로만 들어간다.

루프는 반드시 막아 둔다. 도구가 도구를 부르는 되먹임은 문맥 창과 사용자의 돈을
같은 속도로 태운다.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from app.core.enums import Screen
from app.core.schemas import Citation, Section, now_kst
from app.llm.client import LlmClient, ToolUse
from app.llm.generate import (
    build_system,
    citations_from_hits,
    documents_block,
    generate_section,
)
from app.llm.guard import Feature
from app.llm.tools import TOOLS, ToolContext, dispatch

log = logging.getLogger("app.llm.agent")

__all__ = [
    "MAX_TOOL_TURNS",
    "MAX_TOOL_CALLS",
    "PROMPT",
    "AnswerOutcome",
    "answer",
]

PROMPT = "ask_my_portfolio"
HistoryMessage = tuple[str, str]

#: 도구를 고르는 왕복 횟수. 3턴이면 "포트폴리오 → 그 종목 검색 → 논지 확인"까지
#: 닿는다. 그 이상은 대개 모델이 같은 자리를 맴도는 것이다.
MAX_TOOL_TURNS = 3

#: 한 대화 전체의 도구 실행 상한. 한 턴에 검색을 스무 번 펼치는 사고를 막는다.
MAX_TOOL_CALLS = 12

#: 화면이 대명사의 대상을 정한다. 종목 화면의 "이거"는 그 종목이다.
_SCREEN_HINT: dict[Screen, str] = {
    Screen.HOME: "홈 화면",
    Screen.PORTFOLIO: "포트폴리오 화면",
    Screen.BRIEFING: "브리핑 화면",
    Screen.STOCK_DETAIL: "종목 상세 화면",
    Screen.NEWS_DETAIL: "뉴스 상세 화면",
    Screen.ORDER: "주문 화면",
    Screen.CHAT: "대화 화면",
}


@dataclass(frozen=True, slots=True)
class AnswerOutcome:
    """답변 한 건. 실패해도 예외를 던지지 않는다 — 사유를 들고 온다."""

    section: Section | None = None
    reasons: tuple[str, ...] = ()
    tools_used: tuple[str, ...] = ()
    citations: tuple[Citation, ...] = ()
    blocked: bool = False
    turns: int = 0


def _opening(question: str, ctx: ToolContext, now: datetime) -> str:
    """수집 턴의 첫 메시지. 대명사를 풀 수 있는 정보를 먼저 준다."""
    parts = [f"[기준 시각]\n{now.isoformat()}"]
    where = _SCREEN_HINT.get(ctx.screen, "대화 화면")
    if ctx.ticker:
        parts.append(
            f"[사용자가 보고 있는 곳]\n{where} · 종목 {ctx.ticker}\n"
            f'"이거", "이 종목", "얘"는 {ctx.ticker}를 가리킵니다.'
        )
    else:
        parts.append(f"[사용자가 보고 있는 곳]\n{where}")
    parts.append(f"[질문]\n{question}")
    return "\n\n".join(parts)


def _history_block(history: Sequence[HistoryMessage]) -> str:
    if not history:
        return ""
    labels = {"user": "사용자", "assistant": "FINCH"}
    lines = [f"{labels.get(role, role)}: {content}" for role, content in history]
    return "[이전 대화 — 지시 대상과 의도 파악용]\n" + "\n".join(lines)


async def _run_tools(
    uses: tuple[ToolUse, ...], ctx: ToolContext, *, budget: int
) -> list[dict[str, Any]]:
    """한 턴이 부른 도구를 동시에 돌리고 tool_result 블록으로 묶는다."""
    allowed, refused = uses[:budget], uses[budget:]
    results = await asyncio.gather(*(dispatch(u.name, u.input, ctx) for u in allowed))

    blocks = [
        {"type": "tool_result", "tool_use_id": use.id, "content": _as_text(result)}
        for use, result in zip(allowed, results, strict=True)
    ]
    for use in refused:
        log.warning("도구 호출 상한 초과 · %s", use.name)
        blocks.append(
            {
                "type": "tool_result",
                "tool_use_id": use.id,
                "content": "도구 호출 한도를 넘었습니다. 지금까지 받은 자료로 답하십시오.",
                "is_error": True,
            }
        )
    return blocks


def _as_text(result: dict[str, Any]) -> str:
    return json.dumps(result, ensure_ascii=False, default=str)


async def answer(
    question: str,
    *,
    client: LlmClient,
    ctx: ToolContext,
    history: Sequence[HistoryMessage] = (),
    now: datetime | None = None,
) -> AnswerOutcome:
    """질문 하나에 답한다. 도구를 고르고, 모아서, 서술한다."""
    stamp = now or now_kst()
    system = build_system(PROMPT)
    messages: list[dict[str, Any]] = [
        {"role": role, "content": content} for role, content in history
    ]
    messages.append({"role": "user", "content": _opening(question, ctx, stamp)})

    turns = 0
    for turns in range(1, MAX_TOOL_TURNS + 1):  # noqa: B007 — 마지막 값을 결과에 싣는다
        turn = await client.converse(system=system, messages=messages, tools=TOOLS)
        if turn.stop_reason != "tool_use" or not turn.tool_uses:
            break
        budget = max(0, MAX_TOOL_CALLS - len(ctx.used))
        messages.append({"role": "assistant", "content": turn.content})
        messages.append(
            {"role": "user", "content": await _run_tools(turn.tool_uses, ctx, budget=budget)}
        )
    else:
        log.warning("도구 턴 상한 %d회 도달 · 모은 자료로 답한다", MAX_TOOL_TURNS)

    # 모델이 수익률 분해만 선택해도 "왜"라는 질문에는 사건 근거가 필요하다.
    # 분해 결과에서 영향이 큰 종목을 골라 뉴스 검색을 보강한다. 검색 결과가 없으면
    # 기존 규약대로 확인하지 못했다고 답하되, 숫자를 원인처럼 단정하지 않는다.
    if _needs_causal_news(question, ctx):
        for ticker, name in ctx.attribution_news_targets:
            await dispatch(
                "search_news",
                {"query": f"{name} 최근 주가 등락 원인 실적 산업 뉴스", "ticker": ticker},
                ctx,
            )

    citations = citations_from_hits(ctx.hits)
    history_text = _history_block(history)
    request_parts = ["사용자의 질문에 완결된 답을 제시하십시오."]
    if history_text:
        request_parts.append(history_text)
    request_parts.append(f"[현재 질문]\n{question}")
    request_parts.append(
        "확인하겠다거나 데이터를 가져오겠다는 작업 계획으로 끝내지 마십시오. "
        "이미 실행된 도구 결과로 답하고, 결과가 없으면 확인하지 못했다고 명시하십시오."
    )
    # 자리표시자 key 가 `return_000660` 처럼 종목코드로 끝난다. 이름표가 없으면
    # 모델이 코드를 종목명으로 오인해 "000660: +3.4%" 라고 쓴다 — 사용자는 그것이
    # 무슨 종목인지 모른다.
    if ctx.names:
        rows = "\n".join(f"{code} = {name}" for code, name in sorted(ctx.names.items()))
        request_parts.append(
            f"[종목코드와 이름]\n{rows}\n\n본문에는 코드가 아니라 종목명을 씁니다."
        )
    outcome = await generate_section(
        "answer",
        client=client,
        request="\n\n".join(request_parts),
        feature=Feature.ASK_MY_PORTFOLIO,
        prompt=PROMPT,
        engine_values=ctx.values,
        citations=citations,
        documents=documents_block(ctx.hits, citations),
        wiki=ctx.wiki,
        wiki_source=ctx.wiki_source,
        now=stamp,
    )

    return AnswerOutcome(
        section=outcome.section,
        reasons=outcome.reasons,
        tools_used=tuple(dict.fromkeys(ctx.used)),
        citations=tuple(citations),
        blocked=outcome.blocked,
        turns=turns,
    )


_CAUSAL_QUESTION = re.compile(r"왜|원인|이유|때문|무슨\s*일")


def _needs_causal_news(question: str, ctx: ToolContext) -> bool:
    """수익률 분해를 실제로 쓴 인과 질문만 뉴스 자동 보강 대상으로 삼는다."""
    return (
        "calc_attribution" in ctx.used
        and "search_news" not in ctx.used
        and bool(ctx.attribution_news_targets)
        and _CAUSAL_QUESTION.search(question) is not None
    )
