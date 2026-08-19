"""라우터 공통 의존성."""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends, Header
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_session
from app.core.errors import Unauthorized

DbSession = Annotated[AsyncSession, Depends(get_session)]


async def get_current_user_id(
    authorization: Annotated[str | None, Header()] = None,
) -> str:
    """Bearer 토큰에서 사용자 식별자를 꺼낸다.

    사용자 식별자는 경로나 본문에 넣지 않는다. API 명세 §2.1 참조.

    TODO: 백엔드와 서명 키를 맞춘 뒤 실제 JWT 검증으로 교체한다.
          지금은 개발용으로 토큰 문자열을 그대로 사용자 id로 쓴다.
    """
    if not authorization or not authorization.lower().startswith("bearer "):
        raise Unauthorized("인증 토큰이 없습니다.")
    token = authorization.split(" ", 1)[1].strip()
    if not token:
        raise Unauthorized("인증 토큰이 비어 있습니다.")
    return token


CurrentUser = Annotated[str, Depends(get_current_user_id)]
