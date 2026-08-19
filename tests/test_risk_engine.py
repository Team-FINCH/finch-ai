"""Risk Engine 회귀 테스트. 산식 §3.2–§3.7.

통과만 하는 지표는 쓸모가 없다. 여기 절반은 "잡아야 정상"인 케이스다 —
히스토리가 짧은데 변동성을 뱉는지, 역방향 정규화가 뒤집혔는지, 히스테리시스가
경계에서 등급을 매일 뒤집는지, 문서에 없는 섹터명을 표에 심어 놨는지.
"""

from __future__ import annotations

import json
import math
from datetime import date, timedelta
from pathlib import Path

import pytest

from app.core.config import settings
from app.core.enums import FindingCategory, RateSensitivity, RiskLevel, Severity
from app.engines.portfolio import Holding, PortfolioSnapshot
from app.engines.risk import (
    _LEVEL_CUTS,
    _RATE_SENSITIVITY,
    _SCORE_COMPONENTS,
    _level,
    _normalize,
    assess,
)

DAY0 = date(2025, 1, 2)
SECTORS_PATH = Path(__file__).resolve().parents[1] / "ingest" / "ksic_sectors.json"


# ── 픽스처 헬퍼 ─────────────────────────────────────────────
def _holding(symbol: str, weight: float, sector: str = "반도체") -> Holding:
    """비중과 섹터만 의미 있는 최소 보유. 나머지 필드는 위험 지표에 쓰이지 않는다."""
    return Holding(
        symbol=symbol,
        name=symbol,
        sector=sector,
        quantity=1.0,
        price=100.0,
        avg_cost=100.0,
        market_value=weight * 100.0,
        weight=weight,
        stock_weight=weight,
        unrealized_pnl=0.0,
        return_rate=0.0,
    )


def _snapshot(*holdings: Holding, cash_weight: float = 0.0) -> PortfolioSnapshot:
    return PortfolioSnapshot(
        trade_date=DAY0,
        holdings=holdings,
        cash=cash_weight * 100.0,
        total_value=100.0,
        cash_weight=cash_weight,
    )


def _series(*daily_returns: float, start: float = 100.0) -> dict[date, float]:
    """일별 수익률 리스트를 종가 시계열로 편다. 거래일은 하루씩 이어 붙인다."""
    prices = {DAY0: start}
    level = start
    for i, r in enumerate(daily_returns, start=1):
        level *= 1 + r
        prices[DAY0 + timedelta(days=i)] = level
    return prices


def _zigzag(n: int, amplitude: float, *, flip: bool = False) -> list[float]:
    """±amplitude를 번갈아 내는 수익률. flip이면 위상을 반대로 — 상관 −1을 만든다."""
    sign = -1.0 if flip else 1.0
    return [sign * amplitude * (1 if i % 2 == 0 else -1) for i in range(n)]


N = settings.min_history_days + 5  # 충분한 표본


# ── §3.5 섹터 어휘 ──────────────────────────────────────────
def test_금리민감도_표의_섹터명은_전부_ksic_어휘다() -> None:
    """문서에 적힌 "IT"·"산업재"를 그대로 키로 쓰면 어떤 종목도 매칭되지 않는다.

    실제 섹터명은 `ingest/ksic_sectors.json`이 유일한 출처다. 이전에 표를 손으로
    지어내다 픽스처가 깨진 적이 있어서 소스와 직접 맞춘다.
    """
    known = set(json.loads(SECTORS_PATH.read_text(encoding="utf-8"))["sectors"])
    unknown = set(_RATE_SENSITIVITY) - known
    assert not unknown, f"ksic_sectors.json에 없는 섹터명: {sorted(unknown)}"


def test_표에_없는_섹터는_금리민감도_0으로_본다() -> None:
    """ "기타"·"미분류"에 임의 계수를 주면 분류 실패가 진단으로 새어 나온다."""
    result = assess(_snapshot(_holding("A", 1.0, sector="기타")), {})
    assert result.rate_exposure.score == 0.0
    assert result.rate_exposure.level is RateSensitivity.LOW


