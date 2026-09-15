"""Ask My Portfolio. API 명세 §4.

유일한 도구 호출 에이전트. 나머지 기능의 파이프라인을 Tool로 재사용한다.
담당 트랙: feat/llm-agent
"""

from __future__ import annotations

import logging
import re
import uuid
from datetime import datetime
from typing import Literal

from fastapi import APIRouter
from pydantic import BaseModel
from sqlalchemy import select

from app.api.deps import CurrentUser, DbSession, UsageLimit
from app.core.enums import Screen
from app.core.errors import GuardrailBlocked, InsufficientData, InvalidRequest
from app.core.models import ChatMessage
from app.core.response_log import record
from app.core.schemas import ContentModel, DataAsOf, Envelope, Section
from app.llm.agent import answer
from app.llm.client import NullLlmClient, get_llm_client
from app.llm.guard.input import injection_hit, sanitize
from app.llm.tools import ToolContext

log = logging.getLogger("app.api.chat")

router = APIRouter(prefix="/chat", tags=["chat"])

_TICKER_RE = re.compile(r"\d{6}")

#: 질문 길이 상한. 이보다 긴 것은 대화가 아니라 문서 붙여넣기다.
_MAX_MESSAGE = 2_000
_HISTORY_LIMIT = 12


class ChatContext(BaseModel):
    screen: Screen = Screen.CHAT
    ticker: str | None = None


class ChatRequest(BaseModel):
    conversation_id: str | None = None
    message: str
    context: ChatContext = ChatContext()


class ChatContent(ContentModel):
    """§4 답변 본문."""

    conversation_id: str
    answer: Section
    tools_used: list[str]


class ChatMessageContent(ContentModel):
    role: Literal["user", "assistant"]
    content: str
    created_at: datetime


class ChatHistoryContent(ContentModel):
    conversation_id: str
    messages: list[ChatMessageContent]


async def _conversation_history(
    db: DbSession, user_id: str, conversation_id: str | None
) -> tuple[tuple[str, str], ...]:
    """현재 사용자의 최근 대화를 오래된 순서로 돌려준다."""
    if conversation_id is None:
        return ()
    rows = (
        await db.scalars(
            select(ChatMessage)
            .where(
                ChatMessage.user_id == user_id,
                ChatMessage.conversation_id == conversation_id,
            )
            .order_by(ChatMessage.id.desc())
            .limit(_HISTORY_LIMIT)
        )
    ).all()
    return tuple((row.role, row.content) for row in reversed(rows))


@router.post("")
async def chat(
    body: ChatRequest, user_id: CurrentUser, db: DbSession, _usage: UsageLimit
) -> Envelope[ChatContent]:
    question = body.message.strip()
    if not question:
        raise InvalidRequest("질문이 비어 있습니다.")
    if len(question) > _MAX_MESSAGE:
        raise InvalidRequest("질문이 너무 깁니다.", detail={"max_length": _MAX_MESSAGE})
    # 입력단 가드 — 제어문자 정리 후 인젝션 휴리스틱을 본다(guard/input).
    question = sanitize(question)
    if injection_hit(question):
        raise GuardrailBlocked(
            "질문에 사용할 수 없는 내용이 포함되어 있습니다.",
            detail={"reason": "prompt_injection"},
        )
    if body.context.ticker and not _TICKER_RE.fullmatch(body.context.ticker):
        raise InvalidRequest("종목코드는 6자리 숫자입니다.", detail={"ticker": body.context.ticker})

    client = get_llm_client()
    if isinstance(client, NullLlmClient):
        # 종목 분석과 같은 이유로 같은 자리에서 실패한다. 빈 답변으로 지어내지 않는다.
        raise InsufficientData(
            "지금은 답변을 만들 수 없습니다.", detail={"reason": "llm_key_missing"}
        )

    ctx = ToolContext(
        user_id=user_id,
        db=db,
        screen=body.context.screen,
        ticker=body.context.ticker,
    )
    history = await _conversation_history(db, user_id, body.conversation_id)
    outcome = await answer(question, client=client, ctx=ctx, history=history)

    if outcome.section is None:
        # §7 — 검사에 걸린 답변은 내보내지 않는다. 사유는 로그에만 남긴다.
        log.warning("답변 차단 · %s", "; ".join(outcome.reasons))
        raise GuardrailBlocked("답변을 생성하지 못했습니다. 질문을 조금 더 구체적으로 적어 주세요.")

    conversation_id = body.conversation_id or f"conv_{uuid.uuid4().hex[:16]}"
    envelope = Envelope[ChatContent](
        content=ChatContent(
            conversation_id=conversation_id,
            answer=outcome.section,
            tools_used=list(outcome.tools_used),
        ),
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
    db.add(
        ChatMessage(
            conversation_id=conversation_id,
            user_id=user_id,
            role="user",
            content=question,
        )
    )
    db.add(
        ChatMessage(
            conversation_id=conversation_id,
            user_id=user_id,
            role="assistant",
            content=outcome.section.text,
        )
    )
    await db.commit()
    await record(db, envelope, user_id=user_id, endpoint="chat")
    return envelope


@router.get("/conversations/{conversation_id}/messages")
async def conversation_messages(
    conversation_id: str, user_id: CurrentUser, db: DbSession
) -> Envelope[ChatHistoryContent]:
    """현재 사용자가 소유한 한 대화의 메시지를 입력 순서대로 돌려준다."""
    rows = (
        await db.scalars(
            select(ChatMessage)
            .where(
                ChatMessage.user_id == user_id,
                ChatMessage.conversation_id == conversation_id,
            )
            .order_by(ChatMessage.id)
        )
    ).all()
    return Envelope[ChatHistoryContent](
        content=ChatHistoryContent(
            conversation_id=conversation_id,
            messages=[
                ChatMessageContent(
                    role=row.role,
                    content=row.content,
                    created_at=row.created_at,
                )
                for row in rows
            ],
        )
    )
