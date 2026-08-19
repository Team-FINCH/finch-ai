"""Ask My Portfolio. API 명세 §4.

유일한 도구 호출 에이전트. 나머지 기능의 파이프라인을 Tool로 재사용한다.
담당 트랙: feat/llm-agent
"""

from __future__ import annotations

from fastapi import APIRouter
from pydantic import BaseModel

from app.api.deps import CurrentUser, DbSession
from app.core.enums import Screen
from app.core.errors import InsufficientData
from app.core.schemas import Envelope

router = APIRouter(prefix="/chat", tags=["chat"])


class ChatContext(BaseModel):
    screen: Screen = Screen.CHAT
    ticker: str | None = None


class ChatRequest(BaseModel):
    conversation_id: str | None = None
    message: str
    context: ChatContext = ChatContext()


@router.post("")
async def chat(body: ChatRequest, user_id: CurrentUser, db: DbSession) -> Envelope[dict]:
    raise InsufficientData("대화 에이전트가 아직 연결되지 않았습니다.")