@pytest.mark.parametrize(
    ("sector", "expected"),
    [
        ("반도체", RateSensitivity.HIGH),
        ("경기소비재", RateSensitivity.MODERATE),
        ("은행", RateSensitivity.LOW),
    ],
)
def test_단일섹터_포트폴리오의_금리민감도(sector: str, expected: RateSensitivity) -> None:
    """S = 1.0·계수. 반도체 1.0 → high, 경기소비재 0.3 → moderate, 은행 −0.5 → low."""
    result = assess(_snapshot(_holding("A", 1.0, sector=sector)), {})
    assert result.rate_exposure.level is expected


def test_금융_비중이_성장_비중을_상쇄한다() -> None:
    """은행 −0.5가 실제로 빼져야 한다. 절댓값만 더하면 high로 잘못 올라간다."""
    result = assess(
        _snapshot(_holding("A", 0.5, sector="반도체"), _holding("B", 0.5, sector="은행")), {}
    )
    assert result.rate_exposure.score == pytest.approx(0.25)
    assert result.rate_exposure.level is RateSensitivity.MODERATE


# ── §3.6 정규화 ─────────────────────────────────────────────
@pytest.mark.parametrize(
    ("x", "lo", "hi", "expected"),
    [
        (0.10, 0.10, 0.50, 0.0),
        (0.50, 0.10, 0.50, 100.0),
        (0.30, 0.10, 0.50, 50.0),
        (0.90, 0.10, 0.50, 100.0),  # 범위 밖은 절단
        (0.00, 0.10, 0.50, 0.0),
    ],
)
def test_정규화는_구간선형이고_범위밖을_절단한다(
    x: float, lo: float, hi: float, expected: float
) -> None:
    assert _normalize(x, lo, hi) == pytest.approx(expected)


@pytest.mark.parametrize(
    ("dr", "expected"),
    [(1.05, 100.0), (1.50, 0.0), (1.275, 50.0), (2.00, 0.0), (1.00, 100.0)],
)
def test_역방향_지표는_같은_식으로_뒤집힌다(dr: float, expected: float) -> None:
    """`DR`은 `lo > hi`다. 분모가 음수라 clip 방향까지 함께 뒤집힌다 —
    잘 분산된 DR=2.0이 100점(위험)으로 나오면 부호를 놓친 것이다.
    """
    assert _normalize(dr, 1.50, 1.05) == pytest.approx(expected)


def test_역방향_현금비중도_같다() -> None:
    """현금이 많을수록 안전해야 한다. w_c=0.02는 100점, 0.20은 0점."""
    assert _normalize(0.02, 0.20, 0.02) == pytest.approx(100.0)
    assert _normalize(0.20, 0.20, 0.02) == pytest.approx(0.0)


def test_가중치_합은_100이다() -> None:
    """합이 100이 아니면 risk_score가 0–100을 벗어난다."""
    assert sum(weight for *_, weight in _SCORE_COMPONENTS) == pytest.approx(100.0)


# ── §3.6 히스테리시스 ───────────────────────────────────────
@pytest.mark.parametrize(
    ("score", "expected"),
    [
        (0.0, RiskLevel.LOW),
        (34.9, RiskLevel.LOW),
        (35.0, RiskLevel.MODERATE),
        (64.9, RiskLevel.MODERATE),
        (65.0, RiskLevel.HIGH),
        (100.0, RiskLevel.HIGH),
    ],
)
def test_첫_진단은_히스테리시스_없이_구간을_쓴다(score: float, expected: RiskLevel) -> None:
    assert _level(score, None) is expected


@pytest.mark.parametrize(
    ("previous", "score", "expected"),
    [
        # 문서의 예시 그대로: moderate → high는 68점 이상.
        (RiskLevel.MODERATE, 66.0, RiskLevel.MODERATE),
        (RiskLevel.MODERATE, 68.0, RiskLevel.HIGH),
        # high → moderate는 62점 이하.
        (RiskLevel.HIGH, 63.0, RiskLevel.HIGH),
        (RiskLevel.HIGH, 62.0, RiskLevel.MODERATE),
        # low/moderate 경계도 같은 폭.
        (RiskLevel.LOW, 37.0, RiskLevel.LOW),
        (RiskLevel.LOW, 38.0, RiskLevel.MODERATE),
        (RiskLevel.MODERATE, 33.0, RiskLevel.MODERATE),
        (RiskLevel.MODERATE, 32.0, RiskLevel.LOW),
    ],
)
def test_등급_변경은_3점을_벌려야_반영된다(
    previous: RiskLevel, score: float, expected: RiskLevel
) -> None:
    """임계 근처에서 매일 등급이 뒤집히면 신뢰를 잃는다(§3.6 경계 안정화)."""
    assert _level(score, previous) is expected


