"""라우터 공통 의존성."""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends, Header
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.db import get_session
from app.core.errors import Unauthorized

DbSession = Annotated[AsyncSession, Depends(get_session)]


async def get_current_user_id(
    user_id: Annotated[str | None, Header(alias=settings.trusted_user_header)] = None,
) -> str:
    """백엔드가 넘긴 신뢰 헤더에서 사용자 식별자를 꺼낸다.

    호출 경로가 프런트 → 백엔드 → AI 로 고정되어 있어 JWT 검증은 백엔드가 이미
    끝냈다. 여기서 다시 검증하면 서명 키를 두 곳에서 관리하게 되므로 하지 않는다.
    사용자 식별자는 경로나 본문에 넣지 않는다. API 명세 §2.1 참조.

    이 값은 검증 없이 그대로 믿는다. 그래서 **AI 서버는 내부 네트워크에서만
    접근 가능해야 한다** — 외부에 노출되면 누구나 헤더를 위조해 남의 위키와
    포트폴리오를 읽는다. docs/api-contract-proposal.md §4 참조.
    """
    if not user_id or not user_id.strip():
        raise Unauthorized("사용자 식별 헤더가 없습니다.")
    return user_id.strip()


CurrentUser = Annotated[str, Depends(get_current_user_id)]
