"""개발용 백엔드 목업과 실제 원장 어댑터의 연결 테스트."""

from __future__ import annotations

import re
from collections import defaultdict
from datetime import date
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.core.adapters import BackendLedgerSource
from app.core.config import settings
from app.core.models import PriceDaily
from app.engines.portfolio import PortfolioEngine
from scripts.mock_backend import SCENARIOS, VALID_TRADING_DAYS, app

ROW = re.compile(r"^\| (\d{6}) \| (.+?) \| (\w+) \| (.+?) \|$", re.MULTILINE)


@pytest.fixture
async def sessions():
    engine = create_async_engine(settings.database_url, poolclass=NullPool)
    yield async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    await engine.dispose()


def test_목업_티커는_시드_30종_안에_있다() -> None:
    document = (Path(__file__).parents[1] / "docs/seed-dataset.md").read_text(encoding="utf-8")
    seed_tickers = {match[0] for match in ROW.findall(document)}
    mock_tickers = {
        row["stockCode"]
        for scenario in SCENARIOS.values()
        for key in ("trades", "holdings")
        for row in scenario[key]
    }
    assert mock_tickers <= seed_tickers


def test_모든_체결일은_실제_거래일이다() -> None:
    days = {
        date.fromisoformat(row["executedAt"][:10]).isoformat()
        for scenario in SCENARIOS.values()
        for row in scenario["trades"]
    }
    assert days <= VALID_TRADING_DAYS


def test_거래를_재생하면_현재_보유수량과_같다() -> None:
    for scenario in SCENARIOS.values():
        quantities: defaultdict[str, float] = defaultdict(float)
        for trade in scenario["trades"]:
            sign = 1 if trade["side"] == "BUY" else -1
            quantities[trade["stockCode"]] += sign * trade["quantity"]
        expected = {row["stockCode"]: row["quantity"] for row in scenario["holdings"]}
        assert dict(quantities) == expected


def test_두_라우트가_백엔드_계약_모양을_반환한다() -> None:
    with TestClient(app) as client:
        portfolio = client.get("/internal/v1/portfolio", headers={"X-User-Id": "1"}).json()
        trades = client.get("/internal/v1/trades", headers={"X-User-Id": "1"}).json()

    assert set(portfolio) == {"roundId", "cashBalance", "asOf", "holdings"}
    assert set(portfolio["holdings"][0]) == {
        "stockCode",
        "stockName",
        "quantity",
        "avgBuyPrice",
        "currentPrice",
    }
    assert set(trades) == {"roundId", "trades", "nextCursor", "hasNext"}
    assert set(trades["trades"][0]) == {
        "tradeId",
        "stockCode",
        "side",
        "quantity",
        "price",
        "executedAt",
    }
    assert {row["side"] for row in trades["trades"]} <= {"BUY", "SELL"}


@pytest.mark.asyncio
async def test_실제_DB와_어댑터로_개인화된_원장을_계산한다(sessions: Any) -> None:
    async with sessions() as session:
        count = await session.scalar(select(func.count()).select_from(PriceDaily))
    if not count:
        pytest.skip("price_daily 적재가 필요한 통합 테스트")

    with TestClient(app) as client:
        source = BackendLedgerSource("http://testserver", "tok", client=client, sessions=sessions)
        ledger = await source.load("1")
        other = await source.load("2")
        declared_cash = client.get(
            "/internal/v1/portfolio", headers={"X-User-Id": "1"}
        ).json()["cashBalance"]

    assert len(ledger.trading_days) >= settings.min_history_days
    assert len(ledger.trades) == 6
    tickers = {holding["stockCode"] for holding in SCENARIOS["1"]["holdings"]}
    assert all(ledger.instrument(ticker).sector != "미분류" for ticker in tickers)
    snapshot = PortfolioEngine(ledger).snapshot(ledger.trading_days[-1])
    assert len({holding.sector for holding in snapshot.holdings}) == 5
    assert {holding.symbol for holding in snapshot.holdings if holding.return_rate < 0} == {
        "035420",
        "373220",
    }
    assert snapshot.cash == declared_cash
    other_snapshot = PortfolioEngine(other).snapshot(other.trading_days[-1])
    assert other_snapshot.concentration().hhi != snapshot.concentration().hhi