_LEVELS_BELOW = {35.0: RiskLevel.LOW, 65.0: RiskLevel.MODERATE}
_LEVELS_ABOVE = {35.0: RiskLevel.MODERATE, 65.0: RiskLevel.HIGH}


@pytest.mark.parametrize("cut", _LEVEL_CUTS)
def test_경계에_머무르면_이전_등급을_유지한다(cut: float) -> None:
    """경계값 자체에서는 어느 쪽에서 왔든 움직이지 않아야 한다."""
    below = _LEVELS_BELOW[cut]
    above = _LEVELS_ABOVE[cut]
    assert _level(cut, below) is below
    assert _level(cut, above) is above


def test_두_등급을_한꺼번에_건너뛸_수_있다() -> None:
    """low에서 점수가 90으로 뛰면 두 경계를 모두 3점 이상 넘었으므로 high까지 간다."""
    assert _level(90.0, RiskLevel.LOW) is RiskLevel.HIGH


def test_한_경계만_넘었으면_한_칸만_간다() -> None:
    """low → 66점: 35 경계는 넘었지만 65 경계는 68에 못 미친다. moderate에서 멈춘다."""
    assert _level(66.0, RiskLevel.LOW) is RiskLevel.MODERATE


# ── §3.2 변동성 ─────────────────────────────────────────────
def test_히스토리가_짧으면_변동성을_추정하지_않는다() -> None:
    """20일 표본으로 뽑은 연율 변동성은 숫자만 그럴듯하다. 거절하는 게 맞다."""
    prices = {"A": _series(*_zigzag(20, 0.01))}
    result = assess(_snapshot(_holding("A", 1.0)), prices)
    assert result.volatility is None
    assert result.diversification is None
    assert result.risk_score is None
    assert result.risk_level is None
    assert result.insufficient_history is not None
    # 집중도·현금·금리민감도는 그래도 나와야 한다 — 신규 포트폴리오도 절반은 진단된다.
    assert result.concentration.top1 == pytest.approx(1.0)
    assert result.rate_exposure.level is RateSensitivity.HIGH


def test_경계_바로_위면_추정한다() -> None:
    """`min_history_days`개의 *수익률*이 기준이다. 종가는 하루 더 필요하다."""
    prices = {"A": _series(*_zigzag(settings.min_history_days, 0.01))}
    result = assess(_snapshot(_holding("A", 1.0)), prices)
    assert result.volatility is not None
    assert result.volatility.observed_days == settings.min_history_days


def test_종목별_연율_변동성은_표준편차_x_sqrt246이다() -> None:
    returns = _zigzag(N, 0.02)
    result = assess(_snapshot(_holding("A", 1.0)), {"A": _series(*returns)})
    assert result.volatility is not None
    # ±2%를 번갈아 내는 계열의 일별 표준편차는 약 0.02.
    expected = 0.02 * math.sqrt(settings.trading_days_per_year)
    assert result.volatility.by_symbol["A"] == pytest.approx(expected, rel=0.05)
    assert result.volatility.portfolio == pytest.approx(expected, rel=0.05)


def test_포트폴리오_변동성은_가중평균이_아니다() -> None:
    """반대 위상 두 종목을 반반 담으면 σ_p가 0에 가까워야 한다. 가중평균이면
    개별 변동성이 그대로 남는다 — 이 차이가 곧 분산효과다(§3.2).
    """
    up = _zigzag(N, 0.02)
    down = _zigzag(N, 0.02, flip=True)
    result = assess(
        _snapshot(_holding("A", 0.5), _holding("B", 0.5)),
        {"A": _series(*up), "B": _series(*down)},
    )
    assert result.volatility is not None
    individual = result.volatility.by_symbol["A"]
    assert individual > 0.2  # 개별은 크다
    assert result.volatility.portfolio < individual * 0.1  # 합쳐지면 거의 사라진다


