"""Ask My Portfolio. API 명세 §4.

유일한 도구 호출 에이전트. 나머지 기능의 파이프라인을 Tool로 재사용한다.
담당 트랙: feat/llm-agent
"""

from __future__ import annotations

import logging
import re
import uuid
from datetime import datetime

from fastapi import APIRouter
from pydantic import BaseModel

from app.api.deps import CurrentUser, DbSession
from app.core.enums import Screen
from app.core.errors import GuardrailBlocked, InsufficientData, InvalidRequest
from app.core.response_log import record
from app.core.schemas import DataAsOf, Envelope
from app.llm.agent import answer
from app.llm.client import NullLlmClient, get_llm_client
from app.llm.tools import ToolContext

log = logging.getLogger("app.api.chat")

router = APIRouter(prefix="/chat", tags=["chat"])

_TICKER_RE = re.compile(r"\d{6}")

#: 질문 길이 상한. 이보다 긴 것은 대화가 아니라 문서 붙여넣기다.
_MAX_MESSAGE = 2_000


class ChatContext(BaseModel):
    screen: Screen = Screen.CHAT
    ticker: str | None = None


class ChatRequest(BaseModel):
    conversation_id: str | None = None
    message: str
    context: ChatContext = ChatContext()


@router.post("")
async def chat(body: ChatRequest, user_id: CurrentUser, db: DbSession) -> Envelope[dict]:
    question = body.message.strip()
    if not question:
        raise InvalidRequest("질문이 비어 있습니다.")
    if len(question) > _MAX_MESSAGE:
        raise InvalidRequest(
            "질문이 너무 깁니다.", detail={"max_length": _MAX_MESSAGE}
        )
    if body.context.ticker and not _TICKER_RE.fullmatch(body.context.ticker):
        raise InvalidRequest(
            "종목코드는 6자리 숫자입니다.", detail={"ticker": body.context.ticker}
        )

    client = get_llm_client()
    if isinstance(client, NullLlmClient):
        # 종목 분석과 같은 이유로 같은 자리에서 실패한다. 빈 답변으로 지어내지 않는다.
        raise InsufficientData("LLM 키가 없어 답변을 생성할 수 없습니다.")

    ctx = ToolContext(
        user_id=user_id,
        db=db,
        screen=body.context.screen,
        ticker=body.context.ticker,
    )
    outcome = await answer(question, client=client, ctx=ctx)

    if outcome.section is None:
        # §7 — 검사에 걸린 답변은 내보내지 않는다. 사유는 로그에만 남긴다.
        log.warning("답변 차단 · %s", "; ".join(outcome.reasons))
        raise GuardrailBlocked("답변을 생성하지 못했습니다. 질문을 조금 더 구체적으로 적어 주세요.")

    envelope = Envelope[dict](
        content={
            "conversation_id": body.conversation_id or f"conv_{uuid.uuid4().hex[:16]}",
            "answer": outcome.section.model_dump(mode="json"),
            "tools_used": list(outcome.tools_used),
        },
        citations=list(outcome.citations),
        data_as_of=DataAsOf(
            portfolio=ctx.portfolio_as_of,
            filings=max(
                (
                    hit["published_at"]
                    for hit in ctx.hits
                    if isinstance(hit.get("published_at"), datetime)
                ),
                default=None,
            ),
        ),
    )
    await record(db, envelope, user_id=user_id, endpoint="chat")
    return envelope
