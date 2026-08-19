"""종목 AI 분석. API 명세 §3.

담당 트랙: feat/rag-dart, feat/rag-search, feat/llm-pipeline
"""

from __future__ import annotations

from fastapi import APIRouter
from pydantic import BaseModel, Field

from app.api.deps import CurrentUser, DbSession
from app.core.errors import InsufficientData
from app.core.schemas import Envelope

router = APIRouter(prefix="/stocks", tags=["stocks"])

SECTIONS = (
    "current", "changes", "attention", "risks",
    "my_impact", "thesis_check", "next_events",
)


class AnalysisRequest(BaseModel):
    sections: list[str] | None = Field(default=None)
    personalize: bool = True


@router.post("/{ticker}/analysis")
async def create_analysis(
    ticker: str,
    body: AnalysisRequest,
    user_id: CurrentUser,
    db: DbSession,
) -> Envelope[dict]:
    raise InsufficientData("종목 분석 파이프라인이 아직 연결되지 않았습니다.")