def test_0원_종가는_수익률을_오염시키지_않는다() -> None:
    """0으로 나누면 inf/nan이 나오고 그 nan이 risk_score까지 번진다. 거절해야 한다."""
    prices = {"A": _series(*_zigzag(N, 0.01))}
    prices["A"][DAY0 + timedelta(days=3)] = 0.0
    result = assess(_snapshot(_holding("A", 1.0)), prices)
    assert result.volatility is None
    assert result.risk_score is None


# ── §3.3 상관과 분산효과 ────────────────────────────────────
def test_완전_동행이면_분산효과가_없다() -> None:
    """같은 계열 두 종목의 DR은 1.0이어야 한다(§3.3 DR ≥ 1, DR = 1이면 분산 전무)."""
    returns = _zigzag(N, 0.015)
    result = assess(
        _snapshot(_holding("A", 0.5), _holding("B", 0.5)),
        {"A": _series(*returns), "B": _series(*returns)},
    )
    assert result.diversification is not None
    assert result.diversification.ratio == pytest.approx(1.0, abs=1e-9)
    assert result.diversification.avg_correlation == pytest.approx(1.0)


def test_반대_위상이면_DR이_크게_뛴다() -> None:
    result = assess(
        _snapshot(_holding("A", 0.5), _holding("B", 0.5)),
        {"A": _series(*_zigzag(N, 0.02)), "B": _series(*_zigzag(N, 0.02, flip=True))},
    )
    assert result.diversification is not None
    # 서로를 상계하므로 σ_p ≈ 0이고 DR은 무한대에 가깝다. 여기서 1.0이 나오면
    # 분모 0 처리를 "분산효과 없음"으로 잘못 접은 것이다.
    assert result.diversification.ratio > 5.0
    assert result.diversification.avg_correlation == pytest.approx(-1.0)
    # 점수에서도 "분산 실패" 가중 15점이 0으로 떨어져야 한다.
    assert all(f.id != "correlation" for f in result.findings)


def test_DR은_항상_1_이상이다() -> None:
    """DR < 1이 나오면 σ_p나 Σŵσ 어느 쪽 계산이 틀린 것이다(§3.3)."""
    prices = {
        s: _series(*[0.01 * ((i + k) % 5 - 2) for i in range(N)])
        for k, s in enumerate(("A", "B", "C", "D"))
    }
    result = assess(_snapshot(*(_holding(s, 0.25) for s in prices)), prices)
    assert result.diversification is not None
    assert result.diversification.ratio >= 1.0 - 1e-9


def test_단일_종목은_평균상관이_정의되지_않는다() -> None:
    """ρ̄의 분모가 Σ_{i≠j} ŵ_i ŵ_j라 종목이 하나면 0이다. 0이나 1로 채우면 거짓이다."""
    result = assess(_snapshot(_holding("A", 1.0)), {"A": _series(*_zigzag(N, 0.01))})
    assert result.diversification is not None
    assert result.diversification.avg_correlation is None
    # DR은 정의상 정확히 1 — 분산효과가 전무하다는 뜻이고, 점수에서 그렇게 반영된다.
    assert result.diversification.ratio == pytest.approx(1.0)


def test_상관행렬은_보유_종목_순서를_따른다() -> None:
    prices = {"A": _series(*_zigzag(N, 0.01)), "B": _series(*_zigzag(N, 0.01, flip=True))}
    result = assess(_snapshot(_holding("A", 0.5), _holding("B", 0.5)), prices)
    assert result.diversification is not None
    assert result.diversification.symbols == ("A", "B")
    matrix = result.diversification.correlations
    assert matrix[0][0] == pytest.approx(1.0)
    assert matrix[0][1] == pytest.approx(-1.0)


