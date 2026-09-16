"""주문 전 점검. API 명세 §7.

진단 엔진을 두 번 호출한 차분이다. 별도 시뮬레이션 엔진을 두지 않는다.
담당 트랙: feat/engine-risk

`before`는 `portfolio.py`의 진단이 쓰는 스냅샷 그대로고, `after`는 주문을 체결했다고
가정하고 수량·현금만 바꿔 다시 만든 같은 타입이다. 두 스냅샷을 `assess`에 넣어 얻은
`RiskAssessment` 두 개의 차가 곧 응답이다.

경고는 모델이 고르지 않는다. §3.7 임계값은 이미 `assess`가 적용했으므로, 주문 후에
새로 걸렸거나 등급이 올라간 finding만 추려내면 그것이 경고 목록이다. 모델은 각 경고의
문장만 쓴다.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from datetime import date
from typing import Any

from fastapi import APIRouter
from pydantic import BaseModel, Field
from sqlalchemy import select

from app.api.deps import CurrentUser, DbSession, UsageLimit
from app.api.routes.portfolio import (
    _FINDING_TITLES,
    _as_datetime,
    _benchmark,
    _indicators,
    _ledger,
    _market_cap_ranks,
)
from app.core.adapters import Ledger
from app.core.enums import MetricSource, OrderSide, ThesisStatus, WikiSource
from app.core.errors import InsufficientData, InvalidRequest
from app.core.models import Instrument, PriceDaily
from app.core.response_log import record
from app.core.schemas import DataAsOf, Envelope, Section, Segment
from app.engines.portfolio import Holding, PortfolioEngine, PortfolioSnapshot
from app.engines.risk import Finding, RiskAssessment, assess
from app.llm.client import get_llm_client  # noqa: F401  # backwards-compatible test patch point
from app.wiki.store import list_facts, list_theses

log = logging.getLogger("app.api.orders")

router = APIRouter(prefix="/orders", tags=["orders"])
Number = float | int

_SOURCE = MetricSource.RISK_ENGINE

#: 모델이 "어긋나는 점 없음"을 알리는 고정 문구. 프롬프트가 이 문자열을 그대로 지정하고
#: 서버가 접두사로 걸러 낸다 — 없는 충돌을 채워 넣지 않게 하려면 "안 씀"을 표현할
#: 통로가 있어야 한다.
_NO_CONFLICT = "해당 없음"

#: `delta`와 요약 자리표시자에 쓰는 지표. `_indicators`가 내보내는 것 중 비율이 아닌
#: 값(`rate_sensitivity`는 등급 문자열)은 여기서 자동으로 빠진다.
_SUMMARY_KEYS = ("hhi", "top1_weight", "top_sector_weight", "annualized_volatility", "cash_ratio")

#: finding 하나가 넘긴 지표의 전·후 값. 소수 자릿수는 진단(§5)과 맞춘다.
_METRIC_DIGITS: Mapping[str, int] = {"hhi": 2, "diversification_ratio": 2}


class OrderLine(BaseModel):
    ticker: str = Field(min_length=6, max_length=6)
    side: OrderSide
    quantity: int = Field(gt=0)
    price: int | None = None  # None 이면 최근 종가로 본다


class PreviewRequest(BaseModel):
    orders: list[OrderLine] = Field(min_length=1)


class OrderSummaryLine(BaseModel):
    ticker: str
    side: str
    quantity: int
    price: int
    amount: int


class PreviewMeasures(BaseModel):
    hhi: Number
    top1_weight: Number
    top3_weight: Number
    sector_hhi: Number
    annualized_volatility: Number | None
    max_drawdown_1y: Number
    cash_ratio: Number
    rate_sensitivity: str
    beta: Number | None
    large_cap_weight: Number | None
    diversification_ratio: Number | None
    top_sector_weight: Number


class PreviewWarning(BaseModel):
    id: str
    severity: str
    title: str
    metric: str
    before: Number | None
    after: Number
    threshold: Number
    #: 문장은 만들지 않는다. 엔진 수치(before·after·threshold)를 프런트가 그린다.
    text: str | None = None
    segments: list[Segment] | None = None


class ThesisConflict(BaseModel):
    id: str
    ticker: str
    fact: str
    source: str
    recorded_at: str
    conflict: str
    segments: list[Segment]


class PreviewContent(BaseModel):
    order_summary: list[OrderSummaryLine]
    orders_value: int
    feasible: bool
    shortfall: int | None
    before: PreviewMeasures
    after: PreviewMeasures
    delta: dict[str, Number]
    warnings: list[PreviewWarning]
    thesis_conflicts: list[ThesisConflict]
    summary: Section | None


# ── 호가 ──────────────────────────────────────────────────────────────────────
async def _quote(
    db: DbSession, ledger: Ledger, ticker: str, day: date
) -> tuple[float, Mapping[date, float] | None]:
    """평가에 쓸 종가와, 원장에 없는 종목이면 그 종목의 시세 전체.

    시세를 함께 들고 나오는 이유는 §3.2 변동성·상관이 종목별 일별 수익률을 요구하기
    때문이다. 새로 사는 종목의 히스토리가 없으면 주문 후 포트폴리오만 변동성이 사라져
    차분이 "주문 때문에 변동성이 없어졌다"처럼 읽힌다.
    """
    held = ledger.prices.get(ticker)
    if held:
        # 원장이 아는 종목이면 기준일 종가가 곧 시가다. 기준일에 구멍이 있으면 직전
        # 종가로 물러선다 — 거래정지 구간에도 평가는 해야 한다.
        return held.get(day) or held[max(held)], None

    rows = (
        await db.execute(
            select(PriceDaily.trade_date, PriceDaily.close).where(PriceDaily.ticker == ticker)
        )
    ).all()
    series = {trade_date: float(close) for trade_date, close in rows if close is not None}
    if not series:
        raise InsufficientData(f"{ticker}의 종가가 없어 주문을 평가할 수 없습니다.")
    return series.get(day) or series[max(series)], series


async def _instrument_meta(db: DbSession, tickers: Sequence[str]) -> dict[str, tuple[str, str]]:
    """새로 편입되는 종목의 종목명·업종. 업종은 §3.1 업종 집중도의 입력이다."""
    if not tickers:
        return {}
    rows = (
        await db.execute(
            select(Instrument.ticker, Instrument.name, Instrument.sector).where(
                Instrument.ticker.in_(tuple(tickers))
            )
        )
    ).all()
    return {ticker: (name, sector or "미분류") for ticker, name, sector in rows}


# ── 주문 후 스냅샷 ────────────────────────────────────────────────────────────
async def _after_snapshot(
    db: DbSession,
    ledger: Ledger,
    before: PortfolioSnapshot,
    orders: Sequence[OrderLine],
    day: date,
) -> tuple[PortfolioSnapshot, dict[str, Mapping[date, float]], list[dict[str, Any]], float]:
    """주문을 체결했다고 가정한 스냅샷. `PortfolioEngine._build`와 같은 규칙으로 짠다.

    수수료는 요청에 없으므로 0으로 본다. 매도 수량이 보유를 넘으면 음수 포지션을 만드는
    대신 400으로 끊는다 — 조용히 잘라 주면 사용자가 낸 주문과 다른 것을 점검하게 된다.
    """
    quantity = {h.symbol: h.quantity for h in before.holdings}
    avg_cost = {h.symbol: h.avg_cost for h in before.holdings}
    price = {h.symbol: h.price for h in before.holdings}
    meta = {h.symbol: (h.name, h.sector) for h in before.holdings}
    extra_prices: dict[str, Mapping[date, float]] = {}
    cash = before.cash

    fresh = [line.ticker for line in orders if line.ticker not in meta]
    known = await _instrument_meta(db, fresh)

    summary: list[dict[str, Any]] = []
    for line in orders:
        ticker = line.ticker
        held = quantity.get(ticker, 0.0)
        if line.side is OrderSide.SELL and line.quantity > held:
            # 호가를 찾기 *전에* 끊는다. 보유하지 않은 종목을 파는 주문은 시세가 없어서가
            # 아니라 팔 것이 없어서 틀린 주문이다.
            raise InvalidRequest(
                f"{ticker} 보유 수량은 {held:g}주인데 {line.quantity}주 매도 주문입니다."
            )

        if ticker not in price:
            market, series = await _quote(db, ledger, ticker, day)
            price[ticker] = market
            if series is not None:
                extra_prices[ticker] = series
            meta[ticker] = known.get(ticker) or (
                ledger.instrument(ticker).name,
                ledger.instrument(ticker).sector,
            )

        # 체결가는 사용자가 준 지정가, 없으면 시가다. 평가는 언제나 시가로 한다 —
        # 지정가로 평가하면 주문을 내는 것만으로 평가손익이 생긴다.
        fill = float(line.price) if line.price is not None else price[ticker]
        gross = line.quantity * fill

        if line.side is OrderSide.BUY:
            quantity[ticker] = held + line.quantity
            avg_cost[ticker] = (held * avg_cost.get(ticker, 0.0) + gross) / quantity[ticker]
            cash -= gross
        else:
            quantity[ticker] = held - line.quantity
            cash += gross
            if quantity[ticker] <= 0.0:
                # 전량 매도면 단가를 초기화한다(§2.2). `_Position.sell`과 같은 규칙이다.
                avg_cost[ticker] = 0.0

        summary.append(
            {
                "ticker": ticker,
                "side": line.side.value,
                "quantity": line.quantity,
                "price": round(fill),
                "amount": round(gross),
            }
        )

    return (
        _build(day, quantity, avg_cost, price, meta, cash),
        extra_prices,
        summary,
        sum(row["amount"] for row in summary if row["side"] == OrderSide.BUY.value),
    )


def _build(
    day: date,
    quantity: Mapping[str, float],
    avg_cost: Mapping[str, float],
    price: Mapping[str, float],
    meta: Mapping[str, tuple[str, str]],
    cash: float,
) -> PortfolioSnapshot:
    """`PortfolioEngine._build`의 규칙을 주문 후 상태에 그대로 적용한다.

    엔진의 것을 직접 부르지 않는 이유는 그쪽이 `_Position`과 원장 조회에 묶여 있어서다 —
    원장에 없는 종목을 새로 사는 경우가 곧바로 KeyError가 된다.
    """
    held = [(symbol, qty) for symbol, qty in quantity.items() if qty > 0]
    values = {symbol: qty * price[symbol] for symbol, qty in held}
    stock_total = sum(values.values())
    total = stock_total + cash

    holdings = [
        Holding(
            symbol=symbol,
            name=meta[symbol][0],
            sector=meta[symbol][1],
            quantity=qty,
            price=price[symbol],
            avg_cost=avg_cost.get(symbol, 0.0),
            market_value=values[symbol],
            weight=values[symbol] / total if total else 0.0,
            stock_weight=values[symbol] / stock_total if stock_total else 0.0,
            unrealized_pnl=qty * (price[symbol] - avg_cost.get(symbol, 0.0)),
            return_rate=price[symbol] / avg_cost[symbol] - 1 if avg_cost.get(symbol) else 0.0,
        )
        for symbol, qty in held
    ]
    holdings.sort(key=lambda h: h.market_value, reverse=True)
    return PortfolioSnapshot(
        trade_date=day,
        holdings=tuple(holdings),
        cash=cash,
        total_value=total,
        cash_weight=cash / total if total else 0.0,
    )


# ── 차분 ──────────────────────────────────────────────────────────────────────
def _measures(result: RiskAssessment) -> dict[str, Any]:
    """§5 지표에서 업종 수를 빼고 업종 최대 비중을 더한다(§8 계약)."""
    values = _indicators(result)
    values.pop("sector_count")
    values["top_sector_weight"] = result.top_sector_weight
    return values


def _delta(before: Mapping[str, Any], after: Mapping[str, Any]) -> dict[str, float]:
    """after − before. 한쪽이라도 계산되지 않았으면 0이 아니라 아예 빼 버린다.

    `rate_sensitivity`처럼 수치가 아닌 지표도 여기서 함께 걸러진다.
    """
    return {
        key: after[key] - before[key]
        for key, value in before.items()
        if isinstance(value, int | float)
        and not isinstance(value, bool)
        and isinstance(after.get(key), int | float)
        and not isinstance(after.get(key), bool)
    }


_SEVERITY_RANK = {"info": 0, "medium": 1, "high": 2}


def _raised(before: RiskAssessment, after: RiskAssessment) -> list[Finding]:
    """주문 때문에 새로 걸렸거나 등급이 올라간 항목. 이것이 경고 목록 전부다.

    §3.7 임계 판정은 `assess`가 이미 했으므로 여기서 임계값을 다시 쓰지 않는다.
    나아진 항목은 경고가 아니므로 빠진다 — 요약이 그쪽을 말한다.
    """
    was = {f.id: f.severity.value for f in before.findings}
    return [
        finding
        for finding in after.findings
        if _SEVERITY_RANK[finding.severity.value] > _SEVERITY_RANK.get(was.get(finding.id), -1)
    ]


# 주문 전 점검은 LLM 도 프리셋 문장도 쓰지 않는다. 엔진 차분(before·after·delta·
# warnings·shortfall)만 보내고 문장은 화면이 수치로 만든다. 서버가 "현금이 N원 부족합니다"
# 같은 문장을 조립하면 프런트가 같은 수치를 두 번 표현하고, 문구 정책이 두 곳에 갈린다.


# ── 위키 ──────────────────────────────────────────────────────────────────────
async def _stated_context(db: DbSession, user_id: str, tickers: set[str]) -> tuple[list[Any], str]:
    """사용자가 *직접 진술한* 항목만 고른다(§7).

    AI가 추론한 성향(`ai_inferred`)으로 주문에 이의를 제기하면 근거 없는 참견이 된다.
    논지는 주문에 오른 종목만, 성향은 요약의 배경으로 전부 넘긴다.
    """
    theses = [
        thesis
        for thesis in await list_theses(db, user_id, ThesisStatus.ACTIVE)
        if thesis.source == WikiSource.USER_STATED and thesis.ticker in tickers
    ]
    facts = [
        fact.text for fact in await list_facts(db, user_id) if fact.source == WikiSource.USER_STATED
    ]
    return theses, "\n".join(f"- {text}" for text in facts)


# ── 라우터 ────────────────────────────────────────────────────────────────────
@router.post("/preview")
async def preview(
    body: PreviewRequest, user_id: CurrentUser, db: DbSession, _usage: UsageLimit
) -> Envelope[PreviewContent]:
    """주문 체결을 가정하고 진단을 다시 돌려 차분을 돌려준다(§7).

    승인·거절 판단은 하지 않는다. `feasible`은 현금이 모자라는지만 알리는 사실이다.

    히스토리가 짧으면 진단과 마찬가지로 409로 끊지 않는다. 변동성·상관만 전·후 모두
    null이 되고 집중도·현금 차분은 그대로 유효하다.
    """
    ledger = await _ledger(user_id)
    if ledger is None:
        raise InsufficientData(
            "보유 내역을 불러오지 못해 점검할 수 없습니다.",
            detail={"reason": "ledger_unavailable"},
        )
    if not ledger.trading_days:
        raise InsufficientData("주문을 점검할 수 있는 거래일이 없습니다.")

    engine = PortfolioEngine(ledger)
    last = ledger.trading_days[-1]
    before_snapshot = engine.snapshot(last)

    after_snapshot, extra_prices, order_summary, orders_value = await _after_snapshot(
        db, ledger, before_snapshot, body.orders, last
    )

    # MDD는 과거 총자산 시계열에서 나온다. 아직 내지 않은 주문이 지난 낙폭을 바꾸지는
    # 않으므로 전·후에 같은 시계열을 넘긴다(따라서 delta.max_drawdown_1y는 0이다).
    value_series = [(day, engine.snapshot(day).total_value) for day in ledger.trading_days]
    symbols = tuple(
        {h.symbol for h in before_snapshot.holdings} | {h.symbol for h in after_snapshot.holdings}
    )
    benchmark = await _benchmark(db)
    ranks = await _market_cap_ranks(db, symbols)

    before = assess(
        before_snapshot,
        ledger.prices,
        value_series=value_series,
        benchmark=benchmark,
        market_cap_ranks=ranks,
    )
    after = assess(
        after_snapshot,
        {**ledger.prices, **extra_prices},
        value_series=value_series,
        benchmark=benchmark,
        market_cap_ranks=ranks,
    )

    before_measures, after_measures = _measures(before), _measures(after)
    raised = _raised(before, after)
    feasible = after_snapshot.cash >= 0.0

    envelope = Envelope[PreviewContent](
        content={
            "order_summary": order_summary,
            "orders_value": round(orders_value),
            "feasible": feasible,
            "shortfall": round(-after_snapshot.cash) if after_snapshot.cash < 0.0 else None,
            "before": before_measures,
            "after": after_measures,
            "delta": _delta(before_measures, after_measures),
            "warnings": [
                {
                    "id": finding.id,
                    "severity": finding.severity.value,
                    "title": _FINDING_TITLES.get(finding.id, finding.id),
                    "metric": finding.metric,
                    "before": next((f.value for f in before.findings if f.id == finding.id), None),
                    "after": finding.value,
                    "threshold": finding.threshold,
                }
                for finding in raised
            ],
            "thesis_conflicts": [],
            "summary": None,
        },
        data_as_of=DataAsOf(
            price=_as_datetime(before_snapshot),
            portfolio=_as_datetime(before_snapshot),
        ),
    )
    await record(db, envelope, user_id=user_id, endpoint="orders.preview")
    return envelope
