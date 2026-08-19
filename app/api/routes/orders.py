"""주문 전 점검. API 명세 §7.

진단 엔진을 두 번 호출한 차분이다. 별도 시뮬레이션 엔진을 두지 않는다.
담당 트랙: feat/engine-risk
"""

from __future__ import annotations

from fastapi import APIRouter
from pydantic import BaseModel, Field

from app.api.deps import CurrentUser, DbSession
from app.core.enums import OrderSide
from app.core.errors import InsufficientData
from app.core.schemas import Envelope

router = APIRouter(prefix="/orders", tags=["orders"])


class OrderLine(BaseModel):
    ticker: str = Field(min_length=6, max_length=6)
    side: OrderSide
    quantity: int = Field(gt=0)
    price: int | None = None


class PreviewRequest(BaseModel):
    orders: list[OrderLine] = Field(min_length=1)


@router.post("/preview")
async def preview(
    body: PreviewRequest, user_id: CurrentUser, db: DbSession
) -> Envelope[dict]:
    raise InsufficientData("주문 시뮬레이션이 아직 연결되지 않았습니다.")
