"""종가 결측 시 as-of 조회 정책. `Ledger.price` (산식 §2.1 · §6.1).

휴장일·거래정지로 빠진 종가는 직전 종가로 메우되 상한을 둔다. 상한 밖이거나
그 종목 종가가 아예 없으면 0을 흘리지 않고 `InsufficientData`로 끊는다 —
금액이 0으로 섞이면 수익률이 조용히 틀린다.

정책은 `Ledger.price` 한 곳에만 있다. 호출부(§2의 평가·기여도 세 곳)에 흩어
두면 지금 걸리지 않은 호출부가 그대로 남기 때문이다.
"""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from app.core.adapters import CashFlow, Instrument, Ledger, Trade
from app.core.config import settings
from app.core.enums import OrderSide
from app.core.errors import ErrorCode, InsufficientData
from app.engines.portfolio import PortfolioEngine

TICKER = "005930"
START = date(2025, 9, 1)
LIMIT = settings.price_asof_max_days

# 산식 §6.3 항등식 3의 허용 오차
IDENTITY_TOL = 1e-9


def _ledger(
    prices: dict[str, dict[date, float]],
    *,
    trading_days: tuple[date, ...] = (),
    trades: tuple[Trade, ...] = (),
    flows: tuple[CashFlow, ...] = (),
) -> Ledger:
    return Ledger(
        user_id="tester",
        trading_days=trading_days,
        instruments={TICKER: Instrument(TICKER, "삼성전자", "반도체")},
        prices=prices,
        trades=trades,
        flows=flows,
    )


# ── as-of 조회 ────────────────────────────────────────────
def test_휴장일은_직전_거래일_종가로_메운다() -> None:
    """중간에 빠진 날은 정상이다. 보유는 그대로 남아 있어 평가금액에는 값이 있어야 한다."""
    ledger = _ledger({TICKER: {START: 70_000.0, START + timedelta(days=2): 72_000.0}})
    missing = START + timedelta(days=1)
    assert ledger.price(TICKER, missing) == 70_000.0
    # 실제 종가가 있는 날은 그대로다. as-of가 정상 조회를 건드리면 안 된다.
    assert ledger.price(TICKER, START + timedelta(days=2)) == 72_000.0


def test_as_of는_미래_종가를_보지_않는다() -> None:
    """앞을 보면 그날 알 수 없던 가격으로 평가하게 된다. 뒤로만 간다."""
    ledger = _ledger({TICKER: {START + timedelta(days=1): 70_000.0}})
    with pytest.raises(InsufficientData):
        ledger.price(TICKER, START)


@pytest.mark.parametrize(
    ("gap", "fills"),
    [(LIMIT, True), (LIMIT + 1, False)],
)
def test_상한_안이면_메우고_넘으면_실패한다(gap: int, fills: bool) -> None:
    """무한정 거슬러 올라가면 상장폐지·장기 거래정지 종목이 영원히 평가액을 갖는다."""
    ledger = _ledger({TICKER: {START: 70_000.0}})
    day = START + timedelta(days=gap)
    if fills:
        assert ledger.price(TICKER, day) == 70_000.0
        return
    with pytest.raises(InsufficientData) as caught:
        ledger.price(TICKER, day)
    # 500이 아니라 계약에 있는 409로 나가야 한다. 이게 이 티켓의 목적이다.
    assert caught.value.code is ErrorCode.INSUFFICIENT_DATA
    assert caught.value.status_code == 409


def test_종가가_하나도_없으면_실패한다() -> None:
    """상장 전 구간이거나 시세를 못 받은 종목. 0으로 메우면 수익률이 조용히 틀린다."""
    ledger = _ledger({TICKER: {}})
    with pytest.raises(InsufficientData) as caught:
        ledger.price(TICKER, START)
    assert caught.value.detail["symbol"] == TICKER

    # 원장에 키 자체가 없는 종목도 같은 실패로 다룬다.
    with pytest.raises(InsufficientData):
        _ledger({}).price(TICKER, START)


def test_상한은_설정으로_바꾼다(monkeypatch: pytest.MonkeyPatch) -> None:
    """며칠이 맞는지는 실측 전에는 모른다. 기본값은 코드에 두고 조정은 설정으로 한다."""
    ledger = _ledger({TICKER: {START: 70_000.0}})
    day = START + timedelta(days=LIMIT + 1)
    monkeypatch.setattr(settings, "price_asof_max_days", LIMIT + 1)
    assert ledger.price(TICKER, day) == 70_000.0


# ── 엔진 항등식 ───────────────────────────────────────────
def test_결측일이_있어도_기여도_항등식이_성립한다() -> None:
    """§6.3 항등식 3. `V_t`와 `r_i,t`가 같은 가격을 보므로 결측일 수익률은 0이다.

    결측일을 빼거나 0을 넣으면 이 항등식이 깨진다. as-of 보정을 고른 이유가 이것이다.
    """
    days = tuple(START + timedelta(days=i) for i in range(4))
    gap = days[2]
    ledger = _ledger(
        {TICKER: {days[0]: 70_000.0, days[1]: 71_000.0, days[3]: 74_000.0}},
        trading_days=days,
        trades=(Trade(days[0], TICKER, OrderSide.BUY, 10.0, 70_000.0),),
        flows=(CashFlow(days[0], 1_000_000.0),),
    )

    rows = PortfolioEngine(ledger).daily_returns()
    assert [row.trade_date for row in rows] == list(days[1:])
    for row in rows:
        contributed = sum(c.contribution for c in row.contributions)
        assert abs(contributed - row.portfolio_return) < IDENTITY_TOL, (
            f"{row.trade_date}: Σc={contributed!r} r_p={row.portfolio_return!r}"
        )

    (missing_day,) = [row for row in rows if row.trade_date == gap]
    assert missing_day.portfolio_return == pytest.approx(0.0, abs=IDENTITY_TOL)
