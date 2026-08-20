"""Attribution Engine — 성과 요인 분해(§4).

§4.1 종목 기여도는 `PortfolioEngine.daily_returns()`가 이미 `w_{i,t-1} × r_{i,t}`로
계산하고 있어 여기서 다시 만들지 않는다. 이 모듈이 더하는 것은 §4.2 Brinson–Fachler
분해, §4.3 Carino 링킹, 그리고 §5.1 이벤트 매칭 신뢰도뿐이다.

■ 벤치마크를 어디서 얻는가 — 편입 종목의 시가총액으로 직접 합성한다

§4.2는 섹터 벤치마크 수익률을 KRX 업종지수에서 얻되, *업종지수가 없는 섹터는 편입
종목의 시가총액 가중 수익률로 대체하고 `proxy: true`로 표기하라*고 정한다. 지금
`index_daily`에는 KOSPI 484행만 있고 업종지수는 0행이다. 즉 업종지수를 새로 적재해도
우리 섹터 어휘(`ingest/ksic_sectors.json`의 반도체·IT하드웨어·은행…)와 KRX 업종명
(전기전자·금융…)이 1:1로 붙지 않아 반도체와 IT하드웨어가 같은 전기전자 하나로
뭉개진다. 그러면 `w_j^p`와 `w_j^b`가 서로 다른 축 위에서 계산되어 Brinson 항등식이
성립할 근거 자체가 사라진다.

그래서 전 섹터를 §4.2가 이미 허용한 프록시 경로로 통일한다. 벤치마크는 시장 전체이며
섹터 비중은 Σ시가총액, 섹터 수익률은 그 시가총액 가중 수익률이다. 포트폴리오와
벤치마크가 `Instrument.sector` 하나만 쓰므로 두 비중이 같은 키 위에서 만난다.
결과에는 모든 섹터가 `proxy=True`로 표기된다 — 업종지수가 적재되면 그 섹터만
`proxy=False`로 바꾸면 되고, 산식은 그대로다.

■ 항등식(§6.3)

    market + sector + selection = r_p

Brinson 항등식은 `Σ_j w_j^p = 1`일 때만 성립한다. 현금은 수익률 0·벤치마크 비중 0인
섹터로 넣어 비중 합을 1로 맞춘다(§4.1이 "현금 수익률 0, w_c 포함해 Σw = 1"이라 한
것과 같은 처리다). 벤치마크에 없는 섹터(`w_j^b = 0`)는 `r_j^b`가 무엇이든
`A_j + I_j = w_j^p (r_j^p − r_b)`로 정리되어 항등식에 영향을 주지 않는다.
"""

from __future__ import annotations

import math
from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date

__all__ = [
    "AttributionResult",
    "BenchmarkDay",
    "CASH_SECTOR",
    "ContributorRow",
    "EventMatch",
    "EventRecord",
    "SectorEffect",
    "attribute",
    "carino_factors",
    "match_events",
]

#: 현금을 담는 가상 섹터. 벤치마크 비중은 항상 0이다.
CASH_SECTOR = "현금"

#: 0 나눗셈 방지 임계값. §4.3 코드가 쓰는 값과 같다.
_EPS = 1e-12

#: §5.1 이벤트 매칭 신뢰도 하한. 이보다 낮으면 근거로 붙이지 않는다.
_MIN_CONFIDENCE = 0.35

#: 기여 종목 하나에 붙일 이벤트 수 상한.
_MAX_EVENTS = 3


# ── 입력 ──────────────────────────────────────────────────────────────────────
@dataclass(frozen=True, slots=True)
class BenchmarkDay:
    """벤치마크 하루치. 섹터별 비중과 수익률이 이미 합성된 상태로 들어온다.

    `weights`는 합이 1이어야 한다 — 벤치마크는 전액 투자된 시장 전체다.
    """

    trade_date: date
    weights: Mapping[str, float]
    returns: Mapping[str, float]

    @property
    def total_return(self) -> float:
        """r_b. 섹터 수익률의 비중 가중합이다."""
        return sum(w * self.returns.get(sector, 0.0) for sector, w in self.weights.items())