def test_거래일이_겹치는_구간만_쓴다() -> None:
    """종목마다 히스토리 길이가 다르다. 교집합이 짧으면 긴 쪽 길이를 믿어선 안 된다."""
    long_series = _series(*_zigzag(N, 0.01))
    short_series = {d: v for d, v in long_series.items() if d >= DAY0 + timedelta(days=40)}
    result = assess(
        _snapshot(_holding("A", 0.5), _holding("B", 0.5)),
        {"A": long_series, "B": short_series},
    )
    # 교집합이 min_history_days 미만 → 추정 거절.
    assert result.volatility is None
    assert result.insufficient_history is not None


def test_시세가_아예_없는_종목이_있으면_거절한다() -> None:
    """편입 직후라 종가가 없는 종목이 섞이면 교집합이 비어 표본이 0이 된다."""
    result = assess(
        _snapshot(_holding("A", 0.5), _holding("B", 0.5)),
        {"A": _series(*_zigzag(N, 0.01))},
    )
    assert result.volatility is None
    assert "0일" in result.insufficient_history  # type: ignore[operator]


# ── §3.4 낙폭 ───────────────────────────────────────────────
def test_최대낙폭은_고점_대비_최저점이다() -> None:
    days = [DAY0 + timedelta(days=i) for i in range(5)]
    series = list(zip(days, [100.0, 120.0, 90.0, 110.0, 100.0], strict=True))
    result = assess(_snapshot(_holding("A", 1.0)), {}, value_series=series)
    # 고점 120 → 저점 90 = −25%. 시작가 100 기준 −10%로 계산하면 틀린 것이다.
    assert result.drawdown.mdd == pytest.approx(-0.25)
    assert result.drawdown.peak_date == days[1]
    assert result.drawdown.trough_date == days[2]
    assert result.drawdown.observed_days == 5


def test_단조_상승이면_낙폭이_0이다() -> None:
    series = [(DAY0 + timedelta(days=i), 100.0 + i) for i in range(10)]
    result = assess(_snapshot(_holding("A", 1.0)), {}, value_series=series)
    assert result.drawdown.mdd == 0.0
    assert result.drawdown.peak_date is None


def test_시드머니_입금_전_0원은_건너뛴다() -> None:
    """0을 peak로 잡으면 0으로 나눈다. 첫 양수 시점부터 세야 한다."""
    days = [DAY0 + timedelta(days=i) for i in range(4)]
    series = list(zip(days, [0.0, 100.0, 80.0, 90.0], strict=True))
    result = assess(_snapshot(_holding("A", 1.0)), {}, value_series=series)
    assert result.drawdown.mdd == pytest.approx(-0.20)
    assert result.drawdown.observed_days == 3


def test_기간을_알_수_있게_표본_일수를_같이_준다() -> None:
    """ "최대 낙폭 −8%"만 보여주면 3년치인지 3주치인지 모른다(§3.4 주의사항)."""
    result = assess(_snapshot(_holding("A", 1.0)), {}, value_series=())
    assert result.drawdown.observed_days == 0


# ── 베타·스타일 편중: 데이터 없음 ───────────────────────────
def test_베타는_None으로_남는다() -> None:
    """`index_daily`가 비어 있어 계산할 수 없다. 0.0으로 채우면 "시장과 무관"이라는
    거짓 진단이 API로 그대로 나간다.
    """
    result = assess(_snapshot(_holding("A", 1.0)), {"A": _series(*_zigzag(N, 0.01))})
    assert result.beta is None


def test_style_tilt_finding은_만들지_않는다() -> None:
    """시가총액이 전 종목 NULL이라 w_growth를 계산할 수 없다."""
    result = assess(_snapshot(_holding("A", 1.0)), {"A": _series(*_zigzag(N, 0.01))})
    assert all(f.category is not FindingCategory.STYLE_TILT for f in result.findings)


# ── §3.6 risk_score ────────────────────────────────────────
def test_한_종목에_전액이면_점수가_높다() -> None:
    """HHI 1.0·HHI_sec 1.0·DR 1.0·현금 0 — 집중도 관련 가중 80점이 만점이다."""
    result = assess(_snapshot(_holding("A", 1.0)), {"A": _series(*_zigzag(N, 0.005))})
    assert result.risk_score is not None
    assert result.risk_score >= 80.0
    assert result.risk_level is RiskLevel.HIGH


