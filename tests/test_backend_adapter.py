"""백엔드 원장 어댑터. 백엔드 API 명세 §9.

원장은 백엔드, 시장 데이터(과거 종가·섹터)는 우리 DB 다. 백엔드가 구현 전이라
HTTP 는 목으로 세우고, DB 쪽은 실제로 읽는다 — 여기서 고정하는 것은 두 출처가
하나의 Ledger 로 합쳐지는 방식이다. 백엔드가 형식을 바꾸면 이 파일이 먼저 깨진다.
"""

from __future__ import annotations

from datetime import date
from typing import Any

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.core.adapters import (
    BackendLedgerSource,
    SeedLedgerSource,
    _common_days,
    ledger_source,
)
from app.core.config import settings
from app.core.enums import OrderSide
from app.core.models import PriceDaily

#: 시드 시세가 들어 있는 종목. price_daily 에 60거래일 이상 있다.
TICKER = "005930"

PORTFOLIO = {
    "roundId": 3,
    "cashBalance": 1_250_000,
    "asOf": "2026-08-20T14:30:00+09:00",
    "holdings": [
        {
            "stockCode": TICKER,
            "stockName": "삼성전자",
            "quantity": 10,
            "avgBuyPrice": 71_200,
            "currentPrice": 73_500,
        }
    ],
}

TRADE = {
    "tradeId": 101,
    "stockCode": TICKER,
    "side": "BUY",
    "price": 71_200,
    "quantity": 10,
    "executedAt": "2026-08-18T10:12:44+09:00",
}


class _Response:
    def __init__(self, payload: dict) -> None:
        self._payload = payload

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict:
        return self._payload


class _FakeClient:
    """경로별로 정해진 응답을 돌려주고 요청을 기록한다."""

    def __init__(self, pages: list[dict] | None = None) -> None:
        self.calls: list[tuple[str, dict | None, dict]] = []
        self._pages = pages or [{"trades": [], "nextCursor": None, "hasNext": False}]
        self._served = 0

    def get(self, url: str, params: Any = None, headers: Any = None) -> _Response:
        self.calls.append((url, params, headers))
        if url.endswith("/portfolio"):
            return _Response(PORTFOLIO)
        page = self._pages[min(self._served, len(self._pages) - 1)]
        self._served += 1
        return _Response(page)


@pytest.fixture
async def sessions():
    """전역 엔진의 풀은 처음 열린 루프에 묶인다. 루프가 바뀌는 테스트에서는 못 쓴다.

    test_wiki_store 와 같은 방식이다 — NullPool 로 새로 열고 끝나면 버린다.
    """
    engine = create_async_engine(settings.database_url, poolclass=NullPool)
    yield async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    await engine.dispose()


def _source(client: _FakeClient, sessions: Any = None) -> BackendLedgerSource:
    return BackendLedgerSource(
        "http://backend:8080/", "s3cret", client=client, sessions=sessions
    )


async def _skip_without_prices(sessions: Any) -> None:
    async with sessions() as session:
        count = await session.scalar(select(func.count()).select_from(PriceDaily))
    if not count:
        pytest.skip("price_daily 적재가 필요한 통합 테스트")


# ── 어댑터 선택 ──────────────────────────────────────────────────────────
@pytest.mark.parametrize(
    ("mode", "expected"),
    [("seed", SeedLedgerSource), ("backend", BackendLedgerSource), ("none", type(None))],
)
def test_설정이_어댑터를_고른다(monkeypatch, mode, expected):
    """선택은 ledger_source() 한 곳에서만 한다. 호출부는 None 인지만 본다."""
    monkeypatch.setattr(settings, "ledger_source", mode)
    ledger_source.cache_clear()
    try:
        assert type(ledger_source()) is expected
    finally:
        ledger_source.cache_clear()


def test_설정에_없는_값은_기동_때_걸린다():
    """Literal 이라 오타가 런타임까지 못 간다. 불리언 하나로는 세 상태를 못 담는다."""
    from pydantic import ValidationError

    from app.core.config import Settings

    with pytest.raises(ValidationError):
        Settings(ledger_source="seedd")


# ── 백엔드에서 오는 것 ────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_보유_종목이_Ledger로_옮겨진다(sessions):
    ledger = await _source(_FakeClient(), sessions).load("1")

    assert ledger.user_id == "1"
    assert ledger.instrument(TICKER).name == "삼성전자"


@pytest.mark.asyncio
async def test_거래가_Ledger로_옮겨진다(sessions):
    pages = [{"trades": [TRADE], "nextCursor": None, "hasNext": False}]
    ledger = await _source(_FakeClient(pages), sessions).load("1")

    assert len(ledger.trades) == 1
    trade = ledger.trades[0]
    assert trade.trade_date == date(2026, 8, 18)
    assert trade.side is OrderSide.BUY  # 백엔드는 "BUY", 우리는 "buy"
    assert trade.quantity == 10.0
    assert trade.price == 71_200.0


