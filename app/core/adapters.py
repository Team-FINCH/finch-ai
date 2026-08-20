"""원장(ledger) 어댑터.

거래·입출금·시세는 백엔드가 소유하고 엔진은 읽기만 한다. 아직 백엔드 읽기 권한이
없어서 JSON 시드로 대신 읽지만, 엔진은 `LedgerSource`만 보므로 나중에 DB 어댑터를
끼워 넣어도 엔진과 테스트는 그대로 돈다.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Protocol

from app.core.config import settings
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


class BackendLedgerSource:
    """백엔드 `/internal/v1` 을 읽는 어댑터. 백엔드 API 명세 §9.

    읽기 전용이다. 백엔드는 원본 값만 내려주고 파생 지표는 우리가 계산한다.

    **백엔드가 아직 못 주는 것이 있다.** §9 는 보유·거래만 준다.

    | 필요한 것 | §9 제공 | 여기서 하는 일 |
    | --- | --- | --- |
    | 일별 종가 시계열 | `currentPrice` 한 점뿐 | `asOf` 하루치만 채운다 |
    | 섹터 | 없음 | `Ledger.instrument()` 의 "미분류" 로 떨어진다 |
    | 입출금 | 없음 (`/api/v1/deposits` 는 사용자 API) | 빈 튜플 |

    그래서 이 어댑터만으로는 60거래일이 필요한 위험 지표가 `INSUFFICIENT_DATA`
    로 떨어진다. 명세대로의 정상 동작이지 버그가 아니다. 시계열 원천이 붙어야
    풀리며, 없는 값을 지어내지 않는 편이 조용히 틀린 숫자보다 낫다.
    """

    # ponytail: 동기 클라이언트다. LedgerSource.load 가 동기라 async 로 만들면
    # 엔진·라우터까지 번지는데, 프로토콜 뒤에 숨기는 게 이 설계의 요점이다.
    # 라우터에 물릴 때 asyncio.to_thread 로 감싸면 이벤트 루프를 막지 않는다.
    _TRADE_PAGE = 100

    def __init__(
        self,
        base_url: str,
        token: str,
        *,
        client: object | None = None,
        timeout: float = 5.0,
    ) -> None:
        self._base = base_url.rstrip("/")
        self._token = token
        self._client = client
        self._timeout = timeout
        #: load() 가 채운다. 사용자별 데이터라 모든 요청에 사용자 헤더가 실려야 한다.
        self._user_id = ""

    def _get(self, path: str, params: Mapping[str, object] | None = None) -> dict:
        import httpx

        # 사용자별 데이터라 사용자 헤더가 함께 가야 한다. 백엔드 명세 §9.
        headers = {
            settings.internal_token_header: self._token,
            settings.trusted_user_header: self._user_id,
        }
        client = self._client or httpx.Client(timeout=self._timeout)
        try:
            res = client.get(f"{self._base}{path}", params=params, headers=headers)
            res.raise_for_status()
            return res.json()
        finally:
            if self._client is None:
                client.close()

    def load(self, user_id: str) -> Ledger:
        self._user_id = user_id
        portfolio = self._get("/internal/v1/portfolio")
        as_of = _to_date(portfolio["asOf"])
        round_id = portfolio.get("roundId")

        instruments = {
            h["stockCode"]: Instrument(h["stockCode"], h["stockName"], "미분류")
            for h in portfolio.get("holdings", ())
        }
        # 시세 시계열이 없다. 스냅샷 하루치만 넣는다 — 위 표 참조.
        prices = {
            h["stockCode"]: {as_of: float(h["currentPrice"])}
            for h in portfolio.get("holdings", ())
        }

        trades = tuple(self._trades(round_id))
        # 거래일은 체결일 + 스냅샷 기준일에서 뽑는다. 개장일 달력이 따로 없다.
        trading_days = tuple(sorted({t.trade_date for t in trades} | {as_of}))

        return Ledger(
            user_id=user_id,
            trading_days=trading_days,
            instruments=instruments,
            prices=prices,
            trades=trades,
            flows=(),  # §9 에 입출금이 없다
        )

    def _trades(self, round_id: object) -> Iterable[Trade]:
        """커서 페이징을 끝까지 따라간다. 기본 100건. 백엔드 명세 §9.2."""
        cursor: str | None = None
        seen = 0
        while True:
            params: dict[str, object] = {"size": self._TRADE_PAGE}
            if round_id is not None:
                params["roundId"] = round_id
            if cursor:
                params["cursor"] = cursor
            page = self._get("/internal/v1/trades", params)

            for row in page.get("trades", ()):
                yield Trade(
                    trade_date=_to_date(row["executedAt"]),
                    symbol=row["stockCode"],
                    side=OrderSide(row["side"].lower()),
                    quantity=float(row["quantity"]),
                    price=float(row["price"]),
                    # §9 에 수수료가 없다. 0 이면 수익률이 실제보다 좋게 나온다.
                    fee=float(row.get("fee", 0.0)),
                )
                seen += 1

            cursor = page.get("nextCursor")
            # hasNext 가 참인데 커서가 없으면 같은 쪽을 영원히 다시 받는다.
            if not page.get("hasNext") or not cursor:
                return
            if seen > 100_000:
                raise RuntimeError("거래 페이징이 끝나지 않는다. 백엔드 커서를 확인하라")


def _to_date(value: str) -> date:
    """ISO 8601 날짜/일시에서 날짜만 꺼낸다. 백엔드는 오프셋을 붙여 보낸다."""
    return datetime.fromisoformat(value).date()