def test_잘_분산된_포트폴리오는_점수가_낮다() -> None:
    """10종목·10섹터·서로 다른 위상·현금 20%. 점수가 낮게 나와야 한다."""
    sectors = (
        "반도체",
        "은행",
        "유틸리티",
        "통신",
        "필수소비재",
        "자동차",
        "철강/금속",
        "건설",
        "운송",
        "유통",
    )
    prices = {
        f"S{k}": _series(*[0.01 * math.sin((i + k) * 2 * math.pi / 10) for i in range(N)])
        for k in range(10)
    }
    snapshot = _snapshot(
        *(_holding(f"S{k}", 0.08, sector=sectors[k]) for k in range(10)),
        cash_weight=0.20,
    )
    result = assess(snapshot, prices)
    assert result.risk_score is not None
    assert result.risk_score < 35.0
    assert result.risk_level is RiskLevel.LOW


def test_점수는_0과_100_사이다() -> None:
    """정규화가 절단되고 가중치 합이 100이라 벗어날 수 없다."""
    result = assess(
        _snapshot(_holding("A", 1.0), cash_weight=0.0),
        {"A": _series(*_zigzag(N, 0.10))},  # 극단적 변동성
    )
    assert result.risk_score is not None
    assert 0.0 <= result.risk_score <= 100.0


def test_직전_등급을_넘기면_등급만_고정되고_점수는_그대로다() -> None:
    """히스테리시스는 등급에만 걸린다. 점수를 같이 눌러 버리면 추이가 사라진다."""
    prices = {"A": _series(*_zigzag(N, 0.005))}
    snapshot = _snapshot(_holding("A", 1.0))
    fresh = assess(snapshot, prices)
    held = assess(snapshot, prices, previous_level=RiskLevel.LOW)
    assert held.risk_score == fresh.risk_score


# ── §3.7 findings ──────────────────────────────────────────
@pytest.mark.parametrize(
    ("top1", "expected"),
    [(0.24, None), (0.25, Severity.MEDIUM), (0.39, Severity.MEDIUM), (0.40, Severity.HIGH)],
)
def test_종목_집중도_임계(top1: float, expected: Severity | None) -> None:
    """임계 바로 아래는 발생하지 않아야 한다 — 0.24에서 finding이 나오면 과민하다."""
    rest = (1.0 - top1) / 9
    snapshot = _snapshot(_holding("A", top1), *(_holding(f"B{i}", rest) for i in range(9)))
    found = {f.id: f for f in assess(snapshot, {}).findings}
    if expected is None:
        assert "ticker_concentration" not in found
    else:
        assert found["ticker_concentration"].severity is expected


@pytest.mark.parametrize(
    ("weight", "expected"),
    [(0.34, None), (0.35, Severity.MEDIUM), (0.49, Severity.MEDIUM), (0.50, Severity.HIGH)],
)
def test_섹터_집중도는_HHI가_아니라_단일_섹터_최대비중이다(
    weight: float, expected: Severity | None
) -> None:
    """§3.7은 "단일 섹터 비중"을 본다. HHI_sec으로 대체하면 임계가 전혀 달라진다."""
    others = (1.0 - weight) / 4
    snapshot = _snapshot(
        _holding("A", weight / 2, sector="반도체"),
        _holding("B", weight / 2, sector="반도체"),
        *(
            _holding(f"C{i}", others, sector=s)
            for i, s in enumerate(("은행", "통신", "운송", "화학"))
        ),
    )
    result = assess(snapshot, {})
    assert result.top_sector_weight == pytest.approx(weight)
    found = {f.id: f for f in result.findings}
    if expected is None:
        assert "sector_concentration" not in found
    else:
        assert found["sector_concentration"].severity is expected


