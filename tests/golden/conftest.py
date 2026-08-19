"""골든 케이스 공용 픽스처.

원장은 백엔드가 소유하고 우리는 읽기만 한다. 아직 읽기 권한이 없으므로
시드 어댑터로 JSON 픽스처를 읽는다. 실제 어댑터로 갈아끼워도 이 테스트는
그대로 돌아야 한다 — 그래서 테스트는 `LedgerSource` 인터페이스만 본다.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.core.adapters import Ledger, LedgerSource, SeedLedgerSource

FIXTURE_PATH = Path(__file__).resolve().parents[1] / "fixtures" / "seed_portfolio.json"

# 픽스처에 담긴 포트폴리오. 항등식은 넷 모두에서 성립해야 한다.
ALL_PORTFOLIOS = ("golden_1_single", "golden_4_deposit", "mixed_with_cash", "avg_cost")

# 산식 문서 §6.3 항등식 1·2·3의 허용 오차
IDENTITY_TOL = 1e-9


@pytest.fixture(scope="session")
def source() -> LedgerSource:
    return SeedLedgerSource(FIXTURE_PATH)


@pytest.fixture(scope="session")
def ledgers(source: LedgerSource) -> dict[str, Ledger]:
    return {user_id: source.load(user_id) for user_id in ALL_PORTFOLIOS}
