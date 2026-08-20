"""백엔드 원장 어댑터. 백엔드 API 명세 §9.

백엔드가 아직 구현 전이라 실제 호출은 목으로 세운다. 여기서 고정하는 것은
"백엔드가 §9 대로 응답하면 우리 Ledger 가 이렇게 채워진다"는 매핑이다.
백엔드가 형식을 바꾸면 이 파일이 먼저 깨져야 한다.
"""

from __future__ import annotations

from datetime import date
from typing import Any

import pytest

from app.core.adapters import BackendLedgerSource
from app.core.config import settings
from app.core.enums import OrderSide

PORTFOLIO = {
    "roundId": 3,
    "cashBalance": 1_250_000,
    "asOf": "2026-08-20T14:30:00+09:00",
    "holdings": [
        {
            "stockCode": "005930",
            "stockName": "삼성전자",
            "quantity": 10,
            "avgBuyPrice": 71_200,
            "currentPrice": 73_500,
        }
    ],
}

TRADE = {
    "tradeId": 101,
    "stockCode": "005930",
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


def _source(client: _FakeClient) -> BackendLedgerSource:
    return BackendLedgerSource("http://backend:8080/", "s3cret", client=client)


def test_보유_종목이_Ledger로_옮겨진다():
    client = _FakeClient()
    ledger = _source(client).load("1")

    assert ledger.user_id == "1"
    assert ledger.instrument("005930").name == "삼성전자"
    assert ledger.price("005930", date(2026, 8, 20)) == 73_500.0


def test_거래가_Ledger로_옮겨진다():
    pages = [{"trades": [TRADE], "nextCursor": None, "hasNext": False}]
    ledger = _source(_FakeClient(pages)).load("1")

    assert len(ledger.trades) == 1
    trade = ledger.trades[0]
    assert trade.trade_date == date(2026, 8, 18)
    assert trade.side is OrderSide.BUY  # 백엔드는 "BUY", 우리는 "buy"
    assert trade.quantity == 10.0
    assert trade.price == 71_200.0


def test_거래일은_체결일과_스냅샷_기준일에서_뽑는다():
    pages = [{"trades": [TRADE], "nextCursor": None, "hasNext": False}]
    ledger = _source(_FakeClient(pages)).load("1")
    assert ledger.trading_days == (date(2026, 8, 18), date(2026, 8, 20))


def test_커서를_끝까지_따라간다():
    pages = [
        {"trades": [TRADE], "nextCursor": "c2", "hasNext": True},
        {"trades": [TRADE | {"tradeId": 102}], "nextCursor": None, "hasNext": False},
    ]
    client = _FakeClient(pages)
    ledger = _source(client).load("1")

    assert len(ledger.trades) == 2
    trade_calls = [c for c in client.calls if c[0].endswith("/trades")]
    assert len(trade_calls) == 2
    assert trade_calls[1][1]["cursor"] == "c2"
    assert trade_calls[0][1]["size"] == 100
    assert trade_calls[0][1]["roundId"] == 3


def test_커서_없이_hasNext만_참이면_멈춘다():
    """같은 쪽을 영원히 다시 받는 것을 막는다."""
    pages = [{"trades": [TRADE], "nextCursor": None, "hasNext": True}]
    client = _FakeClient(pages)
    assert len(_source(client).load("1").trades) == 1


def test_모든_요청에_인증_헤더_두_개가_실린다():
    client = _FakeClient()
    _source(client).load("42")

    assert client.calls
    for _url, _params, headers in client.calls:
        assert headers[settings.internal_token_header] == "s3cret"
        assert headers[settings.trusted_user_header] == "42"


def test_없는_값을_지어내지_않는다():
    """§9 에 섹터·입출금·시계열이 없다. 조용히 채우면 틀린 숫자가 나온다."""
    ledger = _source(_FakeClient()).load("1")

    assert ledger.flows == ()
    assert ledger.instrument("005930").sector == "미분류"
    # 시세는 스냅샷 하루치뿐이라 60거래일이 필요한 지표는 INSUFFICIENT_DATA 로 떨어진다.
    assert list(ledger.prices["005930"]) == [date(2026, 8, 20)]

    with pytest.raises(KeyError):
        ledger.price("005930", date(2026, 8, 19))
