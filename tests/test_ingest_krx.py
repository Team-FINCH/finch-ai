"""KRX OpenAPI 어댑터 테스트.

네트워크와 DB 없이 도는 것만 둔다. HTTP는 httpx.MockTransport로 대체한다.
"""

from __future__ import annotations

from datetime import date

import httpx
import pytest

from ingest.krx import (
    BASE_URL,
    INDEX_CODES,
    _fetch,
    _index_rows,
    _int,
    _marketcap_updates,
    _num,
    _retry_after,
    _yyyymmdd,
)


def _idx(name: str, close: str, bas_dd: str = "20260818") -> dict:
    """지수 응답 한 행. 실제 페이로드 컬럼 그대로."""
    return {
        "BAS_DD": bas_dd,
        "IDX_CLSS": "KOSPI",
        "IDX_NM": name,
        "CLSPRC_IDX": close,
        "CMPPREVDD_IDX": "10.00",
        "FLUC_RT": "0.30",
        "OPNPRC_IDX": close,
        "HGPRC_IDX": close,
        "LWPRC_IDX": close,
        "ACC_TRDVOL": "1000",
        "ACC_TRDVAL": "1000",
        "MKTCAP": "1000",
    }


def _stk(ticker: str, cap: str, shares: str) -> dict:
    return {
        "BAS_DD": "20260818",
        "ISU_CD": ticker,
        "ISU_NM": "테스트",
        "MKT_NM": "KOSPI",
        "SECT_TP_NM": "",
        "TDD_CLSPRC": "268500",
        "MKTCAP": cap,
        "LIST_SHRS": shares,
    }


# ── 파싱 ──────────────────────────────────────────────────────────────────────
def test_yyyymmdd() -> None:
    assert _yyyymmdd(date(2026, 8, 18)) == "20260818"


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("3,251.23", 3251.23), ("268500", 268500.0), ("-", None), ("", None), (None, None)],
)
def test_num_handles_commas_and_missing(raw, expected) -> None:
    assert _num(raw) == expected


def test_int_keeps_full_precision_of_market_cap() -> None:
    """시가총액은 1.5경대다. float를 거치면 정밀도가 위태롭다."""
    assert _int("1,569,725,806,248,000") == 1569725806248000


def test_int_rejects_garbage() -> None:
    assert _int("-") is None and _int(None) is None


# ── 지수 이름 매칭 ─────────────────────────────────────────────────────────────
def test_benchmark_is_matched_exactly_not_by_prefix() -> None:
    """실제 페이로드(2026-07-31, 51행)에서 뽑은 이름들이다.

    접두사 매칭은 페이로드 순서상 "코스피 (외국주포함)"을 먼저 집는다 — 그 행은
    종가가 비어 있다. 규모로 걸러낼 수도 없다: "코스피 100"(8179.44)이
    "코스피"(6595.45)보다 오히려 크다.
    """
    rows = _index_rows(
        [
            _idx("코스피 (외국주포함)", ""),
            _idx("코스피", "6,595.45"),
            _idx("코스피 200", "1,046.81"),
            _idx("코스피 100", "8,179.44"),
            _idx("코스피200제외 코스피지수", "4,026.95"),
            _idx("코스피 대형주", "7,249.63"),
            _idx("코스피 200 TOP 10", "2,486.40"),
        ]
    )
    assert rows == [
        {
            "index_code": "KOSPI",
            "trade_date": date(2026, 8, 18),
            "close": 6595.45,
        }
    ]


def test_index_code_is_the_one_the_risk_engine_looks_up() -> None:
    assert INDEX_CODES["코스피"] == "KOSPI"


def test_duplicate_names_collapse_to_one_row() -> None:
    """한 statement에 중복 키가 들어가면 upsert가 통째로 실패한다."""
    rows = _index_rows([_idx("코스피", "3,251.23"), _idx("코스피", "9,999.99")])
    assert len(rows) == 1
    assert rows[0]["close"] == 3251.23


def test_index_rows_drop_unusable_close() -> None:
    assert _index_rows([_idx("코스피", "-"), _idx("코스피", "0")]) == []


