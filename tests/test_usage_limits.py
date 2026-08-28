"""사용자 호출량과 GMS 일일 예산 보호 테스트."""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from app.core.config import settings
from app.core.errors import RateLimited
from app.core.usage_limits import UsageGuard, reset_usage

NOW = datetime(2026, 8, 28, 12, 0, tzinfo=ZoneInfo("Asia/Seoul"))


@pytest.mark.asyncio
async def test_사용자와_엔드포인트별로_요청_한도를_분리한다(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "ai_rate_limit_chat_per_minute", 1)
    guard = UsageGuard()

    first = await guard.enter("u1", "chat", now=NOW)
    reset_usage(first)
    other_user = await guard.enter("u2", "chat", now=NOW)
    reset_usage(other_user)
    other_endpoint = await guard.enter("u1", "stocks.analysis", now=NOW)
    reset_usage(other_endpoint)

    with pytest.raises(RateLimited) as caught:
        await guard.enter("u1", "chat", now=NOW)
    assert caught.value.detail["reason"] == "request_rate_limit"
    assert caught.value.detail["retry_after_seconds"] == 60


@pytest.mark.asyncio
async def test_동시_GMS_예약은_일일_예산을_넘지_않는다(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "ai_daily_token_budget", 30_000)
    guard = UsageGuard()
    token = await guard.enter("u1", "chat", now=NOW)
    try:
        reservation = await guard.reserve(20_000)
        with pytest.raises(RateLimited) as caught:
            await guard.reserve(20_000)
        assert caught.value.detail["reason"] == "daily_token_budget"
        await guard.settle(reservation, input_tokens=11, output_tokens=7)
        assert await guard.reserve(20_000) is not None
    finally:
        reset_usage(token)


@pytest.mark.asyncio
async def test_DB의_오늘_사용량도_예산에_포함한다(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "ai_daily_token_budget", 30_000)

    class Db:
        async def scalar(self, _: object) -> int:
            return 15_000

    guard = UsageGuard()
    token = await guard.enter("u1", "chat", now=NOW, db=Db())
    try:
        with pytest.raises(RateLimited):
            await guard.reserve(20_000)
    finally:
        reset_usage(token)


@pytest.mark.asyncio
async def test_캐시_응답은_GMS_예산을_예약하지_않는다(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "ai_daily_token_budget", 20_000)
    guard = UsageGuard()
    cached_request = await guard.enter("u1", "stocks.analysis", now=NOW)
    reset_usage(cached_request)  # GMS reserve를 호출하지 않은 캐시 경로

    next_request = await guard.enter("u1", "stocks.analysis", now=NOW)
    try:
        assert await guard.reserve(20_000) is not None
    finally:
        reset_usage(next_request)
