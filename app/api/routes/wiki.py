"""투자 논지와 사용자 맥락. API 명세 §9.

담당 트랙: feat/wiki-crud
"""

from __future__ import annotations

from fastapi import APIRouter
from pydantic import BaseModel, Field

from app.api.deps import CurrentUser, DbSession
from app.core.enums import ThesisHorizon
from app.core.schemas import Envelope
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


@router.get("")
async def get_wiki(user_id: CurrentUser, db: DbSession) -> Envelope[dict]:
    """ "AI가 이해한 나" 화면 한 벌.

    항목마다 source를 그대로 실어 보낸다. ai_inferred는 단정투로 렌더링하면 안 되고,
    그 판단은 화면이 한다.
    """
    return Envelope(
        content={
            "profile": [fact_payload(f) for f in await list_facts(db, user_id)],
            "theses": [thesis_payload(t) for t in await list_theses(db, user_id)],
        }
    )


@router.post("/theses")
async def create_thesis(body: ThesisIn, user_id: CurrentUser, db: DbSession) -> Envelope[dict]:
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
    return Envelope(content=thesis_payload(thesis))


@router.put("/theses/{ticker}")
async def update_thesis(
    ticker: str, body: ThesisIn, user_id: CurrentUser, db: DbSession
) -> Envelope[dict]:
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
    return Envelope(content=thesis_payload(thesis))


@router.delete("/facts/{fact_id}")
async def delete_fact(fact_id: str, user_id: CurrentUser, db: DbSession) -> Envelope[dict]:
    """소프트 삭제. 행은 남고 읽기 경로에서만 사라진다."""
    fact = await soft_delete_fact(db, user_id, fact_id)
    await db.commit()
    return Envelope(content={"id": str(fact.id), "deleted_at": fact.deleted_at})
