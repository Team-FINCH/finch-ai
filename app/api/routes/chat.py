"""Ask My Portfolio. API 명세 §4.

유일한 도구 호출 에이전트. 나머지 기능의 파이프라인을 Tool로 재사용한다.
담당 트랙: feat/llm-agent
"""

from __future__ import annotations

import asyncio
import logging
import re
import uuid
from datetime import datetime
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Header, Request
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app import chat_jobs
from app.api.deps import CurrentUser, DbSession, UsageLimit
from app.chat_jobs import is_expired
from app.core.enums import Screen
from app.core.errors import (
    AppError,
    ErrorCode,
    GuardrailBlocked,
    InsufficientData,
    InvalidRequest,
    LLMTimeout,
    ResourceNotFound,
)
from app.core.models import ChatJob, ChatMessage
from app.core.response_log import record
from app.core.schemas import Citation, ContentModel, DataAsOf, Envelope, Section
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


def _validate(body: ChatRequest) -> str:
    """질문 검증·정리. job 생성과 동기 경로가 같은 검사를 받는다."""
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
    if isinstance(get_llm_client(), NullLlmClient):
        # 종목 분석과 같은 이유로 같은 자리에서 실패한다. 빈 답변으로 지어내지 않는다.
        # DB 를 만지기 전에 본다 — 작업 행이 남지 않게.
        raise InsufficientData(
            "지금은 답변을 만들 수 없습니다.", detail={"reason": "llm_key_missing"}
        )
    return question


async def answer_question(
    body: ChatRequest, user_id: str, db: AsyncSession
) -> Envelope[ChatContent]:
    """답 하나를 만들고 이력에 남긴다. 작업 실행기(`app.chat_jobs`)가 부른다."""
    question = _validate(body)
    client = get_llm_client()

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

    conversation_id = body.conversation_id or _new_conversation_id()
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


def _new_conversation_id() -> str:
    return f"conv_{uuid.uuid4().hex[:16]}"


# ── 비동기 작업 (#90) ─────────────────────────────────────────────────────────
#: 동기 `POST /chat` 이 작업 완료를 기다리는 상한. 백엔드 중계 60초 안이어야 한다.
SYNC_WAIT_S = 55


class ChatJobContent(ContentModel):
    job_id: str
    status: Literal["queued", "running", "completed", "failed"]
    conversation_id: str
    created_at: datetime | None = None
    completed_at: datetime | None = None
    result: ChatContent | None = None
    error: dict[str, Any] | None = None


async def _create_job(
    body: ChatRequest,
    user_id: str,
    db: AsyncSession,
    idempotency_key: str | None,
    *,
    status: str = "queued",
) -> ChatJob:
    """작업 행을 만든다. 같은 멱등 키면 기존 행을 돌려준다 (#90 ㄱ)."""
    _validate(body)
    if idempotency_key:
        existing = await db.scalar(
            select(ChatJob).where(
                ChatJob.user_id == user_id, ChatJob.idempotency_key == idempotency_key
            )
        )
        if existing is not None:
            return existing
    job = ChatJob(
        id=f"job_{uuid.uuid4().hex[:16]}",
        user_id=user_id,
        conversation_id=body.conversation_id or _new_conversation_id(),
        idempotency_key=idempotency_key or None,
        question=body.message,
        context=body.context.model_dump(mode="json"),
        status=status,
    )
    db.add(job)
    await db.commit()
    return job


def _job_envelope(job: ChatJob) -> Envelope[ChatJobContent]:
    result = job.result if job.status == "completed" else None
    envelope = Envelope[ChatJobContent](
        content=ChatJobContent(
            job_id=job.id,
            status=job.status,
            conversation_id=job.conversation_id,
            created_at=job.created_at,
            completed_at=job.completed_at,
            result=ChatContent.model_validate(result["content"]) if result else None,
            error=job.error if job.status == "failed" else None,
        )
    )
    if result:
        envelope.citations = [Citation.model_validate(c) for c in result.get("citations", [])]
        envelope.data_as_of = DataAsOf.model_validate(result.get("data_as_of", {}))
        # 저장된 봉투의 request_id 를 그대로 쓴다. Envelope 의 기본값은 조회할 때마다
        # 새 값을 만드는데, POST /ai/feedback 은 이 값으로 원본 응답을 찾고(계약 C14)
        # 응답 로그에는 **답을 만든 쪽의** request_id 만 남는다. 새로 발급하면 피드백이
        # 엉뚱한 응답에 붙거나 404 가 되고, 화면에는 아무 표시도 나지 않는다 (이슈 #102).
        # 동기 경로(POST /ai/chat)는 저장된 봉투를 그대로 돌려줘 이 문제가 없다.
        envelope.request_id = result.get("request_id", envelope.request_id)
    return envelope


@router.post("/jobs", status_code=202)
async def create_chat_job(
    body: ChatRequest,
    user_id: CurrentUser,
    db: DbSession,
    _usage: UsageLimit,
    idempotency_key: Annotated[str | None, Header(alias="X-Idempotency-Key")] = None,
) -> Envelope[ChatJobContent]:
    """답을 만들기 시작하고 바로 202 를 준다. 결과는 `GET /chat/jobs/{job_id}`."""
    job = await _create_job(body, user_id, db, idempotency_key)
    return _job_envelope(job)


@router.get("/jobs/{job_id}")
async def get_chat_job(
    job_id: str, user_id: CurrentUser, db: DbSession
) -> Envelope[ChatJobContent]:
    """상태와 결과. 한도에 걸리지 않는다 — LLM 을 부르지 않는다 (#90 ㄴ).

    남의 것·없는 것·24시간 지난 것은 모두 404 다 (#90 ㄷ·ㅁ).
    """
    job = await db.scalar(select(ChatJob).where(ChatJob.id == job_id, ChatJob.user_id == user_id))
    if job is None or is_expired(job):
        raise ResourceNotFound("작업을 찾을 수 없습니다.", detail={"job_id": job_id})
    return _job_envelope(job)


@router.post("")
async def chat(
    body: ChatRequest, request: Request, user_id: CurrentUser, db: DbSession, _usage: UsageLimit
) -> Envelope[ChatContent]:
    """동기 경로. 프런트 전환까지 유지한다 (#90 ㅂ).

    작업 행을 `running` 으로 만들어 워커가 집지 않게 한 뒤 이 프로세스 안에서 돌린다.
    최대 `SYNC_WAIT_S` 초 기다렸다 답만 돌려주고, 넘기면 504 지만 작업은 취소하지
    않고 끝까지 돌아 이력과 `GET /chat/jobs/{id}` 에 남는다.
    """
    job = await _create_job(body, user_id, db, None, status="running")
    task = asyncio.create_task(chat_jobs.run_job(job, request.app.state.usage_guard))
    done, _ = await asyncio.wait({task}, timeout=SYNC_WAIT_S)
    if not done:
        raise LLMTimeout(
            "응답이 지연되어 중단했습니다. 잠시 후 대화 이력에서 확인해 주세요.",
            detail={"job_id": job.id},
        )
    status, result, error = task.result()
    if status == "completed" and result:
        # 저장된 봉투 그대로 — request_id 가 응답 로그·피드백과 같아야 한다.
        return Envelope[ChatContent].model_validate(result)
    error = error or {}
    raise AppError(
        error.get("message", "답변을 만들지 못했습니다."),
        code=ErrorCode(error.get("code", ErrorCode.LLM_TIMEOUT.value)),
        detail={"job_id": job.id},
    )


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
