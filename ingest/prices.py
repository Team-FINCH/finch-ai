"""일별 시세 적재.

변동성·상관관계·최대낙폭 계산의 기반이다. 엔진 산식 §1, §3.2, §3.3 참조.

사람이 앉아 있을 필요가 없는 기계 시간 작업이므로, 다른 트랙을 진행하는 동안
백그라운드로 돌려두는 것을 전제로 만들었다. 중단되어도 재실행하면 이어서 받는다.

    python -m ingest.prices --days 120
    python -m ingest.prices --tickers 005930,000660
    python -m ingest.prices --days 120 --limit 30
"""

from __future__ import annotations

import argparse
import asyncio
import logging
from datetime import date, datetime, timedelta

import pandas as pd
from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.db import SessionFactory, engine
from app.core.models import Instrument, PriceDaily

logger = logging.getLogger("ingest.prices")

# pykrx는 KRX를 직접 호출한다. 과하게 두드리면 차단되므로 간격을 둔다.
REQUEST_DELAY_S = 0.2

# pykrx 한글 컬럼 → 모델 컬럼
COLUMN_MAP = {
    "시가": "open",
    "고가": "high",
    "저가": "low",
    "종가": "close",
    "거래량": "volume",
}


def _yyyymmdd(d: date) -> str:
    return d.strftime("%Y%m%d")


def _fetch_ohlcv(ticker: str, start: date, end: date) -> pd.DataFrame:
    """pykrx 호출. 블로킹이므로 호출부에서 스레드로 넘긴다.

    adjusted=True가 중요하다. 수정주가가 아니면 액면분할·유상증자 시점에
    가짜 급등락이 생겨 변동성이 과대 추정된다.
    """
    from pykrx import stock

    return stock.get_market_ohlcv_by_date(
        _yyyymmdd(start), _yyyymmdd(end), ticker, adjusted=True
    )


def _opt_int(value: object) -> int | None:
    """결측을 None으로. 루프 안에서 클로저로 만들면 늦은 바인딩 문제가 생긴다."""
    return None if value is None or pd.isna(value) else int(value)


def _to_rows(ticker: str, df: pd.DataFrame) -> list[dict]:
    """DataFrame을 upsert 행으로 변환한다.

    거래정지일은 제외한다. 종가 0을 그대로 넣으면 수익률이 -100%로 계산되고,
    직전가로 채우면 0 수익률이 들어가 변동성이 과소 추정된다.
    엔진 산식 §6.1의 "거래정지 구간은 해당 날짜를 제외한다"를 여기서 지킨다.
    """
    if df is None or df.empty:
        return []

    df = df.rename(columns=COLUMN_MAP)
    rows: list[dict] = []
    for idx, r in df.iterrows():
        close = r.get("close")
        if pd.isna(close) or int(close) <= 0:
            continue
        trade_date = idx.date() if isinstance(idx, (pd.Timestamp, datetime)) else idx

        rows.append(
            {
                "ticker": ticker,
                "trade_date": trade_date,
                "open": _opt_int(r.get("open")),
                "high": _opt_int(r.get("high")),
                "low": _opt_int(r.get("low")),
                "close": int(close),
                "volume": _opt_int(r.get("volume")),
                # pykrx의 이 호출은 거래대금을 주지 않는다. 필요해지면 별도 조회를 붙인다.
                "trade_value": None,
            }
        )
    return rows


async def _target_tickers(
    session: AsyncSession, explicit: list[str] | None, limit: int | None
) -> list[str]:
    """적재 대상을 고른다.

    price_daily.ticker는 instruments를 참조하므로 마스터에 없는 종목은 넣을 수 없다.
    제약을 완화하는 대신 여기서 걸러낸다. 마스터에 없는 종목의 시세는 섹터도
    corp_code도 없어 어차피 해석할 수 없다.
    """
    if explicit:
        known = set(
            (
                await session.scalars(
                    select(Instrument.ticker).where(Instrument.ticker.in_(explicit))
                )
            ).all()
        )
        unknown = [t for t in explicit if t not in known]
        if unknown:
            logger.warning(
                "마스터에 없어 건너뜀: %s — 먼저 python -m ingest.instruments 를 실행할 것",
                ", ".join(unknown),
            )
        return [t for t in explicit if t in known]

    stmt = select(Instrument.ticker).where(Instrument.status == "listed")
    # 시가총액 큰 순으로 받는다. 중간에 끊겨도 비중 큰 종목이 먼저 확보된다.
    stmt = stmt.order_by(Instrument.market_cap.desc().nullslast())
    if limit:
        stmt = stmt.limit(limit)
    return list((await session.scalars(stmt)).all())


