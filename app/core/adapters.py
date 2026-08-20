"""원장(ledger) 어댑터.

거래·입출금·시세는 백엔드가 소유하고 엔진은 읽기만 한다. 아직 백엔드 읽기 권한이
없어서 JSON 시드로 대신 읽지만, 엔진은 `LedgerSource`만 보므로 나중에 DB 어댑터를
끼워 넣어도 엔진과 테스트는 그대로 돈다.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Protocol

from app.core.enums import OrderSide


@dataclass(frozen=True, slots=True)
class Instrument:
    symbol: str
    name: str
    sector: str


@dataclass(frozen=True, slots=True)
class Trade:
    """체결 한 건. 수수료는 매수·매도 모두 현금에서 빠진다."""

    trade_date: date
    symbol: str
    side: OrderSide
    quantity: float
    price: float
    fee: float = 0.0


@dataclass(frozen=True, slots=True)
class CashFlow:
    """외부 현금흐름. 입금이 양수, 출금이 음수다.

    매수·매도는 여기 들어가지 않는다. 현금과 주식 사이의 내부 이동이라
    수익률 분모를 건드리면 안 되기 때문이다(산식 §2.3).
    """

    trade_date: date
    amount: float


@dataclass(frozen=True, slots=True)
class Ledger:
    """한 사용자의 원장 스냅샷. 엔진이 재생(replay)하는 입력 전부."""

    user_id: str
    trading_days: tuple[date, ...]
    instruments: Mapping[str, Instrument]
    prices: Mapping[str, Mapping[date, float]]
    trades: tuple[Trade, ...] = ()
    flows: tuple[CashFlow, ...] = ()
    _trades_by_day: dict[date, list[Trade]] = field(default_factory=dict, repr=False)
    _flow_by_day: dict[date, float] = field(default_factory=dict, repr=False)

    def __post_init__(self) -> None:
        for trade in self.trades:
            self._trades_by_day.setdefault(trade.trade_date, []).append(trade)
        for flow in self.flows:
            self._flow_by_day[flow.trade_date] = (
                self._flow_by_day.get(flow.trade_date, 0.0) + flow.amount
            )

    def trades_on(self, day: date) -> Sequence[Trade]:
        return self._trades_by_day.get(day, ())

    def flow_on(self, day: date) -> float:
        """당일 외부 순입금 `F_t`."""
        return self._flow_by_day.get(day, 0.0)

    def price(self, symbol: str, day: date) -> float:
        try:
            return self.prices[symbol][day]
        except KeyError:
            raise KeyError(f"{self.user_id}: {symbol}의 {day} 종가가 원장에 없다") from None

    def instrument(self, symbol: str) -> Instrument:
        return self.instruments.get(symbol) or Instrument(symbol, symbol, "미분류")


class LedgerSource(Protocol):
    """원장 읽기 인터페이스. 엔진은 이것만 안다."""

    def load(self, user_id: str) -> Ledger: ...


class SeedLedgerSource:
    """JSON 시드 픽스처를 읽는 어댑터.

    백엔드 원장 읽기가 열리기 전까지 엔진·프롬프트 트랙이 같은 숫자를 보고 일하도록
    고정 데이터를 제공한다.
    """

    def __init__(self, path: Path | str) -> None:
        self._path = Path(path)
        self._raw: dict | None = None

    def _data(self) -> dict:
        if self._raw is None:
            self._raw = json.loads(self._path.read_text(encoding="utf-8"))
        return self._raw

    def user_ids(self) -> Iterable[str]:
        return self._data()["portfolios"].keys()

    def load(self, user_id: str) -> Ledger:
        data = self._data()
        try:
            portfolio = data["portfolios"][user_id]
        except KeyError:
            raise KeyError(f"시드에 없는 포트폴리오: {user_id}") from None

        instruments = {
            symbol: Instrument(symbol, meta["name"], meta["sector"])
            for symbol, meta in data["instruments"].items()
        }
        prices = {
            symbol: {date.fromisoformat(d): float(p) for d, p in series.items()}
            for symbol, series in portfolio["prices"].items()
        }
        trades = tuple(
            Trade(
                trade_date=date.fromisoformat(t["date"]),
                symbol=t["symbol"],
                side=OrderSide(t["side"]),
                quantity=float(t["quantity"]),
                price=float(t["price"]),
                fee=float(t.get("fee", 0.0)),
            )
            for t in portfolio.get("trades", ())
        )
        flows = tuple(
            CashFlow(date.fromisoformat(f["date"]), float(f["amount"]))
            for f in portfolio.get("flows", ())
        )
        return Ledger(
            user_id=user_id,
            trading_days=tuple(date.fromisoformat(d) for d in portfolio["trading_days"]),
            instruments=instruments,
            prices=prices,
            trades=trades,
            flows=flows,
        )
