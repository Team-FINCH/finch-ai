"""라우터 공통 의존성."""

from __future__ import annotations

import hmac
from typing import Annotated

from fastapi import Depends, Header
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.db import get_session
from app.core.errors import Unauthorized

DbSession = Annotated[AsyncSession, Depends(get_session)]

#: 401 문구는 사유를 가린다. 어느 헤더가 왜 틀렸는지 알려주면 토큰을 맞춰 볼
#: 여지를 준다. 구분이 필요한 쪽은 detail.reason 을 본다 — 내부 전용 API 다.
_DENIED = "인증에 실패했습니다."


def _check_internal_token(token: str | None) -> None:
    """호출자가 백엔드가 맞는지 확인한다. 백엔드 API 명세 §9.

    사용자 식별자를 검증 없이 믿기 때문에, 아무나 이 서버에 닿을 수 있으면
    헤더를 위조해 남의 데이터를 읽는다. 공유 토큰이 그 문을 잠근다.

    토큰이 비어 있으면 로컬에서만 통과시킨다. 운영에서 토큰을 안 넣는 것은
    설정 사고이고, 그때 열어 두면 인증이 아예 없는 것과 같다 — 그래서
    `app_env`가 local이 아니면 막는다. 이건 네트워크 격리를 대신하지 않는다.
    """
    expected = settings.backend_service_token
    if not expected:
        if settings.app_env != "local":
            raise Unauthorized(_DENIED, detail={"reason": "internal_token_not_configured"})
        return
    # 앞에서부터 다른 자리를 찾는 비교는 걸린 시간으로 토큰을 알려 준다.
    if not token or not hmac.compare_digest(token, expected):
        raise Unauthorized(_DENIED, detail={"reason": "internal_token_mismatch"})


async def get_current_user_id(
    user_id: Annotated[str | None, Header(alias=settings.trusted_user_header)] = None,
    internal_token: Annotated[
        str | None, Header(alias=settings.internal_token_header)
    ] = None,
) -> str:
    """백엔드가 넘긴 신뢰 헤더에서 사용자 식별자를 꺼낸다.

    호출 경로가 프런트 → 백엔드 → AI 로 고정되어 있어 JWT 검증은 백엔드가 이미
    끝냈다. 여기서 다시 검증하면 서명 키를 두 곳에서 관리하게 되므로 하지 않는다.
    사용자 식별자는 경로나 본문에 넣지 않는다. API 명세 §2.1 참조.

    헤더 두 개가 짝이다(백엔드 명세 §9). `X-Internal-Token`으로 호출자가
    백엔드임을 확인하고, `X-User-Id`로 누구의 데이터인지 정한다. 뒤쪽 값은
    검증하지 않으므로 **AI 서버는 내부 네트워크에서만 접근 가능해야 한다** —
    토큰까지 새면 누구나 남의 위키와 포트폴리오를 읽는다.
    docs/api/aiApiSpec.md §4 참조.
    """
    _check_internal_token(internal_token)
    if not user_id or not user_id.strip():
        raise Unauthorized(_DENIED, detail={"reason": "user_header_missing"})
    return user_id.strip()


CurrentUser = Annotated[str, Depends(get_current_user_id)]