@dataclass(frozen=True, slots=True)
class EventRecord:
    """§5.1 매칭 대상 이벤트. `events` 테이블 한 행을 그대로 옮긴 것이다."""

    event_id: str
    event_type: str
    title: str
    event_date: date
    ticker: str | None = None
    document_id: str | None = None
    importance: float = 0.5


# ── 출력 ──────────────────────────────────────────────────────────────────────
@dataclass(frozen=True, slots=True)
class EventMatch:
    """기여 종목에 붙은 근거 하나. `matched_confidence`는 §5.1로 계산된 값이다."""

    event_id: str
    event_type: str
    title: str
    event_date: date
    matched_confidence: float
    citation_id: str | None = None


@dataclass(frozen=True, slots=True)
class ContributorRow:
    """§4.1 기여도 한 줄. `weight`는 기간 시작 시점(w_{i,t-1}) 비중이다."""

    ticker: str
    name: str
    sector: str
    weight: float
    return_rate: float
    contribution: float
    held_at_start: bool
    events: tuple[EventMatch, ...] = ()


@dataclass(frozen=True, slots=True)
class SectorEffect:
    """섹터 j의 Brinson 3항. `proxy`는 벤치마크 수익률이 합성값임을 뜻한다."""

    sector: str
    portfolio_weight: float
    benchmark_weight: float
    portfolio_return: float
    benchmark_return: float
    allocation: float
    selection: float
    interaction: float
    proxy: bool = True


@dataclass(frozen=True, slots=True)
class AttributionResult:
    """§4 전체 결과. 항등식은 생성 시점에 이미 검증되었다."""

    start: date
    end: date
    trading_days: int
    portfolio_return: float
    benchmark_return: float
    excess_return: float
    market: float
    sector: float
    selection: float
    contributors: tuple[ContributorRow, ...] = ()
    sector_effects: tuple[SectorEffect, ...] = ()
    notes: tuple[str, ...] = field(default_factory=tuple)


# ── §4.3 Carino 링킹 ──────────────────────────────────────────────────────────
def carino_factors(returns: Sequence[float]) -> tuple[list[float], float]:
    """일별 `k_t`와 기간 `K`. 단순 합산 오차를 부동소수점 한계까지 없앤다.

    `r_t = 0`이면 `k_t = 1`이다 — `ln(1+r)/r`의 극한값이고, 0 나눗셈도 피한다.
    """
    factors = [1.0 if abs(r) < _EPS else math.log1p(r) / r for r in returns]
    total = math.prod(1.0 + r for r in returns) - 1.0
    scale = 1.0 if abs(total) < _EPS else math.log1p(total) / total
    return factors, scale


def _excess_carino(
    port: Sequence[float], bench: Sequence[float]
) -> tuple[list[float], float, float, float]:
    """초과수익용 링킹 계수.

    §4.3의 Carino는 `Σ_i C_i = R_p`를 보장하지만 Brinson 효과에 그대로 쓰면
    `Σ_t r_{b,t} k_t/K ≠ R_b`가 되어 `market + sector + selection = r_p`가 깨진다.
    분자를 포트폴리오·벤치마크 로그수익률의 *차*로 두면

        Σ_t (r_{p,t} − r_{b,t}) k_t = ln(1+R_p) − ln(1+R_b) = K (R_p − R_b)

    가 항등적으로 성립해 초과수익이 정확히 보존된다. 하루짜리 기간에서는 k/K = 1이라
    링킹이 자동으로 사라진다(§4.3이 `1d`는 건너뛰라고 한 것과 같은 결과다).
    """
    total_p = math.prod(1.0 + r for r in port) - 1.0
    total_b = math.prod(1.0 + r for r in bench) - 1.0

    def coefficient(rp: float, rb: float) -> float:
        gap = rp - rb
        if abs(gap) < _EPS:
            return 1.0
        return (math.log1p(rp) - math.log1p(rb)) / gap

    factors = [coefficient(rp, rb) for rp, rb in zip(port, bench, strict=True)]
    scale = coefficient(total_p, total_b)
    return factors, scale, total_p, total_b


