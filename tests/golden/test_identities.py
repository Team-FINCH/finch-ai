"""검증 항등식 회귀 테스트. 엔진 산식 §6.3.

항등식 1·2·3을 문서 그대로 옮겼다. 이 셋은 어떤 포트폴리오에서도,
어떤 거래일에서도 성립해야 한다 — 그래서 픽스처 전체 × 거래일 전체로 돌린다.
"""

from __future__ import annotations

from datetime import date

import pytest

from app.core.adapters import Ledger
from app.engines.portfolio import PortfolioEngine
from tests.golden.conftest import ALL_PORTFOLIOS, IDENTITY_TOL

# 수수료가 없는 포트폴리오. 항등식 3이 오차 없이 정확히 성립한다(아래 잔차 테스트 참조).
FEE_FREE_PORTFOLIOS = ("golden_1_single", "golden_4_deposit", "mixed_with_cash")


def _days(ledger: Ledger) -> list[date]:
    return list(ledger.trading_days)


# ── 항등식 1: Σ w_i + w_c = 1 ────────────────────────────
@pytest.mark.parametrize("user_id", ALL_PORTFOLIOS)
def test_identity_1_weights_sum_to_one(ledgers: dict[str, Ledger], user_id: str) -> None:
    """비중의 분모에는 현금이 들어간다. 그래서 종목 비중과 현금 비중의 합이 정확히 1이다."""
    ledger = ledgers[user_id]
    engine = PortfolioEngine(ledger)
    for day in _days(ledger):
        snapshot = engine.snapshot(day)
        total = sum(h.weight for h in snapshot.holdings) + snapshot.cash_weight
        assert abs(total - 1.0) < IDENTITY_TOL, f"{user_id} {day}: Σw={total!r}"


# ── 항등식 2: Σ ŵ_i = 1 ──────────────────────────────────
@pytest.mark.parametrize("user_id", ALL_PORTFOLIOS)
def test_identity_2_stock_weights_sum_to_one(
    ledgers: dict[str, Ledger], user_id: str
) -> None:
    """집중도는 현금을 빼고 재정규화한 비중으로 계산한다. 그 합은 1이다."""
    ledger = ledgers[user_id]
    engine = PortfolioEngine(ledger)
    checked = 0
    for day in _days(ledger):
        snapshot = engine.snapshot(day)
        if not snapshot.holdings:
            continue  # 전량 매도 직후. 재정규화할 주식이 없으므로 항등식 대상이 아니다.
        total = sum(h.stock_weight for h in snapshot.holdings)
        assert abs(total - 1.0) < IDENTITY_TOL, f"{user_id} {day}: Σŵ={total!r}"
        checked += 1
    assert checked, f"{user_id}: 검증한 날이 없다"


# ── 항등식 3: Σ c_i,t = r_p,t ────────────────────────────
@pytest.mark.parametrize("user_id", FEE_FREE_PORTFOLIOS)
def test_identity_3_contributions_sum_to_daily_return(
    ledgers: dict[str, Ledger], user_id: str
) -> None:
    """일별 종목 기여도의 합은 일별 포트폴리오 수익률과 같다.

    기여도의 비중은 전일 종가 기준(w_{i,t-1})이다. 당일 비중을 쓰면 이 항등식이 깨진다(§4.1).
    """
    engine = PortfolioEngine(ledgers[user_id])
    daily = engine.daily_returns()
    assert daily, f"{user_id}: 일별 수익률이 비어 있다"
    for row in daily:
        contributed = sum(c.contribution for c in row.contributions)
        assert abs(contributed - row.portfolio_return) < IDENTITY_TOL, (
            f"{user_id} {row.trade_date}: Σc={contributed!r} r_p={row.portfolio_return!r}"
        )


@pytest.mark.parametrize("user_id", ALL_PORTFOLIOS)
def test_identity_3_residual_equals_fee_drag(
    ledgers: dict[str, Ledger], user_id: str
) -> None:
    """수수료가 있으면 잔차가 정확히 fee_t / V_{t-1} 만큼 생긴다.

    기여도는 가격 변동만 설명하는 값이고, 수수료는 가격 변동이 아니라 자산에서
    빠져나간 비용이기 때문이다. 잔차를 뭉개지 않고 크기까지 못박아 둔다.
    """
    ledger = ledgers[user_id]
    engine = PortfolioEngine(ledger)
    days = _days(ledger)
    for row in engine.daily_returns():
        previous = days[days.index(row.trade_date) - 1]
        prev_value = engine.snapshot(previous).total_value
        fee = sum(t.fee for t in ledger.trades_on(row.trade_date))
        contributed = sum(c.contribution for c in row.contributions)
        expected = fee / prev_value
        assert abs((contributed - row.portfolio_return) - expected) < IDENTITY_TOL, (
            f"{user_id} {row.trade_date}: 잔차가 수수료로 설명되지 않는다"
        )


# ── 항등식 6: 1/N ≤ HHI ≤ 1 ─────────────────────────────
@pytest.mark.parametrize("user_id", ALL_PORTFOLIOS)
def test_identity_6_hhi_within_bounds(ledgers: dict[str, Ledger], user_id: str) -> None:
    """현금을 분모에 넣으면 HHI가 하한 1/N 밑으로 내려간다. 그 실수를 잡는 테스트다."""
    ledger = ledgers[user_id]
    engine = PortfolioEngine(ledger)
    for day in _days(ledger):
        snapshot = engine.snapshot(day)
        if not snapshot.holdings:
            continue
        hhi = snapshot.concentration().hhi
        lower = 1.0 / len(snapshot.holdings)
        assert lower - IDENTITY_TOL <= hhi <= 1.0 + IDENTITY_TOL, f"{user_id} {day}: {hhi!r}"