async def _last_date(session: AsyncSession, ticker: str) -> date | None:
    return await session.scalar(
        select(func.max(PriceDaily.trade_date)).where(PriceDaily.ticker == ticker)
    )


async def _upsert(session: AsyncSession, rows: list[dict]) -> int:
    if not rows:
        return 0
    stmt = pg_insert(PriceDaily).values(rows)
    stmt = stmt.on_conflict_do_update(
        index_elements=["ticker", "trade_date"],
        set_={
            "open": stmt.excluded.open,
            "high": stmt.excluded.high,
            "low": stmt.excluded.low,
            "close": stmt.excluded.close,
            "volume": stmt.excluded.volume,
            "trade_value": stmt.excluded.trade_value,
        },
    )
    await session.execute(stmt)
    return len(rows)


async def ingest(
    *,
    days: int,
    tickers: list[str] | None = None,
    limit: int | None = None,
    full: bool = False,
) -> dict[str, int]:
    """시세를 적재하고 요약을 돌려준다.

    full=False면 종목별 마지막 적재일 다음날부터만 받는다. 재실행 비용이 낮아
    cron으로 매일 돌려도 된다.
    """
    end = date.today()
    start = end - timedelta(days=days)
    stats = {"tickers": 0, "rows": 0, "skipped": 0, "failed": 0}

    async with SessionFactory() as session:
        targets = await _target_tickers(session, tickers, limit)
        if not targets:
            logger.warning(
                "대상 종목이 없다. 종목 마스터를 먼저 적재할 것: python -m ingest.instruments"
            )
            return stats

        logger.info("대상 %d종목 · %s ~ %s", len(targets), start, end)

        for i, ticker in enumerate(targets, 1):
            fetch_start = start
            if not full:
                last = await _last_date(session, ticker)
                if last is not None:
                    if last >= end:
                        stats["skipped"] += 1
                        continue
                    fetch_start = max(start, last + timedelta(days=1))

            try:
                df = await asyncio.to_thread(_fetch_ohlcv, ticker, fetch_start, end)
                rows = _to_rows(ticker, df)
                n = await _upsert(session, rows)
                await session.commit()
                stats["tickers"] += 1
                stats["rows"] += n
            except Exception:
                # 한 종목이 실패해도 전체를 멈추지 않는다. 재실행하면 이어서 받는다.
                await session.rollback()
                stats["failed"] += 1
                logger.exception("적재 실패: %s", ticker)

            if i % 50 == 0:
                logger.info(
                    "진행 %d/%d · 누적 %d행", i, len(targets), stats["rows"]
                )
            await asyncio.sleep(REQUEST_DELAY_S)

    return stats


async def _main() -> None:
    parser = argparse.ArgumentParser(description="일별 시세 적재")
    parser.add_argument(
        "--days",
        type=int,
        default=settings.min_history_days * 2,
        help="오늘 기준 며칠 전부터 받을지 (기본: 최소 요건의 2배)",
    )
    parser.add_argument("--tickers", help="쉼표 구분 종목코드. 생략 시 마스터 전체")
    parser.add_argument("--limit", type=int, help="시가총액 상위 N종목만")
    parser.add_argument(
        "--full", action="store_true", help="증분 무시하고 구간 전체 재적재"
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)-5s %(message)s"
    )

    tickers = (
        [t.strip() for t in args.tickers.split(",") if t.strip()]
        if args.tickers
        else None
    )
    try:
        stats = await ingest(
            days=args.days, tickers=tickers, limit=args.limit, full=args.full
        )
        logger.info(
            "완료 — 적재 %d종목 / %d행 · 건너뜀 %d · 실패 %d",
            stats["tickers"],
            stats["rows"],
            stats["skipped"],
            stats["failed"],
        )
    finally:
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(_main())