# ── §5.1 이벤트 매칭 ──────────────────────────────────────────────────────────
def _entity_score(
    event: EventRecord, ticker: str, sector: str, sectors: Mapping[str, str]
) -> float:
    """§5.1 엔티티 점수. 직접 언급 1.0 · 같은 섹터 0.5 · 매크로 0.3."""
    if event.ticker is None:
        return 0.3
    if event.ticker == ticker:
        return 1.0
    if sectors.get(event.ticker) == sector:
        return 0.5
    return 0.0


def match_events(
    events: Iterable[EventRecord],
    *,
    ticker: str,
    sector: str,
    anchor: date,
    trading_days: Sequence[date],
    sectors: Mapping[str, str],
) -> tuple[EventMatch, ...]:
    """종목 하나에 붙는 근거 목록. 붙일 게 없으면 빈 튜플이다.

    `anchor`는 기간 안에서 그 종목이 가장 크게 움직인 날이며, 근접도는 그 날과
    이벤트 사이의 *거래일* 간격으로 잰다(§5.1의 τ = 1 거래일). 달력 일수로 재면
    주말을 사이에 둔 금요일 공시가 부당하게 깎인다.

    ponytail: §5.1의 semantic(임베딩 코사인) 항은 빠져 있다. `events` 테이블에
    임베딩이 없고 이 기능만을 위해 임베딩 파이프라인을 놓을 이유가 없다. 대신
    계산 가능한 두 항(recency 0.40 · entity 0.30)을 0.70으로 재정규화해 값의
    범위를 유지한다. 이벤트 임베딩이 적재되면 세 항 원래 가중치로 되돌린다.
    """
    index = {day: i for i, day in enumerate(trading_days)}
    anchor_at = index.get(anchor)
    matches: list[EventMatch] = []
    for event in events:
        entity = _entity_score(event, ticker, sector, sectors)
        if entity == 0.0:
            continue
        at = index.get(event.event_date)
        if at is None or anchor_at is None:
            continue
        recency = math.exp(-abs(at - anchor_at))
        confidence = (0.40 * recency + 0.30 * entity) / 0.70
        if confidence < _MIN_CONFIDENCE:
            continue
        matches.append(
            EventMatch(
                event_id=event.event_id,
                event_type=event.event_type,
                title=event.title,
                event_date=event.event_date,
                matched_confidence=round(confidence, 4),
                citation_id=f"cit_{event.document_id}" if event.document_id else None,
            )
        )
    matches.sort(key=lambda m: (-m.matched_confidence, m.event_date))
    return tuple(matches[:_MAX_EVENTS])


# ── §4.2 Brinson–Fachler ──────────────────────────────────────────────────────
def _sector_split(
    weights: Mapping[str, float], returns: Mapping[str, float], sectors: Mapping[str, str]
) -> tuple[dict[str, float], dict[str, float]]:
    """종목 단위 비중·수익률을 섹터 단위로 접는다.

    섹터 수익률은 그 섹터 안에서의 비중 가중 평균이다. 비중이 0인 섹터는 수익률을
    0으로 둔다 — 어차피 모든 항에 0이 곱해진다.
    """
    weight: dict[str, float] = defaultdict(float)
    weighted: dict[str, float] = defaultdict(float)
    for symbol, w in weights.items():
        sector = sectors.get(symbol, CASH_SECTOR)
        weight[sector] += w
        weighted[sector] += w * returns.get(symbol, 0.0)
    rate = {s: (weighted[s] / weight[s] if abs(weight[s]) > _EPS else 0.0) for s in weight}
    return dict(weight), rate


def _brinson(
    port_weight: Mapping[str, float],
    port_return: Mapping[str, float],
    bench_weight: Mapping[str, float],
    bench_return: Mapping[str, float],
    benchmark_total: float,
) -> dict[str, tuple[float, float, float]]:
    """섹터별 (Allocation, Selection, Interaction).

    벤치마크에 없는 섹터는 `r_j^b`를 관측할 수 없다. 대수적으로는
    `A_j + I_j = w_j^p (r_j^p − r_b)`로 정리되어 `r_j^b`가 무엇이든 결과가 같지만,
    Allocation과 Selection을 따로 내보내야 하므로 관행대로 `r_j^b = r_b`로 둔다.
    이러면 그 섹터의 Allocation이 0이 되고 초과분이 전부 selection으로 간다 —
    "벤치마크에 없는 걸 골랐다"는 판단은 배분이 아니라 선택이라는 뜻이다.
    """
    effects: dict[str, tuple[float, float, float]] = {}
    for sector in set(port_weight) | set(bench_weight):
        wp = port_weight.get(sector, 0.0)
        wb = bench_weight.get(sector, 0.0)
        rp = port_return.get(sector, 0.0)
        rb = bench_return.get(sector, benchmark_total) if wb > _EPS else benchmark_total
        effects[sector] = (
            (wp - wb) * (rb - benchmark_total),
            wb * (rp - rb),
            (wp - wb) * (rp - rb),
        )
    return effects


