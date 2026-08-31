"""사용자별 요청 횟수와 일일 GMS 토큰 예산.

요청 횟수는 API 진입에서 세고, 토큰은 실제 GMS 호출 직전에 예약한다. 캐시가
응답을 대신하면 GMS 예약 함수가 호출되지 않으므로 토큰 예산을 쓰지 않는다.

배치처럼 사용자가 누르지 않은 호출은 `enter_system`으로 시스템 몫의 장부를 연다.
개인 예산과 섞이지 않으면서 총량 상한과 토큰 기록은 그대로 남는다.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections import defaultdict, deque
from contextvars import ContextVar, Token
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import text

from app.core.config import settings
from app.core.errors import RateLimited


@dataclass(slots=True)
class UsageCounter:
    user_id: str
    endpoint: str
    day: date
    #: 이 문맥에 적용할 일일 토큰 상한. 사용자 요청과 배치가 서로 다른 값을 쓴다.
    budget: int = 0
    db: Any = None
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0


@dataclass(frozen=True, slots=True)
class TokenReservation:
    user_id: str
    day: date
    amount: int
    id: uuid.UUID | None = None


@dataclass(slots=True)
class DailyUsage:
    spent: int
    reserved: int = 0


_current: ContextVar[UsageCounter | None] = ContextVar("ai_usage", default=None)
log = logging.getLogger("app.core.usage_limits")
KST = ZoneInfo("Asia/Seoul")

#: 브리핑 배치가 쓰는 시스템 장부의 주인. 실제 사용자 ID와 겹치지 않도록 접두를
#: 붙인다. 장부 테이블의 user_id 컬럼이 40자이므로 이름을 더 늘리지 않는다.
BRIEFING_BATCH_USER = "system:briefing-batch"


class UsageGuard:
    """DB 설정 시 모든 Pod가 공유하고, 없으면 로컬 메모리로 동작하는 장부."""

    def __init__(self, session_factory: Any = None) -> None:
        self._store = PersistentUsageStore(session_factory) if session_factory else None
        self._lock = asyncio.Lock()
        self._requests: dict[tuple[str, str], deque[float]] = defaultdict(deque)
        self._daily: dict[tuple[str, date], DailyUsage] = {}

    async def enter(
        self,
        user_id: str,
        endpoint: str,
        *,
        now: datetime,
        db: Any = None,
    ) -> Token[UsageCounter | None]:
        key = (user_id, endpoint)
        limit = endpoint_limit(endpoint)
        timestamp = now.timestamp()
        budget = settings.ai_daily_token_budget
        if self._store is not None:
            await self._store.check_request(user_id, endpoint, now=now, limit=limit)
            return _current.set(
                UsageCounter(
                    user_id=user_id, endpoint=endpoint, day=now.date(), budget=budget
                )
            )
        async with self._lock:
            window = self._requests[key]
            cutoff = timestamp - settings.ai_rate_limit_window_s
            while window and window[0] <= cutoff:
                window.popleft()
            if len(window) >= limit:
                retry_after = max(1, round(window[0] + settings.ai_rate_limit_window_s - timestamp))
                raise RateLimited(
                    "요청이 너무 많습니다. 잠시 후 다시 시도해 주세요.",
                    detail={
                        "reason": "request_rate_limit",
                        "endpoint": endpoint,
                        "retry_after_seconds": retry_after,
                    },
                )
            window.append(timestamp)
        return _current.set(
            UsageCounter(
                user_id=user_id, endpoint=endpoint, day=now.date(), budget=budget, db=db
            )
        )

    async def enter_system(
        self,
        user_id: str,
        endpoint: str,
        *,
        now: datetime,
        budget: int,
    ) -> Token[UsageCounter | None]:
        """배치·스케줄러용 문맥. 토큰 장부만 열고 요청 창은 세지 않는다.

        분당 횟수 한도는 한 사람이 화면을 연타하는 것을 막는 장치다. 대상 사용자
        수만큼 도는 배치에 그대로 걸면 대상이 늘어날수록 정상 실행이 막힌다.
        새는 것은 요금이므로 토큰 쪽만 잠근다.
        """
        return _current.set(
            UsageCounter(user_id=user_id, endpoint=endpoint, day=now.date(), budget=budget)
        )

    async def reserve(self, amount: int) -> TokenReservation | None:
        counter = _current.get()
        if counter is None or counter.budget <= 0:
            return None
        amount = max(amount, 1)
        if self._store is not None:
            return await self._store.reserve(
                counter.user_id,
                counter.day,
                amount=amount,
                limit=counter.budget,
            )
        key = (counter.user_id, counter.day)
        async with self._lock:
            known = key in self._daily
        if not known:
            # 캐시 응답은 reserve까지 오지 않으므로 DB 조회도 하지 않는다.
            baseline = await spent_today(counter.db, counter.user_id, counter.day)
            async with self._lock:
                self._daily.setdefault(key, DailyUsage(spent=baseline))
        async with self._lock:
            usage = self._daily.setdefault(key, DailyUsage(spent=0))
            if usage.spent + usage.reserved + amount > counter.budget:
                raise RateLimited(
                    "오늘 사용할 수 있는 AI 분석량을 모두 사용했습니다.",
                    detail={
                        "reason": "daily_token_budget",
                        "used_tokens": usage.spent,
                        "reserved_tokens": usage.reserved,
                        "limit_tokens": counter.budget,
                    },
                )
            usage.reserved += amount
        return TokenReservation(counter.user_id, counter.day, amount)

    async def settle(
        self,
        reservation: TokenReservation | None,
        *,
        input_tokens: int = 0,
        output_tokens: int = 0,
        cache_read_tokens: int = 0,
    ) -> None:
        counter = _current.get()
        actual = max(input_tokens, 0) + max(output_tokens, 0)
        if reservation is not None:
            if self._store is not None:
                await self._store.settle(reservation, actual=actual)
            else:
                async with self._lock:
                    usage = self._daily.setdefault(
                        (reservation.user_id, reservation.day), DailyUsage(spent=0)
                    )
                    usage.reserved = max(usage.reserved - reservation.amount, 0)
                    usage.spent += actual
        if counter is not None:
            counter.input_tokens += max(input_tokens, 0)
            counter.output_tokens += max(output_tokens, 0)
            counter.cache_read_tokens += max(cache_read_tokens, 0)

    async def cancel(self, reservation: TokenReservation | None) -> None:
        if reservation is None:
            return
        if self._store is not None:
            await self._store.cancel(reservation)
            return
        async with self._lock:
            usage = self._daily.get((reservation.user_id, reservation.day))
            if usage is not None:
                usage.reserved = max(usage.reserved - reservation.amount, 0)


class PersistentUsageStore:
    """짧은 독립 트랜잭션과 advisory lock으로 Pod 간 경쟁을 직렬화한다."""

    def __init__(self, session_factory: Any) -> None:
        self._sessions = session_factory

    async def _lock(self, db: Any, key: str) -> None:
        await db.execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
            {"key": key},
        )

    async def check_request(
        self, user_id: str, endpoint: str, *, now: datetime, limit: int
    ) -> None:
        cutoff = now - timedelta(seconds=settings.ai_rate_limit_window_s)
        async with self._sessions() as db, db.begin():
            row = (
                await db.execute(
                    text(
                        """
                        INSERT INTO ai_request_windows
                            (user_id, endpoint, window_started_at, request_count)
                        VALUES (:user_id, :endpoint, :now, 1)
                        ON CONFLICT (user_id, endpoint) DO UPDATE SET
                            window_started_at = CASE
                                WHEN ai_request_windows.window_started_at <= :cutoff
                                THEN :now ELSE ai_request_windows.window_started_at END,
                            request_count = CASE
                                WHEN ai_request_windows.window_started_at <= :cutoff
                                THEN 1 ELSE ai_request_windows.request_count + 1 END
                        RETURNING window_started_at, request_count
                        """
                    ),
                    {
                        "user_id": user_id,
                        "endpoint": endpoint,
                        "now": now,
                        "cutoff": cutoff,
                    },
                )
            ).one()
        if row.request_count > limit:
            retry_after = max(
                1,
                round(
                    (
                        row.window_started_at
                        + timedelta(seconds=settings.ai_rate_limit_window_s)
                        - now
                    ).total_seconds()
                ),
            )
            raise RateLimited(
                "요청이 너무 많습니다. 잠시 후 다시 시도해 주세요.",
                detail={
                    "reason": "request_rate_limit",
                    "endpoint": endpoint,
                    "retry_after_seconds": retry_after,
                },
            )

    async def reserve(
        self, user_id: str, day: date, *, amount: int, limit: int
    ) -> TokenReservation:
        reservation_id = uuid.uuid4()
        now = datetime.now(KST)
        async with self._sessions() as db, db.begin():
            await self._lock(db, f"ai-token:{user_id}:{day.isoformat()}")
            await db.execute(
                text("DELETE FROM ai_token_reservations WHERE expires_at <= :now"),
                {"now": now},
            )
            spent = int(
                await db.scalar(
                    text(
                        "SELECT COALESCE(spent_tokens, 0) FROM ai_token_daily "
                        "WHERE user_id=:user_id AND usage_date=:day"
                    ),
                    {"user_id": user_id, "day": day},
                )
                or 0
            )
            reserved = int(
                await db.scalar(
                    text(
                        "SELECT COALESCE(SUM(amount), 0) FROM ai_token_reservations "
                        "WHERE user_id=:user_id AND usage_date=:day AND expires_at > :now"
                    ),
                    {"user_id": user_id, "day": day, "now": now},
                )
                or 0
            )
            if spent + reserved + amount > limit:
                raise RateLimited(
                    "오늘 사용할 수 있는 AI 분석량을 모두 사용했습니다.",
                    detail={
                        "reason": "daily_token_budget",
                        "used_tokens": spent,
                        "reserved_tokens": reserved,
                        "limit_tokens": limit,
                    },
                )
            await db.execute(
                text(
                    "INSERT INTO ai_token_reservations "
                    "(id,user_id,usage_date,amount,expires_at) "
                    "VALUES (:id,:user_id,:day,:amount,:expires_at)"
                ),
                {
                    "id": reservation_id,
                    "user_id": user_id,
                    "day": day,
                    "amount": amount,
                    "expires_at": now + timedelta(seconds=settings.ai_gms_reservation_ttl_s),
                },
            )
        return TokenReservation(user_id, day, amount, reservation_id)

    async def settle(self, reservation: TokenReservation, *, actual: int) -> None:
        async with self._sessions() as db, db.begin():
            await self._lock(db, f"ai-token:{reservation.user_id}:{reservation.day.isoformat()}")
            await db.execute(
                text("DELETE FROM ai_token_reservations WHERE id=:id"),
                {"id": reservation.id},
            )
            await db.execute(
                text(
                    """
                    INSERT INTO ai_token_daily (user_id, usage_date, spent_tokens)
                    VALUES (:user_id, :day, :actual)
                    ON CONFLICT (user_id, usage_date) DO UPDATE SET
                        spent_tokens = ai_token_daily.spent_tokens + :actual,
                        updated_at = now()
                    """
                ),
                {
                    "user_id": reservation.user_id,
                    "day": reservation.day,
                    "actual": actual,
                },
            )

    async def cancel(self, reservation: TokenReservation) -> None:
        async with self._sessions() as db, db.begin():
            await db.execute(
                text("DELETE FROM ai_token_reservations WHERE id=:id"),
                {"id": reservation.id},
            )


def default_guard() -> UsageGuard:
    """환경에 맞는 장부를 고른다. 앱과 배치가 서로 다른 규칙을 쓰면 안 된다.

    운영·스테이징은 PostgreSQL 장부를 공유하고, 로컬은 DB 없이도 개발할 수 있게
    프로세스 메모리 장부를 쓴다.
    """
    from app.core.db import SessionFactory

    return UsageGuard(SessionFactory if settings.app_env != "local" else None)


def endpoint_limit(endpoint: str) -> int:
    return {
        "stocks.analysis": settings.ai_rate_limit_stocks_per_minute,
        "chat": settings.ai_rate_limit_chat_per_minute,
        "portfolio.diagnosis": settings.ai_rate_limit_portfolio_per_minute,
        "portfolio.attribution": settings.ai_rate_limit_portfolio_per_minute,
        "orders.preview": settings.ai_rate_limit_orders_per_minute,
        "briefing": settings.ai_rate_limit_briefing_per_minute,
    }[endpoint]


def current_usage() -> UsageCounter | None:
    return _current.get()


def reset_usage(token: Token[UsageCounter | None]) -> None:
    _current.reset(token)


async def reserve_gms() -> TokenReservation | None:
    counter = _current.get()
    if counter is None:
        return None
    guard = _guard_by_counter.get(id(counter))
    return await guard.reserve(settings.ai_gms_reservation_tokens) if guard else None


async def settle_gms(
    reservation: TokenReservation | None,
    *,
    input_tokens: int,
    output_tokens: int,
    cache_read_tokens: int = 0,
) -> None:
    counter = _current.get()
    guard = _guard_by_counter.get(id(counter)) if counter else None
    if guard:
        await guard.settle(
            reservation,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cache_read_tokens=cache_read_tokens,
        )


async def cancel_gms(reservation: TokenReservation | None) -> None:
    counter = _current.get()
    guard = _guard_by_counter.get(id(counter)) if counter else None
    if guard:
        await guard.cancel(reservation)


_guard_by_counter: dict[int, UsageGuard] = {}


def bind_guard(guard: UsageGuard, token: Token[UsageCounter | None]) -> None:
    counter = _current.get()
    if counter is not None:
        _guard_by_counter[id(counter)] = guard


def unbind_guard() -> None:
    counter = _current.get()
    if counter is not None:
        _guard_by_counter.pop(id(counter), None)


def usage_values() -> dict[str, int]:
    counter = _current.get()
    if counter is None:
        return {"input_tokens": 0, "output_tokens": 0, "cache_read_tokens": 0}
    return {
        "input_tokens": counter.input_tokens,
        "output_tokens": counter.output_tokens,
        "cache_read_tokens": counter.cache_read_tokens,
    }


async def spent_today(db: Any, user_id: str, day: date) -> int:
    """DB에 기록된 오늘 토큰. 테스트용 세션이나 장애에서는 0으로 시작한다."""
    from sqlalchemy import func, select

    from app.core.models import AIResponse

    if db is None:
        return 0
    start = datetime.combine(day, datetime.min.time(), tzinfo=KST)
    try:
        value = await db.scalar(
            select(
                func.coalesce(func.sum(AIResponse.input_tokens), 0)
                + func.coalesce(func.sum(AIResponse.output_tokens), 0)
            ).where(AIResponse.user_id == user_id, AIResponse.created_at >= start)
        )
    except Exception:
        log.exception("오늘 토큰 사용량을 읽지 못해 0부터 집계한다 · user=%s", user_id)
        return 0
    return int(value) if isinstance(value, (int, float)) else 0