@pytest.mark.parametrize(
    ("cash", "expected"),
    [(0.06, None), (0.05, Severity.INFO), (0.03, Severity.INFO), (0.02, Severity.MEDIUM)],
)
def test_현금_완충_임계는_이하_조건이다(cash: float, expected: Severity | None) -> None:
    """`w_c ≤ 0.05`. 부등호를 뒤집으면 현금 많은 사람에게 경고가 날아간다."""
    snapshot = _snapshot(_holding("A", 1.0), cash_weight=cash)
    found = {f.id: f for f in assess(snapshot, {}).findings}
    if expected is None:
        assert "liquidity" not in found
    else:
        assert found["liquidity"].severity is expected


def test_변동성_finding은_히스토리가_없으면_안_나온다() -> None:
    """σ_p를 못 구했는데 finding을 만들면 근거 없는 경고가 된다."""
    result = assess(_snapshot(_holding("A", 1.0)), {"A": _series(*_zigzag(10, 0.05))})
    assert all(f.id != "volatility" for f in result.findings)


def test_변동성_finding_임계() -> None:
    """일별 ±2.5%는 연율 39% 남짓 — medium. ±3%면 47%로 high."""
    medium = assess(_snapshot(_holding("A", 1.0)), {"A": _series(*_zigzag(N, 0.025))})
    high = assess(_snapshot(_holding("A", 1.0)), {"A": _series(*_zigzag(N, 0.030))})
    assert {f.id: f.severity for f in medium.findings}["volatility"] is Severity.MEDIUM
    assert {f.id: f.severity for f in high.findings}["volatility"] is Severity.HIGH


def test_상관_finding은_N_eff_3_미만이면_안_나온다() -> None:
    """ "숫자상 분산, 실질은 집중"만 잡는 조건이다. 2종목에는 할 말이 아니다(§3.3)."""
    returns = _zigzag(N, 0.01)
    result = assess(
        _snapshot(_holding("A", 0.5), _holding("B", 0.5)),
        {"A": _series(*returns), "B": _series(*returns)},
    )
    assert result.diversification is not None
    assert result.diversification.ratio == pytest.approx(1.0, abs=1e-9)  # DR은 임계 이하
    assert result.concentration.n_eff == pytest.approx(2.0)
    assert all(f.id != "correlation" for f in result.findings)  # 그래도 발생하지 않는다


def test_단일_종목에는_상관_finding이_없다() -> None:
    """N=1은 DR이 정의상 1.0이지만 "분산에 실패했다"고 지적할 대상이 아니다."""
    result = assess(_snapshot(_holding("A", 1.0)), {"A": _series(*_zigzag(N, 0.01))})
    assert all(f.id != "correlation" for f in result.findings)


def test_숫자상_분산_실질은_집중을_잡는다() -> None:
    """3종목을 고르게 담았는데 셋이 같이 움직이면 DR ≈ 1이고 N_eff = 3이다.
    기획서의 "종목은 세 개지만 실질 분산은 제한적"이 바로 이 조합이다.
    """
    returns = _zigzag(N, 0.01)
    prices = {s: _series(*returns) for s in ("A", "B", "C")}
    result = assess(_snapshot(*(_holding(s, 1 / 3) for s in prices)), prices)
    found = {f.id: f for f in result.findings}
    assert found["correlation"].severity is Severity.HIGH
    assert result.concentration.n_eff == pytest.approx(3.0)


def test_분산이_실제로_작동하면_상관_finding이_없다() -> None:
    """DR이 1.20을 넘으면 3종목이어도 지적할 이유가 없다."""
    prices = {
        s: _series(*[0.02 * math.sin((i + k) * 2 * math.pi / 3) for i in range(N)])
        for k, s in enumerate(("A", "B", "C"))
    }
    result = assess(_snapshot(*(_holding(s, 1 / 3) for s in prices)), prices)
    assert result.diversification is not None
    assert result.diversification.ratio > 1.20
    assert all(f.id != "correlation" for f in result.findings)


def test_금리민감도_high면_macro_exposure를_남긴다() -> None:
    result = assess(_snapshot(_holding("A", 1.0, sector="2차전지")), {})
    found = {f.id: f for f in result.findings}
    assert found["macro_exposure"].severity is Severity.INFO
    assert found["macro_exposure"].category is FindingCategory.MACRO_EXPOSURE


