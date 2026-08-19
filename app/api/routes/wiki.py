"""투자 논지와 사용자 맥락. API 명세 §9.

담당 트랙: feat/wiki-crud
"""

from __future__ import annotations

from fastapi import APIRouter
from pydantic import BaseModel, Field

from app.api.deps import CurrentUser, DbSession
from app.core.enums import ThesisHorizon
from app.core.errors import InsufficientData
from app.core.schemas import Envelope

router = APIRouter(prefix="/wiki", tags=["wiki"])


class ThesisIn(BaseModel):
    ticker: str = Field(min_length=6, max_length=6)
    text: str = Field(min_length=1, max_length=500)
    horizon: ThesisHorizon | None = None
    linked_trade_id: str | None = None


@router.get("")
async def get_wiki(user_id: CurrentUser, db: DbSession) -> Envelope[dict]:
    raise InsufficientData("Wiki 저장소가 아직 연결되지 않았습니다.")


@router.post("/theses")
async def create_thesis(
    body: ThesisIn, user_id: CurrentUser, db: DbSession
) -> Envelope[dict]:
    raise InsufficientData("Wiki 저장소가 아직 연결되지 않았습니다.")


@router.put("/theses/{ticker}")
async def update_thesis(
    ticker: str, body: ThesisIn, user_id: CurrentUser, db: DbSession
) -> Envelope[dict]:
    raise InsufficientData("Wiki 저장소가 아직 연결되지 않았습니다.")


@router.delete("/facts/{fact_id}")
async def delete_fact(
    fact_id: str, user_id: CurrentUser, db: DbSession
) -> Envelope[dict]:
    raise InsufficientData("Wiki 저장소가 아직 연결되지 않았습니다.")
