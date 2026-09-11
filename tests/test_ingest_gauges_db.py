"""부분 백필 게이지가 실제 PostgreSQL 에서 안티조인으로 도는지 검증한다.

`ingest_price_daily` 는 시세가 한 건만 들어와도 1이라 중단된 백필을 못 잡는다.
그 구멍을 메우는 게 `ingest_price_backfill_pending` 이므로, 상관 서브쿼리가 제대로
`instruments.ticker` 에 묶이는지는 진짜 DB 로만 확인할 수 있다.
"""

from __future__ import annotations

from datetime import date

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.api.main import _INGEST_PROBES
from app.core.config import settings
from app.core.enums import InstrumentStatus
from app.core.models import Instrument, PriceDaily

# 6자 제약만 지키면 되고 숫자일 필요는 없다. 실제 종목코드와 겹치지 않는 값을 쓴다.
TICKER = "ZZTEST"

test_engine = create_async_engine(settings.database_url, poolclass=NullPool)
TestSessions = async_sessionmaker(test_engine, expire_on_commit=False)


async def _clean() -> None:
    async with TestSessions() as db, db.begin():
        # price_daily 는 ON DELETE CASCADE 라 종목만 지우면 따라 지워진다.
        await db.execute(
            text("DELETE FROM instruments WHERE ticker=:ticker"), {"ticker": TICKER}
        )


@pytest.mark.asyncio
async def test_backfill_gauge_flags_listed_instrument_without_prices() -> None:
    stmt, _ = _INGEST_PROBES["price_backfill_pending"]
    # 개발 DB 에 남아 있는 다른 종목에 흔들리지 않게 이 종목으로만 좁힌다.
    # 안티조인 자체는 운영과 같은 문장 그대로다.
    scoped = stmt.where(Instrument.ticker == TICKER)

    await _clean()
    try:
        async with TestSessions() as db, db.begin():
            db.add(
                Instrument(
                    ticker=TICKER,
                    name="백필 점검용",
                    market="KOSPI",
                    status=InstrumentStatus.LISTED,
                )
            )

        async with TestSessions() as db:
            assert (await db.execute(scoped)).first() is not None, "시세 0건인데 못 잡았다"

        async with TestSessions() as db, db.begin():
            db.add(PriceDaily(ticker=TICKER, trade_date=date(2026, 9, 10), close=70_000))

        async with TestSessions() as db:
            assert (await db.execute(scoped)).first() is None, "시세가 생겼는데 계속 대기다"
    finally:
        await _clean()


@pytest.mark.asyncio
async def test_backfill_gauge_ignores_delisted_instruments() -> None:
    """상장폐지 종목은 시세를 받지 않는다. 세면 게이지가 영원히 1로 붙는다."""
    stmt, _ = _INGEST_PROBES["price_backfill_pending"]
    scoped = stmt.where(Instrument.ticker == TICKER)

    await _clean()
    try:
        async with TestSessions() as db, db.begin():
            db.add(
                Instrument(
                    ticker=TICKER,
                    name="상장폐지",
                    market="KOSPI",
                    status=InstrumentStatus.DELISTED,
                )
            )

        async with TestSessions() as db:
            assert (await db.execute(scoped)).first() is None
    finally:
        await _clean()
