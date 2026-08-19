"""수집기는 순수 함수라 DB 없이 원장만 조립하면 끝난다."""

from __future__ import annotations

from datetime import date

import pytest

from app.core.adapters import Instrument, Ledger, Trade
from app.core.enums import OrderSide
from app.wiki.collector import poll, trade_id

SAMSUNG = "005930"
HYNIX = "000660"


def _ledger(*trades: Trade) -> Ledger:
    days = tuple(sorted({t.trade_date for t in trades})) or (date(2025, 9, 1),)
    return Ledger(
        user_id="u1",
        trading_days=days,
        instruments={
            SAMSUNG: Instrument(SAMSUNG, "삼성전자", "반도체"),
            HYNIX: Instrument(HYNIX, "SK하이닉스", "반도체"),
        },
        prices={},
        trades=trades,
    )


def _buy(day: str, symbol: str = SAMSUNG, qty: float = 10) -> Trade:
    return Trade(date.fromisoformat(day), symbol, OrderSide.BUY, qty, 70000.0)


def _sell(day: str, symbol: str = SAMSUNG, qty: float = 10) -> Trade:
    return Trade(date.fromisoformat(day), symbol, OrderSide.SELL, qty, 70000.0)


def test_워터마크가_없으면_모든_매수가_신규다() -> None:
    result = poll(_ledger(_buy("2025-09-01"), _buy("2025-09-02", HYNIX)))

    assert [b.trade.symbol for b in result.buys] == [SAMSUNG, HYNIX]
    assert result.watermark == "2025-09-02#0001"


def test_워터마크_이후의_매수만_돌려준다() -> None:
    ledger = _ledger(_buy("2025-09-01"), _buy("2025-09-02", HYNIX))

    result = poll(ledger, last_trade_id="2025-09-01#0000")

    assert [b.trade.symbol for b in result.buys] == [HYNIX]


def test_새로_본_거래가_없으면_워터마크가_그대로다() -> None:
    ledger = _ledger(_buy("2025-09-01"))

    result = poll(ledger, last_trade_id="2025-09-01#0000")

    assert result.buys == ()
    assert result.watermark == "2025-09-01#0000"


def test_매도는_질문거리가_아니지만_워터마크는_민다() -> None:
    """매도를 워터마크에 반영하지 않으면 다음 폴링이 같은 매도를 계속 다시 훑는다."""
    ledger = _ledger(_buy("2025-09-01"), _sell("2025-09-03"))

    result = poll(ledger, last_trade_id="2025-09-01#0000")

    assert result.buys == ()
    assert result.watermark == "2025-09-03#0001"


def test_추가_매수는_신규_편입과_다르게_묻는다() -> None:
    ledger = _ledger(_buy("2025-09-01"), _buy("2025-09-05"))

    first, second = poll(ledger).buys

    assert first.first_position is True
    assert "새로 담으셨네요" in first.question
    assert second.first_position is False
    assert "추가로 사셨네요" in second.question


def test_전량_매도_후_재매수는_다시_신규_편입이다() -> None:
    ledger = _ledger(_buy("2025-09-01"), _sell("2025-09-02"), _buy("2025-09-03"))

    last = poll(ledger).buys[-1]

    assert last.first_position is True


def test_보유_판정은_워터마크_이전_거래까지_본다() -> None:
    """새로 본 거래만으로 판단하면 물타기를 신규 편입으로 오인한다."""
    ledger = _ledger(_buy("2025-09-01"), _buy("2025-09-05"))

    result = poll(ledger, last_trade_id="2025-09-01#0000")

    assert result.buys[0].first_position is False


def test_질문에_종목명이_들어간다() -> None:
    question = poll(_ledger(_buy("2025-09-01"))).buys[0].question

    assert "삼성전자" in question
    assert SAMSUNG in question


def test_원장이_비면_워터마크를_건드리지_않는다() -> None:
    result = poll(_ledger(), last_trade_id="2025-09-01#0000")

    assert result.buys == ()
    assert result.watermark == "2025-09-01#0000"


@pytest.mark.parametrize(
    ("earlier", "later"),
    [
        (("2025-09-01", 0), ("2025-09-02", 1)),
        (("2025-09-01", 9), ("2025-09-01", 10)),  # 0으로 채우지 않으면 여기서 뒤집힌다
    ],
)
def test_거래id는_사전순이_시간순과_같다(earlier: tuple[str, int], later: tuple[str, int]) -> None:
    assert trade_id(_buy(earlier[0]), earlier[1]) < trade_id(_buy(later[0]), later[1])
