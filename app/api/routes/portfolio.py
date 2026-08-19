"""포트폴리오 진단과 수익률 원인 분석. API 명세 §5 · §6.

담당 트랙: feat/engine-risk, feat/engine-attribution
"""

from __future__ import annotations

from fastapi import APIRouter
from pydantic import BaseModel

from app.api.deps import CurrentUser, DbSession
from app.core.enums import Period
from app.core.errors import InsufficientData
from app.core.schemas import Envelope

router = APIRouter(prefix="/portfolio", tags=["portfolio"])


class AttributionRequest(BaseModel):
    period: Period = Period.D1
    benchmark: str | None = None


@router.post("/diagnosis")
async def diagnosis(user_id: CurrentUser, db: DbSession) -> Envelope[dict]:
    raise InsufficientData("Risk Engine이 아직 연결되지 않았습니다.")


@router.post("/attribution")
async def attribution(
    body: AttributionRequest, user_id: CurrentUser, db: DbSession
) -> Envelope[dict]:
    raise InsufficientData("Attribution Engine이 아직 연결되지 않았습니다.")
