"""응답 피드백. API 명세 §10.

reasons 분포가 프롬프트 개선의 우선순위를 정하므로 Phase 1부터 붙인다.
"""

from __future__ import annotations

from fastapi import APIRouter
from pydantic import BaseModel, Field

from app.api.deps import CurrentUser, DbSession
from app.core.enums import FeedbackRating, FeedbackReason
from app.core.errors import InsufficientData
from app.core.schemas import Envelope

router = APIRouter(prefix="/feedback", tags=["feedback"])


class FeedbackIn(BaseModel):
    request_id: str
    rating: FeedbackRating
    reasons: list[FeedbackReason] = Field(default_factory=list)
    comment: str | None = Field(default=None, max_length=1000)


@router.post("")
async def submit_feedback(
    body: FeedbackIn, user_id: CurrentUser, db: DbSession
) -> Envelope[dict]:
    raise InsufficientData("응답 로그 저장소가 아직 연결되지 않았습니다.")
