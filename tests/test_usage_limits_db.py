"""PostgreSQL 장부가 여러 Pod 역할의 Guard 사이에서 공유되는지 검증한다."""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.core.config import settings
from app.core.errors import RateLimited
from app.core.usage_limits import UsageGuard, reset_usage

NOW = datetime(2026, 8, 28, 12, 0, tzinfo=ZoneInfo("Asia/Seoul"))
test_engine = create_async_engine(settings.database_url, poolclass=NullPool)
TestSessions = async_sessionmaker(test_engine, expire_on_commit=False)


async def _clean(user_id: str) -> None:
    async with TestSessions() as db, db.begin():
        for table in (
            "ai_token_reservations",
            "ai_token_daily",
            "ai_request_windows",
        ):
            await db.execute(
                text(f"DELETE FROM {table} WHERE user_id=:user_id"),  # noqa: S608
                {"user_id": user_id},
            )


@pytest.mark.asyncio
async def test_서로_다른_Guard가_같은_요청_창을_공유한다(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user_id = f"test-{uuid.uuid4().hex}"
    monkeypatch.setattr(settings, "ai_rate_limit_chat_per_minute", 1)
    await _clean(user_id)
    try:
        first = UsageGuard(TestSessions)
        second = UsageGuard(TestSessions)
        token = await first.enter(user_id, "chat", now=NOW)
        reset_usage(token)

        with pytest.raises(RateLimited) as caught:
            await second.enter(user_id, "chat", now=NOW)
        assert caught.value.detail["reason"] == "request_rate_limit"
    finally:
        await _clean(user_id)


@pytest.mark.asyncio
async def test_토큰_예약과_정산을_Guard_간에_공유한다(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user_id = f"test-{uuid.uuid4().hex}"
    monkeypatch.setattr(settings, "ai_daily_token_budget", 30_000)
    first = UsageGuard(TestSessions)
    second = UsageGuard(TestSessions)
    await _clean(user_id)
    try:
        token = await first.enter(user_id, "chat", now=NOW)
        reservation = await first.reserve(20_000)
        reset_usage(token)

        token = await second.enter(user_id, "chat", now=NOW)
        with pytest.raises(RateLimited):
            await second.reserve(20_000)
        reset_usage(token)

        token = await first.enter(user_id, "stocks.analysis", now=NOW)
        await first.settle(reservation, input_tokens=11, output_tokens=7)
        reset_usage(token)

        token = await second.enter(user_id, "stocks.analysis", now=NOW)
        assert await second.reserve(20_000) is not None
        reset_usage(token)
    finally:
        await _clean(user_id)


@pytest.mark.asyncio
async def test_창_경계에서_두_배_버스트가_통과하지_않는다(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """고정 창 하나만 세면 12:00:59의 4건과 12:01:01의 4건이 모두 통과한다."""
    user_id = f"test-{uuid.uuid4().hex}"
    monkeypatch.setattr(settings, "ai_rate_limit_chat_per_minute", 4)
    guard = UsageGuard(TestSessions)
    await _clean(user_id)
    try:
        end_of_window = NOW.replace(second=59)
        for _ in range(4):
            reset_usage(await guard.enter(user_id, "chat", now=end_of_window))

        # 창이 바뀌어도 직전 4건이 겹치는 비율(59/60)만큼 남아 있다.
        next_window = NOW + timedelta(minutes=1, seconds=1)
        with pytest.raises(RateLimited) as caught:
            await guard.enter(user_id, "chat", now=next_window)
        assert caught.value.detail["reason"] == "request_rate_limit"
        # 4 * (59-14)/60 + 0 + 1 == 4 이므로 14초 뒤에 정확히 한 건이 열린다.
        assert caught.value.detail["retry_after_seconds"] == 14

        reset_usage(
            await guard.enter(user_id, "chat", now=next_window + timedelta(seconds=14))
        )
    finally:
        await _clean(user_id)


@pytest.mark.asyncio
async def test_거절된_요청은_창에_기록되지_않는다(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """막힌 요청까지 세면 그 숫자가 다음 창의 직전 값이 되어 계속 눌린다."""
    user_id = f"test-{uuid.uuid4().hex}"
    monkeypatch.setattr(settings, "ai_rate_limit_chat_per_minute", 1)
    guard = UsageGuard(TestSessions)
    await _clean(user_id)
    try:
        reset_usage(await guard.enter(user_id, "chat", now=NOW))
        for _ in range(3):
            with pytest.raises(RateLimited):
                await guard.enter(user_id, "chat", now=NOW)

        async with TestSessions() as db:
            count = await db.scalar(
                text("SELECT request_count FROM ai_request_windows WHERE user_id=:user_id"),
                {"user_id": user_id},
            )
        assert count == 1

        # 두 칸을 지나면 기록이 잊히고 다시 열린다. 연타가 이 시점을 밀지 못한다.
        reset_usage(await guard.enter(user_id, "chat", now=NOW + timedelta(seconds=120)))
    finally:
        await _clean(user_id)


@pytest.mark.asyncio
async def test_배치_장부는_영속되고_사용자_예산과_섞이지_않는다(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    batch_user = f"system:batch-{uuid.uuid4().hex[:8]}"
    user_id = f"test-{uuid.uuid4().hex}"
    monkeypatch.setattr(settings, "ai_daily_token_budget", 20_000)
    await _clean(batch_user)
    await _clean(user_id)
    try:
        first = UsageGuard(TestSessions)
        token = await first.enter_system(batch_user, "briefing.batch", now=NOW, budget=30_000)
        await first.settle(await first.reserve(20_000), input_tokens=20_000, output_tokens=0)
        reset_usage(token)

        # 다른 Pod의 배치도 같은 장부를 본다.
        second = UsageGuard(TestSessions)
        token = await second.enter_system(batch_user, "briefing.batch", now=NOW, budget=30_000)
        with pytest.raises(RateLimited) as caught:
            await second.reserve(20_000)
        assert caught.value.detail["limit_tokens"] == 30_000
        reset_usage(token)

        # 배치가 쓴 20,000 토큰은 사용자 몫을 건드리지 않는다.
        token = await second.enter(user_id, "briefing", now=NOW)
        assert await second.reserve(20_000) is not None
        reset_usage(token)
    finally:
        await _clean(batch_user)
        await _clean(user_id)


@pytest.mark.asyncio
async def test_Pod_장애로_남은_예약은_만료_후_회수된다(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user_id = f"test-{uuid.uuid4().hex}"
    monkeypatch.setattr(settings, "ai_daily_token_budget", 20_000)
    guard = UsageGuard(TestSessions)
    await _clean(user_id)
    try:
        token = await guard.enter(user_id, "chat", now=NOW)
        await guard.reserve(20_000)
        reset_usage(token)

        async with TestSessions() as db, db.begin():
            await db.execute(
                text("UPDATE ai_token_reservations SET expires_at=:expired WHERE user_id=:user_id"),
                {
                    "expired": datetime.now(ZoneInfo("Asia/Seoul")) - timedelta(seconds=1),
                    "user_id": user_id,
                },
            )

        second = UsageGuard(TestSessions)
        token = await second.enter(user_id, "stocks.analysis", now=NOW)
        assert await second.reserve(20_000) is not None
        reset_usage(token)
    finally:
        await _clean(user_id)