def _assert_identity(market: float, sector: float, selection: float, total: float) -> None:
    """§6.3 항등식. 어긋나면 계산이 틀린 것이므로 응답을 만들지 않는다."""
    gap = abs(market + sector + selection - total)
    if gap > 1e-9:
        raise ValueError(
            f"§6.3 항등식 위반: market({market}) + sector({sector}) + "
            f"selection({selection}) ≠ portfolio_return({total}), 오차 {gap}"
        )


# ── 진입점 ────────────────────────────────────────────────────────────────────
def attribute(
    *,
    trading_days: Sequence[date],
    portfolio_weights: Sequence[Mapping[str, float]],
    portfolio_returns: Sequence[Mapping[str, float]],
    benchmark: Sequence[BenchmarkDay],
    sectors: Mapping[str, str],
    names: Mapping[str, str] | None = None,
    events: Iterable[EventRecord] = (),
) -> AttributionResult:
    """기간 성과를 시장·섹터·선택으로 분해한다(§4).

    `portfolio_weights[t]`는 **t 시작 시점**, 즉 전일 종가 기준 비중이다(§4.1). 각 날의
    비중 합이 1보다 작으면 모자란 만큼을 현금으로 채워 항등식 전제를 맞춘다. 기간
    중간에 산 종목은 그 날 비중이 0이라 자연히 기여도 0이 되고, 판 종목도 팔기 전까지의
    기여도를 그대로 유지한다 — 어느 쪽도 목록에서 빠지지 않는다.

    `benchmark`는 `trading_days`와 같은 길이·같은 순서여야 한다.
    """
    if not trading_days:
        raise ValueError("거래일이 없어 분해할 구간이 없습니다.")
    if not (len(trading_days) == len(portfolio_weights) == len(portfolio_returns)):
        raise ValueError("거래일과 비중·수익률 계열의 길이가 다릅니다.")
    if len(benchmark) != len(trading_days):
        raise ValueError("벤치마크 계열이 거래일과 어긋납니다.")

    names = names or {}
    events = tuple(events)

    daily_port: list[float] = []
    daily_bench: list[float] = []
    daily_effects: list[dict[str, tuple[float, float, float]]] = []
    daily_contrib: list[dict[str, float]] = []
    sector_view: dict[str, tuple[float, float, float, float]] = {}

    for day_weights, day_returns, bench in zip(
        portfolio_weights, portfolio_returns, benchmark, strict=True
    ):
        weights = dict(day_weights)
        cash = 1.0 - sum(weights.values())
        if abs(cash) > _EPS:
            weights[CASH_SECTOR] = weights.get(CASH_SECTOR, 0.0) + cash

        contrib = {s: w * day_returns.get(s, 0.0) for s, w in weights.items()}
        r_p = sum(contrib.values())
        r_b = bench.total_return

        pw, pr = _sector_split(weights, day_returns, sectors)
        effects = _brinson(pw, pr, bench.weights, bench.returns, r_b)

        daily_port.append(r_p)
        daily_bench.append(r_b)
        daily_effects.append(effects)
        daily_contrib.append(contrib)
        for sector in set(pw) | set(bench.weights):
            prev = sector_view.get(sector, (0.0, 0.0, 0.0, 0.0))
            sector_view[sector] = (
                prev[0] + pw.get(sector, 0.0),
                prev[1] + bench.weights.get(sector, 0.0),
                prev[2] + pr.get(sector, 0.0),
                prev[3] + bench.returns.get(sector, 0.0),
            )

    span = len(trading_days)
    factors, scale = carino_factors(daily_port)
    excess_factors, excess_scale, total_p, total_b = _excess_carino(daily_port, daily_bench)

    # §4.3 — 종목 기여도는 포트폴리오 Carino로 링킹해 Σ C_i = R_p를 맞춘다.
    linked: dict[str, float] = defaultdict(float)
    for contrib, k in zip(daily_contrib, factors, strict=True):
        for symbol, value in contrib.items():
            linked[symbol] += value * (k / scale)

    # 초과수익 Carino로 Brinson 효과를 링킹한다. 1d면 계수가 모두 1이다.
    allocation: dict[str, float] = defaultdict(float)
    selection: dict[str, float] = defaultdict(float)
    for effects, k in zip(daily_effects, excess_factors, strict=True):
        ratio = k / excess_scale
        for sector, (a, s, i) in effects.items():
            allocation[sector] += a * ratio
            selection[sector] += (s + i) * ratio

    sector_total = sum(allocation.values())
    selection_total = sum(selection.values())
    _assert_identity(total_b, sector_total, selection_total, total_p)

    start_weights = dict(portfolio_weights[0])
    contributors = _contributors(
        linked=linked,
        start_weights=start_weights,
        trading_days=trading_days,
        portfolio_returns=portfolio_returns,
        sectors=sectors,
        names=names,
        events=events,
    )

    sector_effects = tuple(
        sorted(
            (
                SectorEffect(
                    sector=sector,
                    portfolio_weight=totals[0] / span,
                    benchmark_weight=totals[1] / span,
                    portfolio_return=totals[2] / span,
                    benchmark_return=totals[3] / span,
                    allocation=allocation.get(sector, 0.0),
                    selection=selection.get(sector, 0.0),
                    interaction=0.0,
                    proxy=sector != CASH_SECTOR,
                )
                for sector, totals in sector_view.items()
            ),
            key=lambda e: -(e.allocation + e.selection),
        )
    )

    notes: list[str] = []
    if span == 1:
        notes.append("하루 구간이라 다기간 링킹을 적용하지 않았습니다(§4.3).")
    if not events:
        notes.append("구간에 매칭된 공시·이벤트가 없습니다.")

    return AttributionResult(
        start=trading_days[0],
        end=trading_days[-1],
        trading_days=span,
        portfolio_return=total_p,
        benchmark_return=total_b,
        excess_return=total_p - total_b,
        market=total_b,
        sector=sector_total,
        selection=selection_total,
        contributors=contributors,
        sector_effects=sector_effects,
        notes=tuple(notes),
    )