def test_index_trade_date_comes_from_the_payload() -> None:
    rows = _index_rows([_idx("코스피", "3,251.23", bas_dd="20240102")])
    assert rows[0]["trade_date"] == date(2024, 1, 2)


# ── 시가총액 ──────────────────────────────────────────────────────────────────
def test_marketcap_updates_keyed_by_ticker() -> None:
    assert _marketcap_updates([_stk("005930", "1569725806248000", "5846278608")]) == {
        "005930": {
            "ticker": "005930",
            "market_cap": 1569725806248000,
            "listed_shares": 5846278608,
        }
    }


def test_marketcap_ticker_stays_a_six_char_string() -> None:
    """정수로 다루면 선행 0이 사라져 마스터와 매칭되지 않는다."""
    out = _marketcap_updates([_stk("000660", "1", "1")])
    assert list(out) == ["000660"]


def test_marketcap_skips_rows_without_a_cap() -> None:
    assert _marketcap_updates([_stk("005930", "-", "100")]) == {}


def test_both_markets_merge_into_one_mapping() -> None:
    """코스피 942 + 코스닥 1,821을 합쳐야 마스터 2,596을 덮는다."""
    merged = _marketcap_updates([_stk("005930", "10", "1")]) | _marketcap_updates(
        [_stk("035720", "20", "2")]
    )
    assert set(merged) == {"005930", "035720"}


# ── HTTP ─────────────────────────────────────────────────────────────────────
def _client(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


async def test_fetch_sends_key_in_header_never_in_query(monkeypatch) -> None:
    """httpx는 전체 URL을 로그에 남긴다. 키가 쿼리에 실리면 그대로 샌다."""
    monkeypatch.setattr("ingest.krx.settings.krx_api_key", "secret-key")
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["auth"] = request.headers.get("AUTH_KEY")
        return httpx.Response(200, json={"OutBlock_1": [_idx("코스피", "1")]})

    async with _client(handler) as client:
        rows = await _fetch(client, "/idx/kospi_dd_trd", date(2026, 8, 18))

    assert seen["auth"] == "secret-key"
    assert "secret-key" not in seen["url"]
    assert seen["url"] == f"{BASE_URL}/idx/kospi_dd_trd?basDd=20260818"
    assert len(rows) == 1


async def test_holiday_is_an_empty_list_not_an_error() -> None:
    """휴장일은 200 + 빈 OutBlock_1로 온다. 실패로 처리하면 안 된다."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"OutBlock_1": []})

    async with _client(handler) as client:
        assert await _fetch(client, "/idx/kospi_dd_trd", date(2026, 8, 15)) == []


async def test_missing_outblock_key_is_also_empty() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={})

    async with _client(handler) as client:
        assert await _fetch(client, "/sto/stk_bydd_trd", date(2026, 8, 18)) == []


async def test_429_is_retried_after_honoring_retry_after(monkeypatch) -> None:
    slept: list[float] = []

    async def fake_sleep(s: float) -> None:
        slept.append(s)

    monkeypatch.setattr("ingest.krx.asyncio.sleep", fake_sleep)
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(429, headers={"Retry-After": "3"}, json={})
        return httpx.Response(200, json={"OutBlock_1": [_idx("코스피", "1")]})

    async with _client(handler) as client:
        rows = await _fetch(client, "/idx/kospi_dd_trd", date(2026, 8, 18))

    assert calls["n"] == 2
    assert slept == [3.0]
    assert len(rows) == 1


async def test_http_error_raises() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="boom")

    async with _client(handler) as client:
        with pytest.raises(httpx.HTTPStatusError):
            await _fetch(client, "/idx/kospi_dd_trd", date(2026, 8, 18))


@pytest.mark.parametrize(
    ("raw", "expected"), [("3", 3.0), (None, 1.0), ("0", 1.0), ("9999", 60.0)]
)
def test_retry_after_is_clamped(raw, expected) -> None:
    headers = httpx.Headers({} if raw is None else {"Retry-After": raw})
    assert _retry_after(headers) == expected
