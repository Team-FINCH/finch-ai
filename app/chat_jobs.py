"""채팅 비동기 작업 실행기 (#90).

큐는 `chat_jobs` 테이블이다. 파드마다 워커 하나가 `queued` 행을
`FOR UPDATE SKIP LOCKED` 로 하나씩 집어 실행하므로 파드가 여럿이어도 같은 작업을
두 번 잡지 않고, 서버가 죽어도 행은 남는다. 기동 때 `running` 으로 남은 행은
직전 파드가 잡고 죽은 것이라 `queued` 로 되돌린다.

Redis 나 별도 큐를 두지 않는다. 채팅은 분당 20건 한도가 있어 대기열이 길어질
일이 없고, 테이블 하나가 "재배포해도 안 사라진다" 는 요구를 그대로 만족한다.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.db import SessionFactory
from app.core.errors import AppError, ErrorCode
from app.core.models import ChatJob
from app.core.schemas import now_kst
from app.core.usage_limits import UsageGuard, reset_usage

log = logging.getLogger("app.chat_jobs")

#: 다시 눌러 볼 가치가 있는 실패 (#90 ㄴ). 나머지는 재시도해도 같은 답이다.
RETRYABLE = frozenset({ErrorCode.LLM_TIMEOUT, ErrorCode.RETRIEVAL_FAILED})
#: 보존 기간. 지나면 조회가 404 다 (#90 ㄷ).
RETENTION = timedelta(hours=24)
_POLL_IDLE_S = 1.0


async def requeue_stale_running(db: AsyncSession) -> int:
    """기동 시 직전 파드가 잡고 죽은 작업을 되돌린다."""
    result = await db.execute(
        update(ChatJob).where(ChatJob.status == "running").values(status="queued", started_at=None)
    )
    await db.commit()
    return result.rowcount or 0


async def claim_next(db: AsyncSession) -> ChatJob | None:
    """queued 하나를 running 으로 바꿔 돌려준다. 없으면 None."""
    row = await db.scalar(
        select(ChatJob)
        .where(ChatJob.status == "queued")
        .order_by(ChatJob.created_at)
        .limit(1)
        .with_for_update(skip_locked=True)
    )
    if row is None:
        return None
    row.status = "running"
    row.started_at = now_kst()
    await db.commit()
    return row


Outcome = tuple[str, dict[str, Any] | None, dict[str, Any] | None]


async def run_job(job: ChatJob, guard: UsageGuard) -> Outcome:
    """작업 하나를 끝까지 돌린다. 결과든 오류든 행에 남기고 예외를 올리지 않는다.

    (status, result, error) 를 돌려줘 동기 경로가 행을 다시 읽지 않아도 되게 한다.
    """
    # 지연 임포트 — 라우트 모듈이 이 모듈을 임포트한다.
    from app.api.routes.chat import ChatRequest, answer_question

    token = await guard.enter_system(
        job.user_id, "chat", now=now_kst(), budget=settings.ai_daily_token_budget
    )
    try:
        async with SessionFactory() as db:
            body = ChatRequest(
                conversation_id=job.conversation_id, message=job.question, context=job.context
            )
            envelope = await answer_question(body, job.user_id, db)
            payload: dict[str, Any] = envelope.model_dump(mode="json")
        await _finish(job.id, status="completed", result=payload)
        return "completed", payload, None
    except AppError as exc:
        error = {
            "code": exc.code.value,
            "message": exc.message,
            "retryable": exc.code in RETRYABLE,
        }
    except Exception:
        log.exception("채팅 작업 %s 실패", job.id)
        error = {
            "code": ErrorCode.LLM_TIMEOUT.value,
            "message": "답변 생성 중 오류가 났습니다. 다시 시도해 주세요.",
            "retryable": True,
        }
    finally:
        reset_usage(token)
    await _finish(job.id, status="failed", error=error)
    return "failed", None, error


async def _finish(job_id: str, **values: Any) -> None:
    async with SessionFactory() as db:
        await db.execute(
            update(ChatJob).where(ChatJob.id == job_id).values(completed_at=now_kst(), **values)
        )
        await db.commit()


async def worker(guard: UsageGuard, *, stop: asyncio.Event) -> None:
    """파드당 하나. queued 가 없으면 1초 쉰다."""
    try:
        async with SessionFactory() as db:
            restored = await requeue_stale_running(db)
        if restored:
            log.warning("running 으로 남은 채팅 작업 %d건을 queued 로 되돌렸다", restored)
    except Exception:
        log.exception("채팅 작업 복구 실패 — 다음 순회에서 이어 간다")
    while not stop.is_set():
        try:
            async with SessionFactory() as db:
                job = await claim_next(db)
            if job is None:
                await asyncio.sleep(_POLL_IDLE_S)
                continue
            await run_job(job, guard)
        except asyncio.CancelledError:
            raise
        except Exception:
            # 큐 조회 자체가 실패해도 워커는 죽지 않는다. DB 가 돌아오면 이어 간다.
            log.exception("채팅 작업 워커 오류")
            await asyncio.sleep(_POLL_IDLE_S)


def is_expired(job: ChatJob, now: datetime | None = None) -> bool:
    created = job.created_at
    if created is None:
        return False
    at = now or now_kst()
    if created.tzinfo is None:
        created = created.replace(tzinfo=at.tzinfo)
    return created < at - RETENTION
