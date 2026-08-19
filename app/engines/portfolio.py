"""Portfolio Engine. 산식 문서 §2·§3.1·§4.1.

원장을 거래일 순서대로 재생해 매일의 보유 스냅샷을 만들고, 거기서 비중·평가손익·
집중도·기여도를 뽑는다. 문서의 항등식(§6.3)이 그대로 성립하도록 다음 셋을 지킨다.

- 비중 `w_i`의 분모에는 현금이 들어간다. 집중도는 현금을 빼고 재정규화한 `ŵ_i`를 쓴다.
- 매수·매도는 현금흐름이 아니다. 수익률의 `F_t`에는 외부 입출금만 들어간다.
- 기여도의 비중은 전일 종가 기준이다. 당일 비중을 쓰면 항등식 3이 깨진다.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from app.core.adapters import Ledger
from app.core.enums import OrderSide


@dataclass(slots=True)
class _Position:
    """이동평균 단가를 들고 있는 재생용 포지션."""

    quantity: float = 0.0
    avg_cost: float = 0.0

    def buy(self, quantity: float, price: float, fee: float) -> None:
        total = self.quantity + quantity
        self.avg_cost = (self.quantity * self.avg_cost + quantity * price + fee) / total
        self.quantity = total

    def sell(self, quantity: float) -> None:
        # 매도는 수량만 줄이고 단가는 그대로 둔다(§2.2). 전량 매도면 단가를 초기화한다 —
        # 재매수 시 이전 단가가 섞이면 손익률이 실제와 어긋난다.
        self.quantity -= quantity
        if self.quantity <= 1e-12:
            self.quantity = 0.0
            self.avg_cost = 0.0


@dataclass(frozen=True, slots=True)
class Holding:
    symbol: str
    name: str
    sector: str
    quantity: float
    price: float
    avg_cost: float
    market_value: float
    weight: float  # w_i — 분모에 현금 포함
    stock_weight: float  # ŵ_i — 현금 제외 후 재정규화
    unrealized_pnl: float
    return_rate: float


@dataclass(frozen=True, slots=True)
class Concentration:
    hhi: float
    n_eff: float
    top1: float
    top3: float
    sector_hhi: float


@dataclass(frozen=True, slots=True)
class PortfolioSnapshot:
    trade_date: date
    holdings: tuple[Holding, ...]
    cash: float
    total_value: float
    cash_weight: float

    def concentration(self) -> Concentration:
        """현금을 제외하고 재정규화한 비중으로 계산한다(§3.1).

        현금만 늘려도 분산된 것처럼 보이는 왜곡을 막기 위해서다.
        """
        weights = sorted((h.stock_weight for h in self.holdings), reverse=True)
        hhi = sum(w * w for w in weights)
        by_sector: dict[str, float] = {}
        for holding in self.holdings:
            by_sector[holding.sector] = by_sector.get(holding.sector, 0.0) + holding.stock_weight
        return Concentration(
            hhi=hhi,
            n_eff=1.0 / hhi if hhi else 0.0,
            top1=weights[0] if weights else 0.0,
            top3=sum(weights[:3]),
            sector_hhi=sum(w * w for w in by_sector.values()),
        )


@dataclass(frozen=True, slots=True)
class Contribution:
    symbol: str
    weight: float  # w_{i,t-1}
    return_rate: float  # r_{i,t}
    contribution: float  # c_{i,t}


@dataclass(frozen=True, slots=True)
class DailyReturn:
    trade_date: date
    portfolio_return: float
    contributions: tuple[Contribution, ...]


class PortfolioEngine:
    def __init__(self, ledger: Ledger) -> None:
        self._ledger = ledger
        self._snapshots: dict[date, PortfolioSnapshot] = {}
        self._replay()

    # ── 재생 ────────────────────────────────────────────
    def _replay(self) -> None:
        ledger = self._ledger
        positions: dict[str, _Position] = {}
        cash = 0.0
        for day in ledger.trading_days:
            cash += ledger.flow_on(day)
            for trade in ledger.trades_on(day):
                position = positions.setdefault(trade.symbol, _Position())
                gross = trade.quantity * trade.price
                if trade.side is OrderSide.BUY:
                    position.buy(trade.quantity, trade.price, trade.fee)
                    cash -= gross + trade.fee
                else:
                    position.sell(trade.quantity)
                    cash += gross - trade.fee
            self._snapshots[day] = self._build(day, positions, cash)

    def _build(
        self, day: date, positions: dict[str, _Position], cash: float
    ) -> PortfolioSnapshot:
        ledger = self._ledger
        held = [(s, p) for s, p in positions.items() if p.quantity > 0]
        values = {s: p.quantity * ledger.price(s, day) for s, p in held}
        stock_total = sum(values.values())
        total = stock_total + cash

        holdings = []
        for symbol, position in held:
            instrument = ledger.instrument(symbol)
            price = ledger.price(symbol, day)
            market_value = values[symbol]
            holdings.append(
                Holding(
                    symbol=symbol,
                    name=instrument.name,
                    sector=instrument.sector,
                    quantity=position.quantity,
                    price=price,
                    avg_cost=position.avg_cost,
                    market_value=market_value,
                    weight=market_value / total if total else 0.0,
                    # ŵ = w / (1 − w_c) 와 같은 값이지만 주식 합으로 직접 나눈다.
                    # 현금 비중이 1에 가까울 때 (1 − w_c)로 나누면 오차가 커진다.
                    stock_weight=market_value / stock_total if stock_total else 0.0,
                    unrealized_pnl=position.quantity * (price - position.avg_cost),
                    return_rate=price / position.avg_cost - 1 if position.avg_cost else 0.0,
                )
            )
        holdings.sort(key=lambda h: h.market_value, reverse=True)
        return PortfolioSnapshot(
            trade_date=day,
            holdings=tuple(holdings),
            cash=cash,
            total_value=total,
            cash_weight=cash / total if total else 0.0,
        )

    # ── 조회 ────────────────────────────────────────────
    def snapshot(self, day: date) -> PortfolioSnapshot:
        try:
            return self._snapshots[day]
        except KeyError:
            raise KeyError(f"{self._ledger.user_id}: 거래일이 아닌 날짜 {day}") from None

    def daily_returns(self) -> list[DailyReturn]:
        """일별 수익률과 종목 기여도. 첫 거래일은 전일이 없으므로 제외한다."""
        ledger = self._ledger
        rows: list[DailyReturn] = []
        for previous, day in zip(ledger.trading_days, ledger.trading_days[1:], strict=False):
            before = self.snapshot(previous)
            after = self.snapshot(day)
            if not before.total_value:
                continue
            # 매수·매도는 빼지 않는다. 외부 입출금만 뺀다(§2.3).
            portfolio_return = (after.total_value - ledger.flow_on(day)) / before.total_value - 1

            contributions = []
            for holding in before.holdings:
                price_now = ledger.price(holding.symbol, day)
                stock_return = price_now / holding.price - 1
                contributions.append(
                    Contribution(
                        symbol=holding.symbol,
                        weight=holding.weight,
                        return_rate=stock_return,
                        contribution=holding.weight * stock_return,
                    )
                )
            contributions.sort(key=lambda c: c.contribution, reverse=True)
            rows.append(DailyReturn(day, portfolio_return, tuple(contributions)))
        return rows

    def cumulative_return(self) -> float:
        """`R = Π(1 + r_t) − 1`. 기간 중 입금이 있어도 값이 흔들리지 않는다."""
        total = 1.0
        for row in self.daily_returns():
            total *= 1 + row.portfolio_return
        return total - 1