def test_findings는_severity_다음_초과폭_순이다() -> None:
    """§3.7 정렬 규칙. info가 high보다 앞에 오면 사용자가 사소한 것부터 읽는다."""
    snapshot = _snapshot(_holding("A", 0.6, sector="반도체"), _holding("B", 0.4, sector="반도체"))
    result = assess(snapshot, {})
    ranks = [f.severity for f in result.findings]
    assert ranks == sorted(
        ranks, key=lambda s: {"high": 2, "medium": 1, "info": 0}[s], reverse=True
    )
    # 동률 구간에서는 초과폭이 큰 것이 앞이다.
    for a, b in zip(result.findings, result.findings[1:], strict=False):
        if a.severity is b.severity:
            assert a.exceedance >= b.exceedance


def test_한_id에_대해_finding은_하나다() -> None:
    """top1 = 0.6은 0.25와 0.40을 모두 넘지만 high 하나만 남아야 한다."""
    snapshot = _snapshot(_holding("A", 0.6), _holding("B", 0.4))
    ids = [f.id for f in assess(snapshot, {}).findings]
    assert ids.count("ticker_concentration") == 1


def test_초과폭은_역방향_지표에서도_양수다() -> None:
    """정렬 키라서 부호가 뒤집히면 순서가 거꾸로 된다."""
    snapshot = _snapshot(_holding("A", 1.0), cash_weight=0.01)
    found = {f.id: f for f in assess(snapshot, {}).findings}
    assert found["liquidity"].exceedance == pytest.approx(0.01)


def test_문제없는_포트폴리오는_findings가_비어_있다() -> None:
    """임계를 아무것도 넘지 않으면 조용해야 한다. 항상 뭔가 뱉으면 신호가 죽는다."""
    sectors = ("은행", "통신", "유틸리티", "필수소비재", "운송")
    snapshot = _snapshot(
        *(_holding(f"S{i}", 0.16, sector=s) for i, s in enumerate(sectors)),
        cash_weight=0.20,
    )
    prices = {
        f"S{i}": _series(*[0.003 * math.sin((k + i) * 2 * math.pi / 5) for k in range(N)])
        for i in range(5)
    }
    assert assess(snapshot, prices).findings == ()


# ── 실제 어댑터와 조립되는지 ────────────────────────────────
def test_시드_원장에서_그대로_조립된다() -> None:
    """`Ledger.prices`와 `PortfolioEngine` 스냅샷이 인자 모양에 그대로 맞는지 확인한다.

    엔진만 단위 테스트하면 `prices`/`value_series` 모양이 어긋난 걸 API 트랙에서야
    발견한다. 시드는 10거래일뿐이라 변동성은 거절되는 게 정상이고, 그 경로까지 함께
    본다.
    """
    from app.core.adapters import SeedLedgerSource
    from app.engines.portfolio import PortfolioEngine

    ledger = SeedLedgerSource(
        Path(__file__).resolve().parent / "fixtures" / "seed_portfolio.json"
    ).load("mixed_with_cash")
    engine = PortfolioEngine(ledger)
    last = ledger.trading_days[-1]

    result = assess(
        engine.snapshot(last),
        ledger.prices,
        value_series=[(d, engine.snapshot(d).total_value) for d in ledger.trading_days],
        previous_level=RiskLevel.MODERATE,
    )
    assert result.trade_date == last
    assert result.volatility is None and result.insufficient_history is not None
    assert result.drawdown.observed_days == len(ledger.trading_days)
    assert result.drawdown.mdd <= 0.0
    assert result.top_sector in {h.sector for h in engine.snapshot(last).holdings}
    assert result.concentration.top1 > 0.0


def test_빈_포트폴리오도_터지지_않는다() -> None:
    """전량 매도 직후에는 보유가 0종목이다. 0으로 나누는 자리가 여럿 있어 한 번 본다."""
    empty = PortfolioSnapshot(
        trade_date=DAY0, holdings=(), cash=100.0, total_value=100.0, cash_weight=1.0
    )
    result = assess(empty, {})
    assert result.top_sector is None
    assert result.top_sector_weight == 0.0
    assert result.concentration.n_eff == 0.0
    assert result.risk_score is None
    assert result.findings == ()
