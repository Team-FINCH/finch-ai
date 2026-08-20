"""백엔드 원장 어댑터. 백엔드 API 명세 §9.

원장은 백엔드, 시장 데이터(과거 종가·섹터)는 우리 DB 다. 백엔드가 구현 전이라
HTTP 는 목으로 세우고, DB 쪽은 실제로 읽는다 — 여기서 고정하는 것은 두 출처가
하나의 Ledger 로 합쳐지는 방식이다. 백엔드가 형식을 바꾸면 이 파일이 먼저 깨진다.
"""

from __future__ import annotations

from datetime import date
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.core.adapters import BackendLedgerSource, _common_days
from app.core.config import settings
from app.core.enums import OrderSide

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
    ledger = await _source(_FakeClient(), sessions).load("1")

    assert len(ledger.trading_days) >= settings.min_history_days
    last = ledger.trading_days[-1]
    assert ledger.price(TICKER, last) > 0


@pytest.mark.asyncio
async def test_섹터는_우리_instruments에서_온다(sessions):
    """DART 로 채운 값이다. §9 에는 섹터가 없다."""
    ledger = await _source(_FakeClient(), sessions).load("1")
    assert ledger.instrument(TICKER).sector != "미분류"


@pytest.mark.asyncio
async def test_시세가_없는_종목이면_거래일이_빈다(sessions):
    """엔진은 거래일마다 보유 종목 전부의 종가를 찾는다. 하나라도 없으면 KeyError 다.

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
async def test_입출금과_수수료는_지어내지_않는다(sessions):
    """§9 에 없고 백엔드만 아는 값이다. 채우면 조용히 틀린 숫자가 나온다."""
    pages = [{"trades": [TRADE], "nextCursor": None, "hasNext": False}]
    ledger = await _source(_FakeClient(pages), sessions).load("1")

    assert ledger.flows == ()
    assert ledger.trades[0].fee == 0.0
