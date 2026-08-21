"""목업 백엔드. AI 의 LEDGER_SOURCE=backend 경로를 실제로 돌리기 위한 개발용 스텁.

    uvicorn scripts.mock_backend:app --port 8080
    LEDGER_SOURCE=backend uvicorn app.api.main:app --reload   # 다른 셸

백엔드 API 명세 §9 의 /internal/v1 2개만 구현한다. 봉투 없음, camelCase.
토큰은 검증하지 않는다 — .env 의 BACKEND_SERVICE_TOKEN 이 비어 있고,
헤더 전송은 tests/test_backend_adapter.py 가 이미 고정한다.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import FastAPI, Header

app = FastAPI(title="Ledger backend mock")
SEED_CASH = 20_000_000
ROUND_ID = 3
VALID_TRADING_DAYS = frozenset(
    {"2025-07-16", "2025-09-01", "2025-11-03", "2026-02-02", "2026-05-04"}
)


def _trade(trade_id: int, day: str, ticker: str, side: str, quantity: int, price: int) -> dict:
    return {
        "tradeId": trade_id,
        "stockCode": ticker,
        "side": side,
        "quantity": quantity,
        "price": price,
        "executedAt": f"{day}T10:12:44+09:00",
    }


_TRADES_1 = [
    _trade(1, "2025-07-16", "005930", "BUY", 50, 64_700),
    _trade(2, "2025-07-16", "105560", "BUY", 30, 113_600),
    _trade(3, "2025-09-01", "005380", "BUY", 10, 220_500),
    _trade(4, "2025-11-03", "035420", "BUY", 8, 274_500),
    _trade(5, "2026-02-02", "373220", "BUY", 5, 380_000),
    _trade(6, "2026-05-04", "105560", "SELL", 10, 157_800),
]
_HOLDINGS_1 = [
    {
        "stockCode": "005930",
        "stockName": "삼성전자",
        "quantity": 50,
        "avgBuyPrice": 64_700,
        "currentPrice": 269_750,
    },
    {
        "stockCode": "105560",
        "stockName": "KB금융",
        "quantity": 20,
        "avgBuyPrice": 113_600,
        "currentPrice": 160_700,
    },
    {
        "stockCode": "005380",
        "stockName": "현대자동차",
        "quantity": 10,
        "avgBuyPrice": 220_500,
        "currentPrice": 419_500,
    },
    {
        "stockCode": "035420",
        "stockName": "NAVER",
        "quantity": 8,
        "avgBuyPrice": 274_500,
        "currentPrice": 221_500,
    },
    {
        "stockCode": "373220",
        "stockName": "LG에너지솔루션",
        "quantity": 5,
        "avgBuyPrice": 380_000,
        "currentPrice": 359_000,
    },
]
_TRADES_2 = [_trade(7, "2025-07-16", "005930", "BUY", 25, 64_700)]
_HOLDINGS_2 = [
    {
        "stockCode": "005930",
        "stockName": "삼성전자",
        "quantity": 25,
        "avgBuyPrice": 64_700,
        "currentPrice": 269_750,
    }
]
SCENARIOS = {
    "1": {"trades": _TRADES_1, "holdings": _HOLDINGS_1},
    "2": {"trades": _TRADES_2, "holdings": _HOLDINGS_2},
}


def _scenario(user_id: str | None) -> dict[str, list[dict[str, Any]]]:
    return SCENARIOS.get(user_id or "", SCENARIOS["1"])


def _net_invested(trades: list[dict[str, Any]]) -> float:
    return sum(
        row["quantity"] * row["price"] * (1 if row["side"] == "BUY" else -1) for row in trades
    )


@app.get("/internal/v1/portfolio")
def portfolio(x_user_id: Annotated[str | None, Header()] = None) -> dict:
    scenario = _scenario(x_user_id)
    return {
        "roundId": ROUND_ID,
        "cashBalance": SEED_CASH - _net_invested(scenario["trades"]),
        "asOf": "2026-08-20T14:30:00+09:00",
        "holdings": scenario["holdings"],
    }


@app.get("/internal/v1/trades")
def trades(
    x_user_id: Annotated[str | None, Header()] = None,
    size: int = 100,
    roundId: int | None = None,
    cursor: str | None = None,
) -> dict:
    del size, roundId, cursor
    return {
        "roundId": ROUND_ID,
        "trades": _scenario(x_user_id)["trades"],
        "nextCursor": None,
        "hasNext": False,
    }
