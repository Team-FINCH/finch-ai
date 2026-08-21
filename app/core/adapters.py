"""원장(ledger) 어댑터.

거래·입출금·시세는 백엔드가 소유하고 엔진은 읽기만 한다. 아직 백엔드 읽기 권한이
없어서 JSON 시드로 대신 읽지만, 엔진은 `LedgerSource`만 보므로 나중에 DB 어댑터를
끼워 넣어도 엔진과 테스트는 그대로 돈다.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from functools import lru_cache
from pathlib import Path
from typing import Protocol

from sqlalchemy import select

from app.core.config import settings
from app.core.enums import OrderSide
from app.core.errors import InsufficientData


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
        """`day`의 종가. 없으면 직전 거래일 종가로 거슬러 올라간다(as-of).

        휴장일·거래정지로 그날 종가가 빠지는 것은 정상인데, 그동안에도 보유는
        그대로 남아 있어 평가금액(§2.1)에는 값이 있어야 한다. 직전가를 그대로 쓰면
        `V_t`와 `r_{i,t}`가 같은 가격을 보므로 그 종목의 당일 수익률이 0이 되고
        항등식 3(`Σc = r_p`)은 그대로 성립한다. 결측일을 빼거나 0을 넣으면 깨진다.

        변동성·상관은 이 경로를 타지 않는다. `risk._aligned_returns`가 날짜
        교집합으로 따로 다루며, §6.1대로 그쪽은 결측일을 메우지 않고 제외한다.

        무한정 거슬러 올라가면 상장폐지·장기 거래정지 종목이 영원히 살아 있는
        평가액을 갖는다. `price_asof_max_days`를 넘거나 그 종목 종가가 아예 없으면
        0이나 None을 흘리지 않고 끊는다 — 금액이 0으로 섞이면 수익률이 조용히 틀린다.
        """
        series = self.prices.get(symbol) or {}
        close = series.get(day)
        if close is not None:
            return close
        limit = settings.price_asof_max_days
        recent = [d for d in series if day - timedelta(days=limit) <= d < day]
        if recent:
            return series[max(recent)]
        raise InsufficientData(
            f"{symbol}의 {day} 종가가 없고 직전 {limit}일 안에도 없습니다.",
            detail={"user_id": self.user_id, "symbol": symbol, "date": day.isoformat()},
        )

    def instrument(self, symbol: str) -> Instrument:
        return self.instruments.get(symbol) or Instrument(symbol, symbol, "미분류")


class LedgerSource(Protocol):
    """원장 읽기 인터페이스. 엔진은 이것만 안다.

    실제 원장은 백엔드 HTTP + 우리 DB 라 async 다. 시드가 파일이라 동기였을 뿐이라
    프로토콜을 async 로 둔다 — 구현마다 갈리면 호출부가 어느 쪽인지 알아야 한다.
    """

    async def load(self, user_id: str) -> Ledger: ...


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

    async def load(self, user_id: str) -> Ledger:
        # 파일 읽기라 await 할 것이 없다. 프로토콜을 하나로 유지하려고 async 다.
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
    """원장은 백엔드에서, 시장 데이터는 우리 DB 에서 읽는 어댑터.

    역할 분담이 있다. 백엔드는 원장(누가 무엇을 얼마나 들고 있나)을 갖고,
    우리는 시장 데이터(과거 종가·섹터)를 갖는다. 백엔드 명세 §9 가
    `currentPrice` 한 점만 주는 것은 설계 누락이 아니라 이 분담 때문이다.

    | 데이터 | 출처 |
    | --- | --- |
    | 보유·현금·회차 | 백엔드 `GET /internal/v1/portfolio` |
    | 거래 이력 | 백엔드 `GET /internal/v1/trades` (커서 페이징) |
    | 과거 일별 종가 | 우리 `price_daily` (ingest/prices.py 가 채운다) |
    | 섹터 | 우리 `instruments.sector` |

    백엔드 원장은 읽기만 한다. 파생 지표는 전부 우리가 계산한다(명세 10.1).

    **아직 §9 에 없는 것**: 입출금 이력과 수수료. 수수료는 지어내지 않고 0으로
    두며, 외부 현금흐름은 현재 현금에서 역산한 기초 입금 한 건으로 복원한다.
    입금 시점은 첫 거래일로 가정하므로 회차 중간 충전은 정확히 반영하지 못한다.

    `load` 가 async 인 점에 주의. `LedgerSource` 프로토콜은 동기지만 우리
    드라이버가 asyncpg 뿐이라(동기 드라이버는 새 의존성이다) DB 를 동기로
    읽을 방법이 없다. 이 클래스는 프로토콜을 만족하지 않으며, 라우터에 물릴
    때 호출부를 async 로 바꿔야 한다 — 엔진은 `Ledger` 만 받으므로 그대로다.
    """

    _TRADE_PAGE = 100

    def __init__(
        self,
        base_url: str,
        token: str,
        *,
        client: object | None = None,
        sessions: object | None = None,
        timeout: float = 5.0,
    ) -> None:
        self._base = base_url.rstrip("/")
        self._token = token
        self._client = client
        # app.core.db 의 엔진은 모듈 전역이라 커넥션 풀이 처음 열린 이벤트 루프에
        # 묶인다. 테스트가 루프를 갈아 끼우므로 세션 팩토리를 갈아 끼울 수 있게 둔다.
        self._sessions = sessions
        self._timeout = timeout
        #: load() 가 채운다. 사용자별 데이터라 모든 요청에 사용자 헤더가 실려야 한다.
        self._user_id = ""

    # ── 백엔드 ───────────────────────────────────────────────────────
    def _get(self, path: str, params: Mapping[str, object] | None = None) -> dict:
        import httpx

        headers = {
            settings.internal_token_header: self._token,
            settings.trusted_user_header: self._user_id,
        }
        client = self._client or httpx.Client(timeout=self._timeout)
        try:
            res = client.get(f"{self._base}{path}", params=params, headers=headers)
            res.raise_for_status()
            return res.json()
        except httpx.HTTPError as exc:
            # 호출부는 원장을 못 읽는 경우를 (KeyError, FileNotFoundError, OSError)
            # 로 잡는다. httpx 예외는 거기 안 들어가서 그대로 새면 500 이 된다.
            # 원천을 못 읽은 것이니 OSError 로 옮겨 준다 — 호출부가 httpx 를
            # 알아야 할 이유가 없다.
            raise OSError(f"백엔드 원장을 읽지 못했다: {path}") from exc
        finally:
            if self._client is None:
                client.close()

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

    # ── 조립 ─────────────────────────────────────────────────────────
    async def load(self, user_id: str) -> Ledger:
        self._user_id = user_id
        # 동기 HTTP 라 이벤트 루프를 막는다. 스레드로 뺀다 — app.rag.search 와 같다.
        portfolio = await asyncio.to_thread(self._get, "/internal/v1/portfolio")
        holdings = tuple(portfolio.get("holdings", ()))
        round_id = portfolio.get("roundId")
        trades = await asyncio.to_thread(lambda: tuple(self._trades(round_id)))

        # 거래한 종목도 시세가 있어야 한다. 전량 매도해 지금은 안 들고 있어도
        # 그날의 평가액을 다시 계산해야 하기 때문이다.
        tickers = tuple({h["stockCode"] for h in holdings} | {t.symbol for t in trades})
        prices, sectors = await _market_data(tickers, self._sessions)
        trading_days = _common_days(prices)

        instruments = {
            h["stockCode"]: Instrument(
                h["stockCode"], h["stockName"], sectors.get(h["stockCode"]) or "미분류"
            )
            for h in holdings
        }

        flows: tuple[CashFlow, ...] = ()
        if trading_days:
            opening_cash = float(portfolio["cashBalance"])
            trading_day_set = set(trading_days)
            for trade in (row for row in trades if row.trade_date in trading_day_set):
                gross = trade.quantity * trade.price
                opening_cash += (
                    gross + trade.fee if trade.side is OrderSide.BUY else -gross + trade.fee
                )
            # ponytail: 입금 시점을 모르니 전액을 첫 거래일로 몰아넣는다. 회차 중간
            # 충전이 있으면 시간가중수익률이 낮게 나온다. 백엔드 /internal/v1 이
            # 입출금 이력을 내주면(aiApiSpec §5 결정항목 7) 이 복원을 걷어낸다.
            flows = (CashFlow(trading_days[0], opening_cash),)

        return Ledger(
            user_id=user_id,
            trading_days=trading_days,
            instruments=instruments,
            prices=prices,
            trades=trades,
            flows=flows,
        )


async def _market_data(
    tickers: Sequence[str], sessions: object | None = None
) -> tuple[dict[str, dict[date, float]], dict[str, str | None]]:
    """우리 DB 의 종가 시계열과 섹터. 종목 마스터·시세는 AI 파트가 자체 구축한다.

    라우터에도 `price_daily` 를 읽는 코드가 있지만 전부 그쪽 전용 헬퍼이고
    (구간 종가, `(ticker, date)` 키) 모양이 다르다. core 가 api 를 임포트할
    수도 없어서 여기서 읽는다.
    """
    if not tickers:
        return {}, {}

    from app.core.db import SessionFactory
    from app.core.models import Instrument as InstrumentRow
    from app.core.models import PriceDaily

    prices: dict[str, dict[date, float]] = {t: {} for t in tickers}
    async with (sessions or SessionFactory)() as session:
        rows = (
            await session.execute(
                select(PriceDaily.ticker, PriceDaily.trade_date, PriceDaily.close)
                .where(PriceDaily.ticker.in_(tickers))
                .order_by(PriceDaily.trade_date)
            )
        ).all()
        for ticker, day, close in rows:
            if close:  # 거래정지 구간은 행이 없다. 0 이 섞이면 변동성이 과소 추정된다.
                prices[ticker][day] = float(close)

        sectors = dict(
            (
                await session.execute(
                    select(InstrumentRow.ticker, InstrumentRow.sector).where(
                        InstrumentRow.ticker.in_(tickers)
                    )
                )
            ).all()
        )
    return prices, sectors


def _common_days(prices: Mapping[str, Mapping[date, float]]) -> tuple[date, ...]:
    """모든 종목에 종가가 있는 날만 거래일로 친다.

    엔진은 거래일마다 보유 종목 전부의 종가를 찾는다. 빠진 날은 `Ledger.price`
    가 상한 안에서 직전 종가로 메우지만, 여기서 교집합을 쓰면 메울 일 자체가
    없어 실제 종가만으로 계산한다. 교집합이 비면 `trading_days` 가 비고,
    호출부는 이미 그것을 데이터 부족으로 다룬다.
    """
    series = [set(days) for days in prices.values() if days]
    if not series or len(series) != len(prices):
        return ()
    return tuple(sorted(set.intersection(*series)))


def _to_date(value: str) -> date:
    """ISO 8601 날짜/일시에서 날짜만 꺼낸다. 백엔드는 오프셋을 붙여 보낸다."""
    return datetime.fromisoformat(value).date()


@lru_cache
def ledger_source() -> LedgerSource | None:
    """설정이 가리키는 원장 어댑터. `none` 이면 None 이다.

    선택은 여기 한 곳에서만 한다. 호출부는 `None` 인지만 보고 어느 어댑터인지
    알 필요가 없다 — 그게 `LedgerSource` 프로토콜을 둔 이유다.

    `backend` 는 백엔드 `/internal/v1` 이 구현되기 전에는 쓸 수 없다.
    """
    match settings.ledger_source:
        case "seed":
            return SeedLedgerSource(settings.seed_fixture_path)
        case "backend":
            return BackendLedgerSource(
                settings.backend_base_url, settings.backend_service_token
            )
        case _:
            return None
