"""시드 픽스처가 다른 트랙에 약속한 조건을 고정한다.

프롬프트·리스크 트랙이 이 픽스처를 그대로 가져다 쓴다. 조건이 하나라도 빠지면
그쪽 진단(섹터 편중, 손익 부호 문구)이 조용히 빈손이 되므로 여기서 못박아 둔다.
"""

from __future__ import annotations

from app.core.adapters import Ledger
from app.engines.portfolio import PortfolioEngine

SEMICONDUCTOR = ("005930", "000660", "042700")


def _last(ledger: Ledger):
    return PortfolioEngine(ledger).snapshot(ledger.trading_days[-1])


def test_semiconductor_sector_is_concentrated(ledgers: dict[str, Ledger]) -> None:
    """반도체 3종 합이 40% 이상. 섹터 집중 진단이 실제로 발동해야 한다."""
    snapshot = _last(ledgers["mixed_with_cash"])
    share = sum(h.stock_weight for h in snapshot.holdings if h.symbol in SEMICONDUCTOR)
    assert share >= 0.40, share


def test_at_least_five_sectors(ledgers: dict[str, Ledger]) -> None:
    snapshot = _last(ledgers["mixed_with_cash"])
    assert len({h.sector for h in snapshot.holdings}) >= 5


def test_both_pnl_signs_exist(ledgers: dict[str, Ledger]) -> None:
    """평가손익이 양수인 종목과 음수인 종목이 모두 있어야 한다."""
    holdings = _last(ledgers["mixed_with_cash"]).holdings
    assert any(h.unrealized_pnl > 0 for h in holdings)
    assert any(h.unrealized_pnl < 0 for h in holdings)


def test_low_cash_portfolio_exists(ledgers: dict[str, Ledger]) -> None:
    assert _last(ledgers["mixed_with_cash"]).cash_weight < 0.05


def test_deposit_portfolio_has_external_flow(ledgers: dict[str, Ledger]) -> None:
    """기간 중(첫날 시드 이후) 외부 입금이 실제로 있어야 골든 4가 의미를 가진다."""
    ledger = ledgers["golden_4_deposit"]
    assert any(ledger.flow_on(day) for day in ledger.trading_days[1:])


def test_single_portfolio_holds_exactly_one_stock(ledgers: dict[str, Ledger]) -> None:
    """골든 1은 매 거래일 단일 종목이어야 HHI=1·N_eff=1이 성립한다."""
    ledger = ledgers["golden_1_single"]
    engine = PortfolioEngine(ledger)
    for day in ledger.trading_days:
        concentration = engine.snapshot(day).concentration()
        assert len(engine.snapshot(day).holdings) == 1
        assert abs(concentration.hhi - 1.0) < 1e-9
        assert abs(concentration.n_eff - 1.0) < 1e-9


def test_fixture_sectors_use_the_project_taxonomy() -> None:
    """픽스처의 섹터는 적재기가 실제로 만들어내는 어휘와 같아야 한다.

    ingest/ksic_sectors.json 이 유일한 출처다. 픽스처가 자기만의 이름을 쓰면
    테스트는 통과하지만 Risk Engine이 실데이터에서 다른 어휘를 보게 된다.
    """
    import json
    from pathlib import Path

    root = Path(__file__).resolve().parents[2]
    known = set(json.loads((root / "ingest" / "ksic_sectors.json").read_text(encoding="utf-8"))["sectors"])
    fixture = json.loads((root / "tests" / "fixtures" / "seed_portfolio.json").read_text(encoding="utf-8"))
    used = {v["sector"] for v in fixture["instruments"].values()}

    assert used <= known, f"표준 섹터에 없는 값: {sorted(used - known)}"
