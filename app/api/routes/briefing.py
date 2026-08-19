"""데일리 브리핑. API 명세 §8.

배치 생성 결과 조회이므로 GET이다. 랭킹은 규칙 엔진이 하고 LLM은 상위 4건만 문장화한다.
담당 트랙: feat/event-ranking
"""

from __future__ import annotations

from datetime import date as Date

from fastapi import APIRouter

from app.api.deps import CurrentUser, DbSession
from app.core.enums import BriefingStatus
from app.core.schemas import Envelope

router = APIRouter(prefix="/briefing", tags=["briefing"])


@router.get("")
async def get_briefing(
    user_id: CurrentUser, db: DbSession, date: Date | None = None
) -> Envelope[dict]:
    return Envelope(content={"date": date, "status": BriefingStatus.EMPTY, "items": []})