def _contributors(
    *,
    linked: Mapping[str, float],
    start_weights: Mapping[str, float],
    trading_days: Sequence[date],
    portfolio_returns: Sequence[Mapping[str, float]],
    sectors: Mapping[str, str],
    names: Mapping[str, str],
    events: Sequence[EventRecord],
) -> tuple[ContributorRow, ...]:
    """기여도 순으로 정렬한 종목 목록. 현금은 설명할 게 없으므로 뺀다.

    정렬 기준은 수익률이 아니라 기여도다(§4.1) — 비중 1%가 20% 오른 것과 비중 20%가
    1% 오른 것은 전체에 같은 영향을 준다.
    """
    rows: list[ContributorRow] = []
    for symbol, contribution in linked.items():
        if symbol == CASH_SECTOR:
            continue
        series = [
            (day, r.get(symbol)) for day, r in zip(trading_days, portfolio_returns, strict=True)
        ]
        observed = [(day, r) for day, r in series if r is not None]
        if not observed:
            continue
        compounded = math.prod(1.0 + r for _, r in observed) - 1.0
        anchor = max(observed, key=lambda pair: abs(pair[1]))[0]
        sector = sectors.get(symbol, CASH_SECTOR)
        rows.append(
            ContributorRow(
                ticker=symbol,
                name=names.get(symbol, symbol),
                sector=sector,
                weight=start_weights.get(symbol, 0.0),
                return_rate=compounded,
                contribution=contribution,
                held_at_start=symbol in start_weights,
                events=match_events(
                    events,
                    ticker=symbol,
                    sector=sector,
                    anchor=anchor,
                    trading_days=trading_days,
                    sectors=sectors,
                ),
            )
        )
    rows.sort(key=lambda row: -row.contribution)
    return tuple(rows)
