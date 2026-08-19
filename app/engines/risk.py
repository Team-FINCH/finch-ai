"""Risk Engine. 산식 문서 §3.2–§3.7.

`PortfolioSnapshot` 하나와 종가 시계열만 받아서 위험 지표를 뽑는 순수 함수다. DB를
보지 않는 이유는 `portfolio.py`와 같다 — 같은 입력이면 같은 숫자가 나와야 골든
픽스처로 회귀를 잡을 수 있다.

§3.1 집중도는 `PortfolioSnapshot.concentration()`이 이미 계산한다. 여기서는 그
결과를 소비하고, 문서 §3.7의 `sector_concentration`이 요구하는 *단일 섹터 최대 비중*만
따로 구한다(`HHI_sec`는 합산 지표라 "어느 섹터가 몇 %인가"에 답하지 못한다).

벤치마크 시계열(§3.4 베타), 시가총액 순위(§3.5 대형주 비중), PBR 백분위(§3.5 성장주
비중)도 DB가 아니라 인자로 받는다. 이유는 위와 같다 — 엔진이 조회를 시작하는 순간
골든 픽스처가 무의미해진다. 셋 다 선택 인자라서 안 넘기면 해당 지표만 `None`이 되고
나머지 진단은 그대로 나온다.

같은 §3.5의 금리민감도는 섹터 규칙 기반이라 데이터가 필요 없고, 여기서 계산한다.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date

import numpy as np

from app.core.config import settings
from app.core.enums import FindingCategory, RateSensitivity, RiskLevel, Severity
from app.engines.portfolio import Concentration, PortfolioSnapshot

# ── §3.5 금리민감도 계수 ────────────────────────────────────
# 키는 전부 `ingest/ksic_sectors.json`의 `sectors` 어휘다. 문서가 쓴 이름
# ("IT", "바이오", "산업재")은 우리 섹터 목록에 없는 상위 개념이라 아래처럼 펼쳤다.
# 표에 없는 섹터("기타"·"미분류")는 0.0으로 본다.
_RATE_SENSITIVITY: Mapping[str, float] = {
    # +1.0 — 먼 미래 이익을 현재가치로 당겨 쓰는 섹터. 할인율이 오르면 가장 먼저 깎인다.
    #        문서의 "IT"는 IT하드웨어·소프트웨어, "바이오"는 바이오/제약으로 펼쳤다.
    "반도체": 1.0,
    "IT하드웨어": 1.0,
    "소프트웨어": 1.0,
    "인터넷": 1.0,
    "바이오/제약": 1.0,
    "2차전지": 1.0,
    # +0.3 — 문서의 "경기소비재·산업재". "산업재"라는 섹터명이 우리 어휘에 없어서
    #        실물 경기 사이클을 타는 제조·건설·운송·유통 계열로 나눠 담았다.
    #        의료기기는 문서의 "바이오"에 들어가지 않으므로 여기 둔다.
    "경기소비재": 0.3,
    "기계/장비": 0.3,
    "자동차": 0.3,
    "조선": 0.3,
    "건설": 0.3,
    "철강/금속": 0.3,
    "화학": 0.3,
    "운송": 0.3,
    "유통": 0.3,
    "미디어/엔터": 0.3,
    "의료기기": 0.3,
    "부동산": 0.3,
    # 0.0 — 규제요금·필수수요라 금리 국면과 상관이 약하다.
    "통신": 0.0,
    "유틸리티": 0.0,
    "필수소비재": 0.0,
    # −0.5 — 금리 상승이 예대마진·운용수익에 오히려 유리한 금융.
    "은행": -0.5,
    "보험": -0.5,
    "증권": -0.5,
}

# ── §3.5 대형 판정 ──────────────────────────────────────────
_LARGE_CAP_RANK = 100  # "시가총액 순위 ≤ 100위"

# ── §3.5 성장 판정 ──────────────────────────────────────────
# "PBR ≥ 시장 상위 30% 분위". 고정 임계(예: PBR 2배)가 아니라 분위라서, 백분위
# 0.70 이상이 곧 상위 30%다. 임계는 여기 두고 백분위 자체는 호출자가 준다 —
# `_growth_weight` docstring 참고.
_GROWTH_PERCENTILE = 0.70

# 벤치마크를 보유 종목 하나처럼 끼워 넣어 정렬할 때 쓰는 자리표. 종목코드는 6자리
# 숫자라 겹칠 일이 없다.
_BENCHMARK = "__benchmark__"

# ── §3.6 risk_score 구성요소 ────────────────────────────────
# (지표 이름, lo, hi, 가중). 역방향 지표는 `lo > hi`로 둔다 — `_normalize`가 같은 식
# 으로 처리하므로 분기가 필요 없다. 가중치 합은 100이어야 한다(테스트가 확인).
_SCORE_COMPONENTS: tuple[tuple[str, float, float, float], ...] = (
    ("hhi", 0.10, 0.50, 30.0),
    ("sector_hhi", 0.20, 0.60, 25.0),
    ("volatility", 0.15, 0.45, 20.0),
    ("diversification_ratio", 1.50, 1.05, 15.0),  # 역방향
    ("cash_weight", 0.20, 0.02, 10.0),  # 역방향
)

_LEVEL_CUTS: tuple[float, float] = (35.0, 65.0)  # low | moderate | high
_LEVELS: tuple[RiskLevel, ...] = (RiskLevel.LOW, RiskLevel.MODERATE, RiskLevel.HIGH)
_HYSTERESIS = 3.0  # §3.6 경계 안정화 폭


@dataclass(frozen=True, slots=True)
class Volatility:
    """§3.2. 연율 기준."""

    portfolio: float  # σ_p
    by_symbol: Mapping[str, float]  # σ_i
    observed_days: int  # 공분산 추정에 실제로 쓴 일별 수익률 개수


@dataclass(frozen=True, slots=True)
class Diversification:
    """§3.3. `symbols` 순서가 `correlations` 행·열 순서다."""

    ratio: float  # DR = Σŵσ / σ_p, 항상 ≥ 1
    avg_correlation: float | None  # ρ̄ — 종목이 1개면 i≠j 쌍이 없어 정의되지 않는다
    symbols: tuple[str, ...]
    correlations: tuple[tuple[float, ...], ...]  # ρ_ij


@dataclass(frozen=True, slots=True)
class Drawdown:
    """§3.4. `mdd`는 음수. `observed_days`를 같이 들고 다니는 이유는 문서 주의사항대로
    "최대 낙폭 −8%"가 3년치인지 3주치인지 밝히지 않으면 오해를 부르기 때문이다.
    """

    mdd: float
    peak_date: date | None
    trough_date: date | None
    observed_days: int


@dataclass(frozen=True, slots=True)
class RateExposure:
    """§3.5 금리민감도. 회귀 계수가 아니라 섹터 성격 기반 근사다 — 프롬프트에
    "추정치이며 실제 반응과 다를 수 있음"을 반드시 함께 넣어야 한다.
    """

    score: float  # S
    level: RateSensitivity


@dataclass(frozen=True, slots=True)
class Finding:
    """§3.7. `exceedance`는 임계를 넘어선 폭이며 동률 정렬 키로 쓴다."""

    id: str
    category: FindingCategory
    severity: Severity
    metric: str
    value: float
    threshold: float
    exceedance: float


@dataclass(frozen=True, slots=True)
class RiskAssessment:
    trade_date: date
    concentration: Concentration
    cash_weight: float
    top_sector: str | None
    top_sector_weight: float
    drawdown: Drawdown
    rate_exposure: RateExposure
    findings: tuple[Finding, ...]
    # 히스토리가 `min_history_days` 미만이면 아래 넷이 모두 None이다. 예외를 던지지
    # 않는 이유는 집중도·현금·금리민감도 진단은 그래도 유효해서, 신규 포트폴리오에도
    # Portfolio Doctor가 절반은 답할 수 있어야 하기 때문이다. 409로 끊을지는 API 판단.
    volatility: Volatility | None
    diversification: Diversification | None
    risk_score: float | None
    risk_level: RiskLevel | None
    insufficient_history: str | None = None
    # §3.4 β = Cov(r_p, r_m) / Var(r_m). `benchmark`를 안 넘겼거나 벤치마크까지 포함한
    # 공통 거래일이 짧으면 None이다. §3.6 risk_score에는 들어가지 않는 보고용 지표다.
    beta: float | None = None
    # §3.5 w_large. `market_cap_ranks`를 안 넘기면 None — 0.0과 구분해야 한다
    # ("대형주 없음"과 "안 세어 봤음"은 다른 말이다).
    large_cap_weight: float | None = None
    # §3.5 w_growth. `pbr_percentiles`를 안 넘기면 None이고, 위와 같은 이유로 0.0과
    # 구분한다. §3.6 risk_score에는 들어가지 않는다 — 가중 5개 구성요소에 성장은 없다.
    growth_weight: float | None = None


# ── 정규화 ──────────────────────────────────────────────────
def _normalize(x: float, lo: float, hi: float) -> float:
    """`s(x; lo, hi) = clip((x−lo)/(hi−lo), 0, 1) × 100`.

    역방향 지표(`lo > hi`)에도 특수 분기 없이 그대로 쓴다. 분모가 음수가 되면 비율의
    부호가 뒤집히고 clip 방향까지 함께 뒤집혀 결과적으로 맞는 점수가 나온다.
    """
    return min(max((x - lo) / (hi - lo), 0.0), 1.0) * 100.0


def _level(score: float, previous: RiskLevel | None) -> RiskLevel:
    """§3.6 등급 + 히스테리시스.

    이전 등급을 *인자로* 받는다. 엔진이 순수해서 상태를 못 들고 있는 것도 이유지만,
    직전 *점수*가 아니라 직전 *등급*을 받는 편이 옳다: 문서의 규칙이 "이전 등급 대비
    ±3점"이고, 사용자가 실제로 본 값도 등급이다. 점수를 저장하면 등급이 안 바뀐 채
    점수만 조금씩 흘러가다가 어느 날 두 칸이 한꺼번에 뒤집힌다.

    `previous`가 None(첫 진단)이면 히스테리시스 없이 원점수 구간을 그대로 쓴다.
    """
    naive = sum(score >= cut for cut in _LEVEL_CUTS)
    if previous is None:
        return _LEVELS[naive]

    index = _LEVELS.index(previous)
    # 경계를 한 칸씩 넘는다. 한 번에 두 등급을 건너뛰는 경우도 각 경계를 따로 본다.
    while naive > index and score >= _LEVEL_CUTS[index] + _HYSTERESIS:
        index += 1
    while naive < index and score <= _LEVEL_CUTS[index - 1] - _HYSTERESIS:
        index -= 1
    return _LEVELS[index]


# ── §3.2·§3.3 수익률 정렬 ──────────────────────────────────
def _aligned_returns(
    symbols: Sequence[str], prices: Mapping[str, Mapping[date, float]]
) -> tuple[tuple[date, ...], np.ndarray]:
    """보유 종목 *전부*가 종가를 가진 날짜의 교집합에서만 일별 수익률을 뽑는다.

    종목마다 상장·편입 시점이 달라 히스토리 길이가 다르고, 국내 거래일에도 거래정지
    같은 구멍이 있다. 종목별로 다른 날짜 축을 쓰면 공분산·상관계수가 의미를 잃으므로
    표본이 짧아지는 쪽을 택한다. 교집합에 구멍이 있으면 그 수익률은 하루가 아니라
    며칠치를 담는데, 이를 메우려면 가격을 만들어내야 하므로 그냥 둔다.
    """
    if not symbols:
        return (), np.empty((0, 0))
    common = set(prices.get(symbols[0], {}))
    for symbol in symbols[1:]:
        common &= set(prices.get(symbol, {}))
    days = tuple(sorted(common))
    if len(days) < 2:
        return days, np.empty((0, len(symbols)))
    levels = np.array([[prices[s][d] for s in symbols] for d in days], dtype=float)
    if not np.all(levels > 0):
        # 0·음수 종가는 수익률을 nan/inf로 만들고 그 nan이 risk_score까지 번진다.
        return days, np.empty((0, len(symbols)))
    return days, levels[1:] / levels[:-1] - 1.0


def _average_correlation(weights: np.ndarray, sigma: np.ndarray, rho: np.ndarray) -> float | None:
    """ρ̄ = Σ_{i≠j} ŵ_i ŵ_j ρ_ij / Σ_{i≠j} ŵ_i ŵ_j.

    변동성이 0인 종목(전 구간 종가 동일 — 거래정지 등)은 ρ가 정의되지 않으므로 그
    쌍을 분자·분모에서 함께 뺀다. 남는 쌍이 없으면 None.
    """
    numerator = denominator = 0.0
    for i in range(len(weights)):
        for j in range(len(weights)):
            if i == j or sigma[i] <= 0.0 or sigma[j] <= 0.0:
                continue
            pair = weights[i] * weights[j]
            numerator += pair * float(rho[i, j])
            denominator += pair
    return numerator / denominator if denominator else None


def _diversification_ratio(weighted_sigma: float, sigma_p: float) -> float:
    """DR = Σŵσ / σ_p. 분모가 0이 되는 두 경우를 구분한다.

    - Σŵσ도 0: 전 종목이 상수 종가라 분산할 위험 자체가 없다 → 1.0.
    - Σŵσ > 0인데 σ_p = 0: 보유가 서로를 완전히 상계한 완전 헤지 → 분산효과 무한대.
      `inf`를 그대로 둔다. `_normalize`가 `DR ≥ 1.50`을 0점으로 절단하므로 점수는
      "분산 실패 없음"으로 정확히 떨어진다. 실데이터에서는 60거래일 내내 가중
      수익률이 정확히 0이어야 해서 사실상 나오지 않고, 아주 작은 양수가 되어
      큰 유한값으로 잡힌다.
    """
    if weighted_sigma <= 0.0:
        return 1.0
    return weighted_sigma / sigma_p if sigma_p > 0.0 else math.inf


def _beta(
    symbols: Sequence[str],
    weights: np.ndarray,
    prices: Mapping[str, Mapping[date, float]],
    benchmark: Mapping[date, float],
) -> tuple[float | None, str | None]:
    """§3.4 `β = Cov(r_p, r_m) / Var(r_m)`. 반환은 `(β, 계산 못 한 이유)`.

    벤치마크를 *보유 종목 하나처럼* 취급해 `_aligned_returns`에 함께 넘긴다. 지수는
    거래일이 길고 종목은 상장·편입 시점 때문에 짧다. r_p와 r_m을 각자의 날짜 축에서
    뽑으면 공분산이 서로 다른 날을 짝지어 버리므로, 정렬 규칙을 두 벌 만드는 대신
    기존 교집합 로직을 그대로 쓴다. 벤치마크가 섞인 교집합은 §3.2·§3.3이 쓰는
    교집합보다 짧을 수 있어서 호출도 따로 한다 — 베타 때문에 변동성 표본이 줄면
    안 된다.

    표본 규칙은 §3.2 변동성과 같다(공통 거래일 전체, `min_history_days` 이상).
    문서가 말하는 "60거래일"이 곧 `settings.min_history_days`다.
    """
    _, joint = _aligned_returns((*symbols, _BENCHMARK), {**prices, _BENCHMARK: benchmark})
    observed = len(joint)
    if observed < settings.min_history_days:
        return None, (
            f"벤치마크 포함 공통 거래일 {observed}일 — {settings.min_history_days}일 "
            f"미만이라 베타를 추정하지 않는다"
        )
    market = joint[:, -1]
    variance = float(market.var(ddof=1))
    if variance <= 0.0:
        # 지수가 전 구간 보합. 분모가 0이면 β는 정의되지 않는다 — inf로 두면 "시장보다
        # 무한히 민감"이라는 읽을 수 없는 값이 API로 나간다.
        return None, "벤치마크 수익률의 분산이 0이라 베타가 정의되지 않는다"
    portfolio = joint[:, :-1] @ weights
    return float(np.cov(portfolio, market, ddof=1)[0, 1] / variance), None


def _large_cap_weight(snapshot: PortfolioSnapshot, ranks: Mapping[str, int]) -> float | None:
    """§3.5 `w_large = Σ_{시가총액 순위 ≤ 100} ŵ_i`.

    시가총액이 아니라 *순위*를 받는다. 100위는 시장 전체 횡단면에서만 정해지는 값인데
    엔진은 포트폴리오만 본다 — 보유 종목의 시가총액을 전부 줘도 그게 시장에서 몇 위인지
    는 알 수 없다. 순위는 전 종목이 보이는 곳에서 한 번 매겨(`ORDER BY market_cap DESC`)
    넘기는 것이 맞고, 그러면 종목 수와 무관하게 인자도 작아진다.

    빈 매핑은 "호출자가 순위를 안 줬다"로 본다 → None. 매핑이 있는데 특정 종목이
    빠져 있으면 100위 밖으로 본다 — 전 종목 순위표에 없다는 건 그런 뜻이다.
    """
    if not ranks:
        return None
    return sum(
        h.stock_weight
        for h in snapshot.holdings
        if ranks.get(h.symbol, _LARGE_CAP_RANK + 1) <= _LARGE_CAP_RANK
    )


def _growth_weight(
    snapshot: PortfolioSnapshot, percentiles: Mapping[str, float]
) -> float | None:
    """§3.5 `w_growth = Σ_{i ∈ 성장} ŵ_i`, 성장 판정은 `PBR_i ≥ 시장 상위 30% 분위`.

    PBR이 아니라 *백분위*를 받는다. 이유는 `_large_cap_weight`가 시가총액 대신 순위를
    받는 것과 같다 — "상위 30%"는 시장 횡단면에서만 정해지는데 엔진은 포트폴리오만
    본다. 보유 종목의 PBR을 전부 줘도 그게 시장에서 몇 분위인지는 알 수 없다.

    **백분위의 모집단은 호출자가 정하고, 엔진은 그것을 그대로 믿는다.** 지금 그 모집단은
    "PBR을 계산할 수 있는 상장 종목 전체"다 — `instruments.market_cap`과
    `financial_annual.total_equity`가 둘 다 있고 자본총계가 양수인 종목
    (`PBR = market_cap / total_equity`). 자본잠식(자본총계 ≤ 0)은 PBR이 음수나 발산이
    되어 순서를 매길 수 없으므로 모집단에서 뺀다. 값은 0.0~1.0이고 1.0이 가장 비싸다.

    **매핑에 없는 보유 종목은 "성장 아님"으로 보고, 분모에서 빼지 않는다.** §3.5의
    식이 `Σ ŵ_i`라 재정규화할 분모가 애초에 없고, 짝인 `w_large`도 순위표에 없는 종목을
    같은 방식으로 다룬다. 대신 부작용이 하나 있다: 미제출·자본잠식으로 PBR을 모르는
    비중이 크면 `w_growth`가 실제보다 낮게 나오고, §3.7 `style_tilt`의 `≤ 0.15` 쪽이
    먼저 걸린다. severity가 info인 이유이기도 하다. 커버리지를 임계로 막지 않는 것은
    문서에 그런 규칙이 없어서다 — 필요해지면 호출자가 커버리지를 재서 넘기지 않으면 된다.

    빈 매핑은 "호출자가 백분위를 안 줬다"로 본다 → None. 0.0("성장주 없음")과 구분해야
    한다.
    """
    if not percentiles:
        return None
    return sum(
        h.stock_weight
        for h in snapshot.holdings
        if percentiles.get(h.symbol, 0.0) >= _GROWTH_PERCENTILE
    )


def _drawdown(value_series: Sequence[tuple[date, float]]) -> Drawdown:
    """§3.4 `MDD = min_t (V_t / peak_t − 1)`.

    총자산이 0 이하인 날(시드머니 입금 전)은 peak 분모가 될 수 없어 건너뛴다.
    """
    positive = [(day, value) for day, value in value_series if value > 0]
    peak = trough = None
    peak_value = 0.0
    running_peak_date: date | None = None
    mdd = 0.0
    for day, value in positive:
        if value > peak_value:
            peak_value, running_peak_date = value, day
        drawdown = value / peak_value - 1.0
        if drawdown < mdd:
            mdd, peak, trough = drawdown, running_peak_date, day
    return Drawdown(mdd=mdd, peak_date=peak, trough_date=trough, observed_days=len(positive))


def _rate_exposure(snapshot: PortfolioSnapshot) -> RateExposure:
    """§3.5 `S = Σ_i ŵ_i × s(sector_i)`."""
    score = sum(
        holding.stock_weight * _RATE_SENSITIVITY.get(holding.sector, 0.0)
        for holding in snapshot.holdings
    )
    if score >= 0.60:
        level = RateSensitivity.HIGH
    elif score >= 0.25:
        level = RateSensitivity.MODERATE
    else:
        level = RateSensitivity.LOW
    return RateExposure(score=score, level=level)


# ── §3.7 findings ──────────────────────────────────────────
_SEVERITY_RANK: Mapping[Severity, int] = {Severity.HIGH: 2, Severity.MEDIUM: 1, Severity.INFO: 0}


def _tiered(
    finding_id: str,
    category: FindingCategory,
    metric: str,
    value: float,
    tiers: Sequence[tuple[float, Severity]],
    *,
    reverse: bool = False,
) -> Finding | None:
    """임계 표에서 가장 심한 단계 하나만 만든다.

    `tiers`는 심한 순서로 준다. `reverse=True`면 "임계 이하"가 발생 조건이다
    (`DR ≤ 1.10`, `w_c ≤ 0.02`처럼).
    """
    for threshold, severity in tiers:
        exceeded = value <= threshold if reverse else value >= threshold
        if exceeded:
            return Finding(
                id=finding_id,
                category=category,
                severity=severity,
                metric=metric,
                value=value,
                threshold=threshold,
                exceedance=threshold - value if reverse else value - threshold,
            )
    return None


def _findings(
    concentration: Concentration,
    top_sector_weight: float,
    cash_weight: float,
    volatility: Volatility | None,
    diversification: Diversification | None,
    rate_exposure: RateExposure,
    growth_weight: float | None,
) -> tuple[Finding, ...]:
    """문서 §3.7 표를 그대로 옮겼다."""
    candidates = [
        _tiered(
            "ticker_concentration",
            FindingCategory.CONCENTRATION,
            "top1",
            concentration.top1,
            ((0.40, Severity.HIGH), (0.25, Severity.MEDIUM)),
        ),
        _tiered(
            "sector_concentration",
            FindingCategory.CONCENTRATION,
            "top_sector_weight",
            top_sector_weight,
            ((0.50, Severity.HIGH), (0.35, Severity.MEDIUM)),
        ),
        _tiered(
            "liquidity",
            FindingCategory.LIQUIDITY,
            "cash_weight",
            cash_weight,
            ((0.02, Severity.MEDIUM), (0.05, Severity.INFO)),
            reverse=True,
        ),
    ]
    if volatility is not None:
        candidates.append(
            _tiered(
                "volatility",
                FindingCategory.VOLATILITY,
                "sigma_p",
                volatility.portfolio,
                ((0.40, Severity.HIGH), (0.30, Severity.MEDIUM)),
            )
        )
    # "숫자상 분산, 실질은 집중"만 잡는 조건이라 N_eff 게이트가 붙는다. 종목이 하나면
    # N_eff = 1이므로 여기서 자동으로 걸러진다 — DR은 N=1에서 정의상 1.0이지만
    # "분산에 실패했다"고 지적할 대상이 아니다.
    if diversification is not None and concentration.n_eff >= 3:
        candidates.append(
            _tiered(
                "correlation",
                FindingCategory.CORRELATION,
                "diversification_ratio",
                diversification.ratio,
                ((1.10, Severity.HIGH), (1.20, Severity.MEDIUM)),
                reverse=True,
            )
        )
    # `w_growth ≥ 0.70` 또는 `≤ 0.15`. 양쪽으로 열린 유일한 항목이라 `_tiered`를 두 번
    # 부른다. 두 구간이 겹치지 않아 동시에 걸릴 수 없고, 따라서 id가 중복되지 않는다.
    # 성장으로 쏠린 것도 가치로 쏠린 것도 "한쪽에 몰렸다"는 같은 지적이라 id를 나누지
    # 않는다 — 어느 쪽인지는 `value`와 `threshold`가 말한다.
    if growth_weight is not None:
        candidates += [
            _tiered(
                "style_tilt",
                FindingCategory.STYLE_TILT,
                "w_growth",
                growth_weight,
                ((0.70, Severity.INFO),),
            ),
            _tiered(
                "style_tilt",
                FindingCategory.STYLE_TILT,
                "w_growth",
                growth_weight,
                ((0.15, Severity.INFO),),
                reverse=True,
            ),
        ]
    if rate_exposure.level is RateSensitivity.HIGH:
        candidates.append(
            Finding(
                id="macro_exposure",
                category=FindingCategory.MACRO_EXPOSURE,
                severity=Severity.INFO,
                metric="rate_sensitivity_score",
                value=rate_exposure.score,
                threshold=0.60,
                exceedance=rate_exposure.score - 0.60,
            )
        )
    found = [f for f in candidates if f is not None]
    found.sort(key=lambda f: (_SEVERITY_RANK[f.severity], f.exceedance), reverse=True)
    return tuple(found)


# ── 진단 ────────────────────────────────────────────────────
def assess(
    snapshot: PortfolioSnapshot,
    prices: Mapping[str, Mapping[date, float]],
    *,
    value_series: Sequence[tuple[date, float]] = (),
    previous_level: RiskLevel | None = None,
    benchmark: Mapping[date, float] | None = None,
    market_cap_ranks: Mapping[str, int] | None = None,
    pbr_percentiles: Mapping[str, float] | None = None,
) -> RiskAssessment:
    """스냅샷 하나에 대한 위험 진단(§3.2–§3.7).

    `prices`는 `Ledger.prices`를 그대로 넘기면 된다. `value_series`는 MDD용 총자산
    시계열로 `[(day, PortfolioEngine.snapshot(day).total_value) for day in ...]` 형태다.
    비워 두면 MDD가 0이 된다.

    `previous_level`은 히스테리시스용 직전 등급이다(`_level` docstring 참고).

    `benchmark`는 §3.4 베타용 벤치마크 지수 종가다. `prices`의 한 종목과 같은 모양
    (`{날짜: 종가}`)이고 `index_daily`의 `index_code = 'KOSPI'` 행이 그대로 들어간다.
    `market_cap_ranks`는 §3.5 대형 판정용 시장 전체 시가총액 순위(`{종목코드: 순위}`,
    1위가 최대)다 — 순위를 받는 이유는 `_large_cap_weight` docstring에 있다.
    `pbr_percentiles`는 §3.5 성장 판정용 시장 전체 PBR 백분위(`{종목코드: 0.0~1.0}`,
    1.0이 최고가)다. 모집단 정의와 미수록 종목 처리는 `_growth_weight` docstring에 있다.
    셋 다 안 넘기면 해당 지표만 None이고 나머지 진단은 그대로 나온다.
    """
    concentration = snapshot.concentration()
    symbols = tuple(h.symbol for h in snapshot.holdings)
    weights = np.array([h.stock_weight for h in snapshot.holdings], dtype=float)

    by_sector: dict[str, float] = {}
    for holding in snapshot.holdings:
        by_sector[holding.sector] = by_sector.get(holding.sector, 0.0) + holding.stock_weight
    top_sector, top_sector_weight = (
        max(by_sector.items(), key=lambda kv: kv[1]) if by_sector else (None, 0.0)
    )

    volatility: Volatility | None = None
    diversification: Diversification | None = None
    # 지표마다 막히는 이유가 다르다(변동성은 종목 히스토리, 베타는 벤치마크와의 교집합).
    # 하나만 담을 수 있는 문자열로 두면 나중에 온 이유가 먼저 온 이유를 덮는다.
    reasons: list[str] = []

    _, returns = _aligned_returns(symbols, prices)
    observed = len(returns)
    if observed < settings.min_history_days:
        # 추정하지 않고 거절한다. 20일 표본으로 뽑은 연율 변동성은 숫자만 그럴듯하다.
        reasons.append(
            f"공통 거래일 {observed}일 — {settings.min_history_days}일 미만이라 "
            f"변동성·상관을 추정하지 않는다 (보유 {len(symbols)}종목)"
        )
    else:
        annualize = math.sqrt(settings.trading_days_per_year)
        sigma = returns.std(axis=0, ddof=1) * annualize
        covariance = np.atleast_2d(np.cov(returns, rowvar=False, ddof=1))
        sigma_p = float(math.sqrt(max(weights @ covariance @ weights, 0.0)) * annualize)
        volatility = Volatility(
            portfolio=sigma_p,
            by_symbol={s: float(v) for s, v in zip(symbols, sigma, strict=True)},
            observed_days=observed,
        )
        with np.errstate(invalid="ignore", divide="ignore"):
            rho = np.atleast_2d(np.corrcoef(returns, rowvar=False))
        weighted_sigma = float(weights @ sigma)
        diversification = Diversification(
            ratio=_diversification_ratio(weighted_sigma, sigma_p),
            avg_correlation=_average_correlation(weights, sigma, rho),
            symbols=symbols,
            correlations=tuple(tuple(float(v) for v in row) for row in rho),
        )

    rate_exposure = _rate_exposure(snapshot)
    growth_weight = _growth_weight(snapshot, pbr_percentiles or {})

    # 벤치마크를 안 넘겼으면 베타를 요구하지 않은 것이므로 이유도 남기지 않는다.
    # 보유가 없으면 r_p 자체가 없어서 계산 대상이 아니다.
    beta: float | None = None
    if benchmark and symbols:
        beta, beta_reason = _beta(symbols, weights, prices, benchmark)
        if beta_reason is not None:
            reasons.append(beta_reason)

    risk_score: float | None = None
    risk_level: RiskLevel | None = None
    if volatility is not None and diversification is not None:
        values = {
            "hhi": concentration.hhi,
            "sector_hhi": concentration.sector_hhi,
            "volatility": volatility.portfolio,
            "diversification_ratio": diversification.ratio,
            "cash_weight": snapshot.cash_weight,
        }
        risk_score = (
            sum(
                weight * _normalize(values[name], lo, hi)
                for name, lo, hi, weight in _SCORE_COMPONENTS
            )
            / 100.0
        )
        risk_level = _level(risk_score, previous_level)

    return RiskAssessment(
        trade_date=snapshot.trade_date,
        concentration=concentration,
        cash_weight=snapshot.cash_weight,
        top_sector=top_sector,
        top_sector_weight=top_sector_weight,
        drawdown=_drawdown(value_series),
        rate_exposure=rate_exposure,
        findings=_findings(
            concentration,
            top_sector_weight,
            snapshot.cash_weight,
            volatility,
            diversification,
            rate_exposure,
            growth_weight,
        ),
        volatility=volatility,
        diversification=diversification,
        risk_score=risk_score,
        risk_level=risk_level,
        insufficient_history=" / ".join(reasons) or None,
        beta=beta,
        large_cap_weight=_large_cap_weight(snapshot, market_cap_ranks or {}),
        growth_weight=growth_weight,
    )
