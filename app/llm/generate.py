"""섹션 생성 파이프라인 — 조립 → 호출 → 치환 → 검사.

Stock Analyst와 Ask My Portfolio가 함께 쓴다. 기능마다 다른 것은 L2 프롬프트
파일과 넘겨받는 엔진 값·근거뿐이고, 나머지 절차는 전부 같다.

세 가지 규칙이 이 모듈의 형태를 정한다.

- **프롬프트는 데이터다**(§7.1). 문자열 리터럴이 아니라 `prompts/*.md`에서 읽는다.
- **고정 구간이 앞에 온다**(§1.2). L1+L2만 캐시 표시를 달고, 기준 시각·질문은
  뒤쪽 사용자 턴으로 내린다. 접두가 1바이트만 바뀌어도 캐시가 통째로 깨진다.
- **수치는 LLM이 쓰지 않는다**(§3). 모델은 `{{key}}`만 내고 서버가 치환한다.
  치환 결과를 그대로 이어 붙여 `Section`을 만들기 때문에 text와 segments가
  어긋날 수 없다.

재생성은 §7이 정한 대로 최대 2회다. 위반 사유를 그대로 프롬프트에 실어야
고쳐진다 — "형식을 지키세요"로는 같은 실수를 반복한다.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from functools import lru_cache
from pathlib import Path
from typing import Any

from app.core.enums import CitationType, Direction, MetricSource, Unit, WikiSource
from app.core.schemas import Citation, Section, Segment, now_kst
from app.llm.client import LlmClient
from app.llm.guard import (
    Disposition,
    Feature,
    GuardContext,
    run_output_guard,
)

log = logging.getLogger("app.llm.generate")

__all__ = [
    "NARRATIVE_SCHEMA",
    "MAX_ATTEMPTS",
    "SectionOutcome",
    "load_prompt",
    "build_system",
    "build_user_turn",
    "segments_from_narrative",
    "citations_from_hits",
    "documents_block",
    "ratio_segment",
    "count_segment",
    "krw_segment",
    "generate_section",
]

PROMPT_DIR = Path(__file__).parent / "prompts"

#: 프롬프트 정책 §3. `used_placeholders`를 함께 받아 검증을 단순화한다.
NARRATIVE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "narrative": {"type": "string"},
        "used_placeholders": {"type": "array", "items": {"type": "string"}},
        "used_citations": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["narrative", "used_placeholders", "used_citations"],
    "additionalProperties": False,
}

#: 최초 1회 + 재생성 2회. 응답 정책 §7.
MAX_ATTEMPTS = 3

_PLACEHOLDER_RE = re.compile(r"\{\{\s*([A-Za-z0-9_.]+)\s*\}\}")


# ── 프롬프트 적재 ────────────────────────────────────────
@lru_cache
def load_prompt(name: str) -> str:
    """`prompts/<name>.md`를 읽는다. 파일이 없으면 그 자리에서 터진다."""
    return (PROMPT_DIR / f"{name}.md").read_text(encoding="utf-8").strip()


def build_system(feature_prompt: str) -> list[dict[str, Any]]:
    """L1 + L2. 마지막 고정 블록에만 캐시 표시를 단다(§1.2).

    현재 시각·사용자 식별자는 절대 여기 넣지 않는다. 캐시는 접두 일치라서
    앞쪽 1바이트가 바뀌면 뒤가 전부 무효화된다.
    """
    return [
        {"type": "text", "text": load_prompt("base_system")},
        {
            "type": "text",
            "text": load_prompt(feature_prompt),
            "cache_control": {"type": "ephemeral"},
        },
    ]


# ── 엔진 값 조각 ─────────────────────────────────────────
def ratio_segment(
    raw: float,
    source: MetricSource,
    *,
    signed: bool = False,
    digits: int = 1,
) -> Segment:
    """비율을 표시 문자열과 원값을 함께 담은 조각으로 만든다.

    표시 문자열이 곧 치환 값이다. 엔진 값 대조(§5.2 5번)가 이 문자열과
    본문을 한 글자 단위로 맞춰 보므로, 포맷을 호출부마다 다르게 쓰면 안 된다.
    """
    sign = "+" if signed and raw > 0 else ""
    direction = None
    if signed:
        direction = Direction.UP if raw > 0 else Direction.DOWN if raw < 0 else None
    return Segment.metric(
        f"{sign}{raw * 100:.{digits}f}%",
        raw=raw,
        source=source,
        unit=Unit.RATIO,
        direction=direction,
    )


def count_segment(raw: float, source: MetricSource, *, digits: int = 1) -> Segment:
    """유효 종목 수처럼 단위가 개수인 값."""
    return Segment.metric(
        f"{raw:.{digits}f}개", raw=raw, source=source, unit=Unit.COUNT
    )


def krw_segment(raw: float, source: MetricSource, *, signed: bool = False) -> Segment:
    """원화 금액. 국내 관례대로 천 단위 쉼표를 넣고 소수점은 버린다."""
    sign = "+" if signed and raw > 0 else ""
    direction = None
    if signed:
        direction = Direction.UP if raw > 0 else Direction.DOWN if raw < 0 else None
    return Segment.metric(
        f"{sign}{raw:,.0f}원",
        raw=raw,
        source=source,
        unit=Unit.KRW,
        direction=direction,
    )


def segments_from_narrative(
    narrative: str, values: Mapping[str, Segment]
) -> list[Segment]:
    """`{{key}}`를 엔진 조각으로 바꾸며 조각 목록을 만든다.

    이어 붙인 결과는 `render_placeholders`가 만든 문자열과 정확히 같다.
    허용 목록에 없는 key는 건드리지 않고 남긴다 — 조용히 지우면 미치환
    검사(§5.2 2번)가 통과해 버린다.
    """
    out: list[Segment] = []
    cursor = 0
    for match in _PLACEHOLDER_RE.finditer(narrative):
        segment = values.get(match.group(1))
        if segment is None:
            continue
        if match.start() > cursor:
            out.append(Segment.text(narrative[cursor : match.start()]))
        out.append(segment)
        cursor = match.end()
    if cursor < len(narrative):
        out.append(Segment.text(narrative[cursor:]))
    return out


# ── 근거·문서 조립 ───────────────────────────────────────
def citations_from_hits(
    hits: Sequence[Mapping[str, Any]],
    *,
    citation_type: CitationType = CitationType.FILING,
    source: str = "DART",
) -> list[Citation]:
    """`app.rag.search.search()` 결과를 근거 목록으로 바꾼다."""
    return [
        Citation(
            id=f"cit_{index}",
            type=citation_type,
            title=hit.get("title") or "제목 없음",
            source=source,
            published_at=hit.get("published_at"),
            snippet=(hit.get("text") or "")[:180],
            relevance=hit.get("similarity"),
        )
        for index, hit in enumerate(hits, start=1)
    ]


def documents_block(
    hits: Sequence[Mapping[str, Any]], citations: Sequence[Citation]
) -> str:
    """L5. 근거 id와 원문 조각을 짝지어 넘긴다."""
    return "\n\n".join(
        f"[^{citation.id}] {citation.title}\n{hit.get('text', '')}"
        for hit, citation in zip(hits, citations, strict=False)
    )


# ── 사용자 턴(L4·L5·L7) ──────────────────────────────────
def build_user_turn(
    key: str,
    *,
    values: Mapping[str, Segment],
    citations: Sequence[Citation] = (),
    documents: str = "",
    wiki: str = "",
    now: datetime | None = None,
    request: str | None = None,
) -> str:
    """매 요청 바뀌는 구간. 자리표시자 목록을 정렬해 순서를 고정한다.

    `request`는 섹션이 아닌 기능 — Ask My Portfolio의 사용자 질문 — 을 위한
    출구다. 넘기지 않으면 지금까지와 똑같이 섹션 요청 문구를 쓴다.
    """
    parts = [f"[요청]\n{request or f'{key} 섹션을 작성하십시오.'}"]
    parts.append(f"[기준 시각]\n{(now or now_kst()).isoformat()}")

    if wiki:
        parts.append(f"[사용자 투자 논지]\n{wiki}")

    if values:
        rows = "\n".join(
            f"{{{{{name}}}}}   {segment.value}" for name, segment in sorted(values.items())
        )
        parts.append(
            "[사용 가능한 수치 자리표시자]\n"
            f"{rows}\n\n"
            "목록에 없는 자리표시자를 쓰면 응답이 폐기됩니다.\n"
            "수치를 직접 쓰지 마십시오. 반올림한 근사값도 허용되지 않습니다."
        )
    else:
        parts.append("[사용 가능한 수치 자리표시자]\n없습니다. 수치를 언급하지 마십시오.")

    if citations:
        rows = "\n".join(
            f"[^{c.id}] {c.title} ({c.source})" for c in citations
        )
        parts.append(f"[사용 가능한 근거]\n{rows}")
    else:
        # §7 — RAG 0건이어도 생성은 진행하되 고정 문구로 한계를 밝힌다.
        parts.append(
            "[사용 가능한 근거]\n"
            "없습니다. 관련 자료를 찾지 못했다고 쓰고, 사실을 추정으로 메우지 마십시오."
        )

    if documents:
        parts.append(f"[참고 자료 원문]\n{documents}")

    return "\n\n".join(parts)


def _regeneration_note(reasons: Sequence[str]) -> str:
    """§7 — 무엇이 걸렸는지 그대로 알려 준다. 일반론으로는 안 고쳐진다."""
    body = "\n".join(f"- {reason}" for reason in reasons)
    return (
        "[직전 응답 검사 결과]\n"
        "아래 항목이 걸려 폐기되었습니다. 같은 실수를 반복하지 마십시오.\n"
        f"{body}"
    )


# ── 본체 ─────────────────────────────────────────────────
@dataclass(frozen=True, slots=True)
class SectionOutcome:
    """섹션 하나의 생성 결과. 실패해도 예외를 던지지 않는다.

    §7 표에 따라 섹션 하나가 실패해도 나머지는 정상 반환해야 하므로,
    실패는 `section=None`으로 표현하고 사유를 함께 들고 온다.
    """

    key: str
    section: Section | None = None
    reasons: tuple[str, ...] = ()
    attempts: int = 0
    blocked: bool = False
    cache_read_tokens: int = 0
    citations: tuple[Citation, ...] = field(default=())


async def generate_section(
    key: str,
    *,
    client: LlmClient,
    title: str | None = None,
    feature: Feature = Feature.STOCK_ANALYST_SECTION,
    prompt: str = "stock_analyst",
    effort: str = "high",
    engine_values: Mapping[str, Segment] | None = None,
    citations: Sequence[Citation] = (),
    documents: str = "",
    wiki: str = "",
    wiki_source: WikiSource | None = None,
    now: datetime | None = None,
    request: str | None = None,
) -> SectionOutcome:
    """섹션 하나를 만든다. 검사를 통과한 것만 `Section`으로 돌아온다."""
    values = dict(engine_values or {})
    display = {name: segment.value for name, segment in values.items()}
    citation_ids = [c.id for c in citations]

    user_turn = build_user_turn(
        key,
        values=values,
        citations=citations,
        documents=documents,
        wiki=wiki,
        now=now,
        request=request,
    )

    reasons: tuple[str, ...] = ()
    cache_read = 0
    feedback = ""

    for attempt in range(1, MAX_ATTEMPTS + 1):
        result = await client.generate(
            system=build_system(prompt),
            user=user_turn if not feedback else f"{user_turn}\n\n{feedback}",
            schema=NARRATIVE_SCHEMA,
            effort=effort,
        )
        cache_read = result.cache_read_tokens

        narrative = result.payload.get("narrative")
        segments = (
            segments_from_narrative(narrative, values)
            if isinstance(narrative, str)
            else []
        )
        report = run_output_guard(
            result.payload,
            GuardContext(
                engine_values=display,
                available_citations=citation_ids,
                feature=feature,
                wiki_source=wiki_source,
                segments=segments,
            ),
        )

        if report.passed:
            return SectionOutcome(
                key=key,
                section=Section.from_segments(segments, title=title),
                attempts=attempt,
                cache_read_tokens=cache_read,
                citations=tuple(citations),
            )

        reasons = report.reasons
        if report.disposition is Disposition.BLOCK:
            # 값이 어긋났다는 것은 파이프라인 고장이다. 다시 만들면 위험만 커진다.
            log.error("섹션 %s 차단 · %s", key, "; ".join(reasons))
            return SectionOutcome(
                key=key,
                reasons=reasons,
                attempts=attempt,
                blocked=True,
                cache_read_tokens=cache_read,
            )

        log.warning("섹션 %s 재생성 %d회차 · %s", key, attempt, "; ".join(reasons))
        feedback = _regeneration_note(reasons)

    return SectionOutcome(
        key=key,
        reasons=reasons,
        attempts=MAX_ATTEMPTS,
        blocked=True,
        cache_read_tokens=cache_read,
    )
