"""위키 저장소 CRUD.

숫자는 넣지 않는다. 비중·수익률·주가는 몇 분이면 낡고, 낡은 숫자가 "AI가 이해한 나"
화면에 그대로 떠 있으면 사용자는 나머지 설명까지 믿지 않는다. 계산은 엔진이 그때그때
하고, 여기에는 계산으로 뽑을 수 없는 문장만 남긴다.

읽기는 항상 source를 함께 돌려준다. ai_inferred 항목은 단정투로 쓰면 안 되는데,
호출부가 어투를 고르려면 근거가 필요하기 때문이다. payload에서 source를 빼지 말 것.

트랜잭션 경계는 라우터가 쥔다. 여기서는 flush까지만 하고 commit하지 않는다.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from typing import Any

from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.enums import Confidence, ThesisHorizon, ThesisStatus, WikiSource
from app.core.errors import InvalidRequest
from app.core.models import WikiFact, WikiThesis
from app.core.schemas import now_kst


# ── 사용자 성향 (facts) ────────────────────────────────────────────────
async def list_facts(db: AsyncSession, user_id: str) -> Sequence[WikiFact]:
    """살아 있는 항목만. 삭제는 소프트 삭제라 읽기가 직접 걸러야 한다."""
    stmt = (
        select(WikiFact)
        .where(WikiFact.user_id == user_id, WikiFact.deleted_at.is_(None))
        .order_by(WikiFact.as_of.desc())
    )
    return (await db.scalars(stmt)).all()


async def add_fact(
    db: AsyncSession,
    user_id: str,
    text: str,
    *,
    source: WikiSource,
    confidence: Confidence = Confidence.MEDIUM,
    evidence: dict[str, Any] | None = None,
    editable: bool = True,
) -> WikiFact:
    fact = WikiFact(
        user_id=user_id,
        text=text,
        source=source,
        confidence=confidence,
        evidence=evidence,
        editable=editable,
    )
    db.add(fact)
    await db.flush()
    return fact


async def soft_delete_fact(db: AsyncSession, user_id: str, fact_id: str) -> WikiFact:
    """행을 지우지 않고 deleted_at만 찍는다.

    사용자가 지운 항목을 AI가 다시 추론해 되살리면 지운 의미가 없다. 흔적을 남겨야
    수집기가 같은 문장을 다시 제안하지 않는지 나중에 확인할 수 있다.
    """
    fact = await db.get(WikiFact, _as_uuid(fact_id))
    # 남의 항목과 이미 지운 항목은 없는 것과 똑같이 취급한다. 존재 여부를 흘리지 않는다.
    if fact is None or fact.user_id != user_id or fact.deleted_at is not None:
        raise InvalidRequest("해당 항목을 찾을 수 없습니다.")
    if not fact.editable:
        raise InvalidRequest("사용자가 지울 수 없는 항목입니다.")
    fact.deleted_at = now_kst()
    await db.flush()
    return fact


# ── 투자 논지 (theses) ─────────────────────────────────────────────────
async def list_theses(
    db: AsyncSession,
    user_id: str,
    status: ThesisStatus = ThesisStatus.ACTIVE,
) -> Sequence[WikiThesis]:
    stmt = (
        select(WikiThesis)
        .where(WikiThesis.user_id == user_id, WikiThesis.status == status)
        .order_by(WikiThesis.recorded_at.desc())
    )
    return (await db.scalars(stmt)).all()


async def get_active_thesis(db: AsyncSession, user_id: str, ticker: str) -> WikiThesis | None:
    stmt = select(WikiThesis).where(
        WikiThesis.user_id == user_id,
        WikiThesis.ticker == ticker,
        WikiThesis.status == ThesisStatus.ACTIVE,
    )
    return await db.scalar(stmt)


async def record_thesis(
    db: AsyncSession,
    user_id: str,
    ticker: str,
    text: str,
    *,
    horizon: ThesisHorizon | None = None,
    source: WikiSource = WikiSource.USER_STATED,
    linked_trade_id: str | None = None,
) -> WikiThesis:
    """새 논지를 활성으로 남기고 이전 활성 논지는 닫는다. 실패시키지 않는다.

    UNIQUE(user_id, ticker, status)는 활성 1건 + 닫힌 1건까지만 허용한다. 그래서
    닫기 전에 이전에 닫아둔 논지를 먼저 지운다 — 직전 논지 한 건만 남는 게 이 스키마가
    허용하는 최대 이력이다. 더 긴 이력이 필요해지면 스키마부터 바꿔야 한다.
    """
    await db.execute(
        delete(WikiThesis).where(
            WikiThesis.user_id == user_id,
            WikiThesis.ticker == ticker,
            WikiThesis.status == ThesisStatus.CLOSED,
        ),
        execution_options={"synchronize_session": "fetch"},
    )
    await db.execute(
        update(WikiThesis)
        .where(
            WikiThesis.user_id == user_id,
            WikiThesis.ticker == ticker,
            WikiThesis.status == ThesisStatus.ACTIVE,
        )
        .values(status=ThesisStatus.CLOSED),
        execution_options={"synchronize_session": "fetch"},
    )
    thesis = WikiThesis(
        user_id=user_id,
        ticker=ticker,
        text=text,
        horizon=horizon,
        source=source,
        status=ThesisStatus.ACTIVE,
        linked_trade_id=linked_trade_id,
    )
    db.add(thesis)
    await db.flush()
    return thesis


async def update_active_thesis(
    db: AsyncSession,
    user_id: str,
    ticker: str,
    text: str,
    *,
    horizon: ThesisHorizon | None = None,
    linked_trade_id: str | None = None,
) -> WikiThesis:
    """활성 논지를 제자리에서 고친다.

    오타 수정이 논지를 통째로 아카이브하면 안 되니 record_thesis와 달리 새 행을 만들지
    않는다. 사용자가 직접 고친 문장은 더 이상 추론이 아니므로 source도 함께 내린다.
    """
    thesis = await get_active_thesis(db, user_id, ticker)
    if thesis is None:
        raise InvalidRequest(f"{ticker}에 기록된 활성 논지가 없습니다.")
    thesis.text = text
    thesis.source = WikiSource.USER_STATED
    if horizon is not None:
        thesis.horizon = horizon
    if linked_trade_id is not None:
        thesis.linked_trade_id = linked_trade_id
    await db.flush()
    return thesis


# ── 직렬화 ─────────────────────────────────────────────────────────────
def fact_payload(fact: WikiFact) -> dict[str, Any]:
    return {
        "id": str(fact.id),
        "text": fact.text,
        "source": fact.source,  # 어투 판단용. 빼지 말 것.
        "confidence": fact.confidence,
        "as_of": fact.as_of,
        "evidence": fact.evidence or {},
        "editable": fact.editable,
    }


def thesis_payload(thesis: WikiThesis) -> dict[str, Any]:
    return {
        "id": str(thesis.id),
        "ticker": thesis.ticker,
        "text": thesis.text,
        "horizon": thesis.horizon,
        "source": thesis.source,  # 어투 판단용. 빼지 말 것.
        "status": thesis.status,
        "linked_trade_id": thesis.linked_trade_id,
        "recorded_at": thesis.recorded_at,
    }


def _as_uuid(value: str) -> uuid.UUID:
    try:
        return uuid.UUID(value)
    except (ValueError, AttributeError, TypeError):
        raise InvalidRequest("해당 항목을 찾을 수 없습니다.") from None