@pytest.mark.asyncio
async def test_커서를_끝까지_따라간다(sessions):
    pages = [
        {"trades": [TRADE], "nextCursor": "c2", "hasNext": True},
        {"trades": [TRADE | {"tradeId": 102}], "nextCursor": None, "hasNext": False},
    ]
    client = _FakeClient(pages)
    ledger = await _source(client, sessions).load("1")

    assert len(ledger.trades) == 2
    trade_calls = [c for c in client.calls if c[0].endswith("/trades")]
    assert len(trade_calls) == 2
    assert trade_calls[1][1]["cursor"] == "c2"
    assert trade_calls[0][1]["size"] == 100
    assert trade_calls[0][1]["roundId"] == 3


@pytest.mark.asyncio
async def test_커서_없이_hasNext만_참이면_멈춘다(sessions):
    """같은 쪽을 영원히 다시 받는 것을 막는다."""
    pages = [{"trades": [TRADE], "nextCursor": None, "hasNext": True}]
    ledger = await _source(_FakeClient(pages), sessions).load("1")
    assert len(ledger.trades) == 1


@pytest.mark.asyncio
async def test_모든_요청에_인증_헤더_두_개가_실린다(sessions):
    client = _FakeClient()
    await _source(client, sessions).load("42")

    assert client.calls
    for _url, _params, headers in client.calls:
        assert headers[settings.internal_token_header] == "s3cret"
        assert headers[settings.trusted_user_header] == "42"


# ── 우리 DB 에서 오는 것 ──────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_시계열은_우리_price_daily에서_온다(sessions):
    """§9 의 currentPrice 한 점이 아니라 우리가 적재한 종가 시계열이다.

    위험 지표가 60거래일을 요구하므로 이게 비면 진단이 통째로 비활성된다.
    """
    await _skip_without_prices(sessions)
    ledger = await _source(_FakeClient(), sessions).load("1")

    assert len(ledger.trading_days) >= settings.min_history_days
    last = ledger.trading_days[-1]
    assert ledger.price(TICKER, last) > 0


@pytest.mark.asyncio
async def test_섹터는_우리_instruments에서_온다(sessions):
    """DART 로 채운 값이다. §9 에는 섹터가 없다."""
    await _skip_without_prices(sessions)
    ledger = await _source(_FakeClient(), sessions).load("1")
    assert ledger.instrument(TICKER).sector != "미분류"


@pytest.mark.asyncio
async def test_시세가_없는_종목이면_거래일이_빈다(sessions):
    """엔진은 거래일마다 보유 종목 전부의 종가를 찾는다. 하나도 없으면 InsufficientData 다.

    비는 편이 낫다 — 호출부가 이미 그것을 데이터 부족으로 다룬다.
    """
    portfolio = PORTFOLIO | {
        "holdings": [PORTFOLIO["holdings"][0] | {"stockCode": "999999"}]
    }

    class _NoPrice(_FakeClient):
        def get(self, url: str, params: Any = None, headers: Any = None) -> _Response:
            self.calls.append((url, params, headers))
            if url.endswith("/portfolio"):
                return _Response(portfolio)
            return _Response({"trades": [], "nextCursor": None, "hasNext": False})

    ledger = await _source(_NoPrice(), sessions).load("1")
    assert ledger.trading_days == ()


def test_거래일은_모든_종목의_교집합이다():
    a, b, c = date(2026, 8, 18), date(2026, 8, 19), date(2026, 8, 20)
    prices = {"A": {a: 1.0, b: 2.0, c: 3.0}, "B": {b: 4.0, c: 5.0}}
    assert _common_days(prices) == (b, c)
    # 한 종목이 통째로 비면 교집합을 낼 수 없다.
    assert _common_days({"A": {a: 1.0}, "B": {}}) == ()
    assert _common_days({}) == ()


# ── 없는 것 ──────────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_현재_현금에서_기초현금을_복원하고_수수료는_지어내지_않는다(monkeypatch):
    async def market_data(*_args, **_kwargs):
        return {TICKER: {date(2026, 8, 18): 73_500.0}}, {TICKER: "반도체"}

    monkeypatch.setattr("app.core.adapters._market_data", market_data)
    excluded = TRADE | {
        "tradeId": 102,
        "executedAt": "2026-08-17T10:12:44+09:00",
        "quantity": 1,
        "price": 100,
    }
    pages = [{"trades": [TRADE, excluded], "nextCursor": None, "hasNext": False}]
    ledger = await _source(_FakeClient(pages)).load("1")

    # 엔진이 재생하지 않는 8월 17일 거래는 기초현금 역산에서도 제외한다.
    assert len(ledger.trades) == 2
    assert ledger.flows[0].amount == 1_250_000 + 712_000
    assert ledger.trades[0].fee == 0.0


@pytest.mark.asyncio
async def test_백엔드가_죽으면_OSError로_나간다(sessions):
    """호출부는 (KeyError, FileNotFoundError, OSError) 로 잡는다.

    httpx 예외가 그대로 새면 500 이 나가고, 개인화 섹션만 비우는 정상 경로를
    타지 못한다. 호출부가 httpx 를 알아야 할 이유도 없다.
    """
    import httpx

    class _Down:
        def get(self, *_: Any, **__: Any):
            raise httpx.ConnectError("연결 실패")

    with pytest.raises(OSError):
        await _source(_Down(), sessions).load("1")
