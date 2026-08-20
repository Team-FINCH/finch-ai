"""비동기 DB 세션.

엔진은 프로세스당 하나만 만든다. 세션은 요청 단위로 열고 닫는다.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.core.config import settings

engine = create_async_engine(
    settings.database_url,
    echo=settings.db_echo,
    pool_pre_ping=True,
)

SessionFactory = async_sessionmaker(
    engine,
    class_=AsyncSession,
    expire_on_commit=False,
    autoflush=False,
)


async def get_session() -> AsyncIterator[AsyncSession]:
    """FastAPI 의존성.

    롤백은 `async with`의 종료 처리가 담당한다. 여기서 except로 잡아 다시 던지면
    예외가 의존성 해제 단계에서 재발생해 앱의 예외 핸들러를 우회한다.
    그러면 AppError가 정상 에러 응답으로 변환되지 않고 500으로 새어 나간다.
    """
    async with SessionFactory() as session:
        yield session
