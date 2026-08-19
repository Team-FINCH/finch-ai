"""응답 로그.

엔드포인트가 돌려준 봉투를 `ai_responses`에 남긴다. 두 가지가 이 테이블에
걸려 있다.

    피드백    `ai_feedback.request_id`가 이 테이블을 외래키로 참조한다.
              행이 없으면 피드백을 받을 수 없다 — §10.
    히스테리시스  진단은 직전 위험 등급이 있어야 1점 차이로 등급이 뒤집히는 것을
              막는다(§3.6). 그 직전 값이 여기 남는다.

기록이 실패해도 응답은 나가야 한다. 로그를 남기지 못한 것과 답을 주지 못한 것은
사용자에게 전혀 다른 사건이다.
"""

from __future__ import annotations

import logging
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.enums import RiskLevel
from app.core.models import AIResponse
from app.core.schemas import Envelope

log = logging.getLogger("app.core.response_log")


async def record(
    db: AsyncSession,
    envelope: Envelope[Any],
    *,
    user_id: str,
    endpoint: str,
    prompt_version: str | None = None,
    effort: str | None = None,
    input_tokens: int = 0,
    output_tokens: int = 0,
    cache_read_tokens: int = 0,
    latency_ms: int | None = None,
    guardrail_result: dict[str, Any] | None = None,
) -> None:
    """봉투 하나를 기록한다. 실패해도 예외를 올리지 않는다.

    커밋까지 여기서 한다. 호출부가 이미 응답을 만들어 놓은 뒤라, 이 트랜잭션이
    호출부의 세션 상태를 물고 늘어지면 기록 실패가 응답 실패로 번진다.
    """
    row = AIResponse(
        request_id=envelope.request_id,
        user_id=user_id,
        endpoint=endpoint,
        prompt_version=prompt_version,
        model=envelope.model,
        effort=effort,
        cached=envelope.cached,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cache_read_tokens=cache_read_tokens,
        latency_ms=latency_ms,
        guardrail_result=guardrail_result,
        payload=envelope.model_dump(mode="json"),
    )
    try:
        db.add(row)
        await db.commit()
    except Exception:
        # 응답은 이미 만들어졌다. 로그를 못 남긴 것이 답을 못 준 것이 되면 안 된다.
        log.exception("응답 로그 기록 실패 · request_id=%s", envelope.request_id)
        await db.rollback()


async def last_risk_level(db: AsyncSession, user_id: str) -> RiskLevel | None:
    """직전 진단의 위험 등급. 없으면 None.

    §3.6의 히스테리시스에 쓴다. 등급은 payload 안에 있으므로 컬럼으로 꺼내
    두지 않고 여기서 읽는다 — 진단 말고는 쓸 데가 없는 값이라 스키마를 넓히는
    대신 조회를 좁혔다.
    """
    stmt = (
        select(AIResponse.payload)
        .where(
            AIResponse.user_id == user_id,
            AIResponse.endpoint == "portfolio.diagnosis",
        )
        .order_by(AIResponse.created_at.desc())
        .limit(1)
    )
    payload = await db.scalar(stmt)
    if not isinstance(payload, dict):
        return None

    content = payload.get("content")
    raw = content.get("risk_level") if isinstance(content, dict) else None
    if not raw:
        return None
    try:
        return RiskLevel(raw)
    except ValueError:
        log.warning("알 수 없는 위험 등급이 기록돼 있다: %r", raw)
        return None
