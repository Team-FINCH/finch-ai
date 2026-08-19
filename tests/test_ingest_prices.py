"""시세 적재 변환 로직 테스트.

네트워크와 DB 없이 도는 것만 둔다. pykrx 호출은 목으로 대체한다.
"""

from __future__ import annotations

from datetime import date

import pandas as pd
import pytest

from ingest.prices import COLUMN_MAP, _to_rows, _yyyymmdd


def _df(rows: list[tuple]) -> pd.DataFrame:
    """pykrx가 돌려주는 모양 그대로 만든다 — 한글 컬럼, 날짜 인덱스."""
    idx = pd.to_datetime([r[0] for r in rows])
    return pd.DataFrame(
        {
            "시가": [r[1] for r in rows],
            "고가": [r[2] for r in rows],
            "저가": [r[3] for r in rows],
            "종가": [r[4] for r in rows],
            "거래량": [r[5] for r in rows],
            "등락률": [0.0] * len(rows),
        },
        index=idx,
    )


def test_yyyymmdd() -> None:
    assert _yyyymmdd(date(2026, 8, 19)) == "20260819"


def test_column_map_covers_model_fields() -> None:
    assert set(COLUMN_MAP.values()) == {"open", "high", "low", "close", "volume"}


def test_converts_basic_rows() -> None:
    df = _df([("2026-08-18", 100, 110, 95, 105, 1000)])
    rows = _to_rows("005930", df)
    assert rows == [
        {
            "ticker": "005930",
            "trade_date": date(2026, 8, 18),
            "open": 100,
            "high": 110,
            "low": 95,
            "close": 105,
            "volume": 1000,
            "trade_value": None,
        }
    ]


def test_ticker_stays_a_six_char_string() -> None:
    """정수로 다루면 선행 0이 사라진다. 모델 제약에 걸려 즉시 실패한다."""
    rows = _to_rows("005930", _df([("2026-08-18", 1, 1, 1, 1, 1)]))
    assert rows[0]["ticker"] == "005930"
    assert len(rows[0]["ticker"]) == 6


def test_halted_days_are_dropped_not_zero_filled() -> None:
    """거래정지일에 0 수익률이 들어가면 변동성이 과소 추정된다.

    엔진 산식 §6.1 — 직전가 유지가 아니라 해당 날짜를 제외한다.
    """
    df = _df(
        [
            ("2026-08-17", 100, 100, 100, 100, 500),
            ("2026-08-18", 0, 0, 0, 0, 0),      # 거래정지
            ("2026-08-19", 110, 110, 110, 110, 700),
        ]
    )
    rows = _to_rows("005930", df)
    assert [r["trade_date"] for r in rows] == [date(2026, 8, 17), date(2026, 8, 19)]


def test_nan_close_is_dropped() -> None:
    df = _df([("2026-08-18", 100, 110, 95, 105, 1000)])
    df.loc[df.index[0], "종가"] = float("nan")
    assert _to_rows("005930", df) == []


def test_nan_in_optional_columns_becomes_none() -> None:
    """종가만 있으면 행을 살린다. 나머지는 null로 둔다."""
    df = _df([("2026-08-18", 100, 110, 95, 105, 1000)])
    df.loc[df.index[0], "거래량"] = float("nan")
    rows = _to_rows("005930", df)
    assert len(rows) == 1
    assert rows[0]["volume"] is None
    assert rows[0]["close"] == 105


@pytest.mark.parametrize("empty", [None, pd.DataFrame()])
def test_empty_input_returns_no_rows(empty) -> None:
    """상장 직후 종목은 구간에 데이터가 없다. 오류가 아니라 빈 결과다."""
    assert _to_rows("005930", empty) == []
