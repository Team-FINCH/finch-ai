"""투자 논지와 사용자 맥락. API 명세 §9.

담당 트랙: feat/wiki-crud
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from app.api.deps import CurrentUser, DbSession
from app.core.adapters import ledger_source
from app.core.enums import Confidence, DeleteReason, ThesisHorizon, ThesisStatus, WikiSource
from app.core.schemas import ContentModel, Envelope
from app.wiki.store import (
    fact_payload,
    list_facts,
    list_theses,
    record_thesis,
    soft_delete_fact,
    thesis_payload,
    update_active_thesis,
)

router = APIRouter(prefix="/wiki", tags=["wiki"])


class ThesisIn(BaseModel):
    ticker: str = Field(min_length=6, max_length=6)
    text: str = Field(min_length=1, max_length=500)
    horizon: ThesisHorizon | None = None
    linked_trade_id: str | None = None


class WikiFactOut(ContentModel):
    id: str
    text: str
    source: WikiSource
    confidence: Confidence
    as_of: datetime
    evidence: dict[str, Any]
    editable: bool


class WikiThesisOut(ContentModel):
    id: str
    ticker: str
    name: str
    text: str
    horizon: ThesisHorizon | None = None
    source: WikiSource
    status: ThesisStatus
    linked_trade_id: str | None = None
    recorded_at: datetime


class WikiContent(ContentModel):
    profile: list[WikiFactOut]
    theses: list[WikiThesisOut]


class DeletedFactContent(ContentModel):
    id: str
    deleted_at: datetime
    reason: DeleteReason


@router.get("")
async def get_wiki(user_id: CurrentUser, db: DbSession) -> Envelope[WikiContent]:
    """ "AI가 이해한 나" 화면 한 벌.

    항목마다 source를 그대로 실어 보낸다. ai_inferred는 단정투로 렌더링하면 안 되고,
    그 판단은 화면이 한다.
    """
    theses = await list_theses(db, user_id)
    names = await _thesis_names(user_id, [thesis.ticker for thesis in theses])
    return Envelope[WikiContent](
        content={
            "profile": [fact_payload(f) for f in await list_facts(db, user_id)],
            "theses": [thesis_payload(thesis, name=names.get(thesis.ticker)) for thesis in theses],
        }
    )


@router.post("/theses")
async def create_thesis(
    body: ThesisIn, user_id: CurrentUser, db: DbSession
) -> Envelope[WikiThesisOut]:
    """새 논지를 남긴다. 같은 종목의 이전 논지는 실패가 아니라 종료 처리된다."""
    thesis = await record_thesis(
        db,
        user_id,
        body.ticker,
        body.text,
        horizon=body.horizon,
        linked_trade_id=body.linked_trade_id,
    )
    await db.commit()
    names = await _thesis_names(user_id, [thesis.ticker])
    return Envelope[WikiThesisOut](content=thesis_payload(thesis, name=names.get(thesis.ticker)))


@router.put("/theses/{ticker}")
async def update_thesis(
    ticker: str, body: ThesisIn, user_id: CurrentUser, db: DbSession
) -> Envelope[WikiThesisOut]:
    """활성 논지 수정. 경로의 ticker가 기준이다(본문 값은 무시한다)."""
    thesis = await update_active_thesis(
        db,
        user_id,
        ticker,
        body.text,
        horizon=body.horizon,
        linked_trade_id=body.linked_trade_id,
    )
    await db.commit()
    names = await _thesis_names(user_id, [thesis.ticker])
    return Envelope[WikiThesisOut](content=thesis_payload(thesis, name=names.get(thesis.ticker)))


async def _thesis_names(user_id: str, tickers: list[str]) -> dict[str, str]:
    """원장에서 논지 종목의 표시명을 찾는다.

    논지는 과거에 보유했던 종목에도 남는다. 원장에 없는 종목은 ticker를 그대로
    표시해 논지 조회 자체가 원장 상태에 막히지 않게 한다.
    """
    source = ledger_source()
    if source is None:
        return {}
    try:
        ledger = await source.load(user_id)
    # 커밋 뒤 쓰기 응답에서도 호출되므로 원장 실패가 영속된 논지를 500으로 만들면 안 된다.
    except Exception:
        return {}
    return {ticker: ledger.instrument(ticker).name for ticker in tickers}


@router.delete("/facts/{fact_id}")
async def delete_fact(
    fact_id: str,
    user_id: CurrentUser,
    db: DbSession,
    reason: str = Query(
        default=DeleteReason.USER_DELETED,
        json_schema_extra={"enum": [reason.value for reason in DeleteReason]},
    ),
) -> Envelope[DeletedFactContent]:
    """소프트 삭제. 행과 삭제 사유는 남고 읽기 경로에서만 사라진다."""
    try:
        delete_reason = DeleteReason(reason)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail="지원하지 않는 삭제 사유입니다.") from exc
    fact = await soft_delete_fact(db, user_id, fact_id, reason=delete_reason)
    await db.commit()
    return Envelope[DeletedFactContent](
        content={"id": str(fact.id), "deleted_at": fact.deleted_at, "reason": fact.deleted_reason}
    )
