"""응답 피드백 수집 — API 명세 §10.

평가는 응답 단위다. `request_id`로 어떤 응답에 대한 것인지 지목하며, 그
`request_id`는 봉투에 실려 나간 값이다. `ai_feedback`이 `ai_responses`를 외래키로
참조하므로, 기록되지 않은 응답에는 피드백을 달 수 없다.

한 응답에 대한 평가는 하나다. 같은 `request_id`로 다시 보내면 마지막 것이 남는다 —
사용자가 별점을 고쳐 다는 것은 정상적인 행동이고, 그때마다 행을 쌓으면 집계가
평가 횟수를 세게 된다.
"""

from __future__ import annotations

from fastapi import APIRouter
from pydantic import BaseModel, Field
from sqlalchemy import select

from app.api.deps import CurrentUser, DbSession
from app.core.enums import FeedbackRating, FeedbackReason
from app.core.errors import InvalidRequest
from app.core.models import AIFeedback, AIResponse
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
    """평가를 기록한다.

    남의 응답에 평가를 달 수 없도록 `user_id`까지 함께 대조한다. 존재하지 않는
    `request_id`와 남의 `request_id`를 같은 오류로 돌려주는 것은 의도한 것이다 —
    구분해 주면 남의 request_id를 하나씩 찔러 존재 여부를 알아낼 수 있다.
    """
    owner = await db.scalar(
        select(AIResponse.user_id).where(AIResponse.request_id == body.request_id)
    )
    if owner is None or owner != user_id:
        raise InvalidRequest("평가할 응답을 찾을 수 없습니다.")

    row = await db.scalar(
        select(AIFeedback).where(AIFeedback.request_id == body.request_id)
    )
    if row is None:
        row = AIFeedback(request_id=body.request_id, user_id=user_id)
        db.add(row)

    row.rating = body.rating
    row.reasons = [r.value for r in body.reasons]
    row.comment = body.comment
    await db.commit()

    return Envelope[dict](content={"recorded": True})
