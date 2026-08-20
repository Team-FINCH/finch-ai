"""DART 재무제표 어댑터 테스트.

네트워크와 DB 없이 도는 것만 둔다. HTTP는 httpx.MockTransport로 대체한다.
"""

from __future__ import annotations

from datetime import date

import httpx
import pytest

from ingest.financials import (
    FS_PRIORITY,
    STATUS_OK,
    default_year,
    fetch,
    parse_amount,
    pick_equity,
)


def _row(fs_div: str, amount: str, account: str = "자본총계") -> dict:
    return {
        "fs_div": fs_div,
        "sj_div": "BS",
        "sj_nm": "재무상태표",
        "account_nm": account,
        "thstrm_amount": amount,
    }


# ── 금액 파싱 ─────────────────────────────────────────────────────────────────
@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("436,320,337,000,000", 436_320_337_000_000),  # 삼성전자 2025 연결
        ("254,330,083,000,000", 254_330_083_000_000),  # 같은 보고서의 별도
        ("1234", 1234),
        ("-1,234", -1234),  # 완전자본잠식
        ("△1,234", -1234),  # 회계 관행의 음수 표기
        ("  9,797,343,882,863  ", 9_797_343_882_863),
        ("", None),
        ("-", None),
        (None, None),
        ("N/A", None),
    ],
)
def test_parse_amount(raw: object, expected: int | None) -> None:
    """쉼표 문자열이고 결측이 ""·"-"로 온다. int()를 그냥 부르면 적재가 통째로 죽는다."""
    assert parse_amount(raw) == expected


# ── 연결/별도 선택 ────────────────────────────────────────────────────────────
def test_pick_equity_prefers_consolidated() -> None:
    """같은 보고서에 둘 다 오면 연결이다. 삼성전자 2025년은 1.7배 차이라 섞이면 안 된다."""
    rows = [_row("OFS", "254,330,083,000,000"), _row("CFS", "436,320,337,000,000")]
    assert pick_equity(rows) == (436_320_337_000_000, "CFS")


def test_pick_equity_falls_back_to_separate() -> None:
    """지주회사처럼 별도만 내는 곳이 있다. 연결만 고집하면 통째로 빠진다."""
    assert pick_equity([_row("OFS", "1,000")]) == (1000, "OFS")


def test_pick_equity_ignores_other_accounts() -> None:
    """자본총계만 본다. 같은 응답에 자산총계·부채총계가 함께 온다."""
    rows = [_row("CFS", "9,999", account="자산총계"), _row("CFS", "1,000")]
    assert pick_equity(rows) == (1000, "CFS")


def test_pick_equity_skips_unparseable_and_falls_through() -> None:
    """연결 금액이 비어 있으면 연결이 없는 것으로 보고 별도로 내려간다."""
    assert pick_equity([_row("CFS", "-"), _row("OFS", "1,000")]) == (1000, "OFS")


def test_pick_equity_returns_none_when_absent() -> None:
    """자본총계 행이 없는 응답이 있다(계정 구성이 다른 업종). 오류가 아니다."""
    assert pick_equity([_row("CFS", "1,000", account="매출액")]) is None
    assert pick_equity([]) is None


def test_pick_equity_keeps_first_of_duplicate_fs_div() -> None:
    """같은 fs_div가 여러 재무제표(sj_div)로 중복되어 온다. 먼저 온 것을 쓴다."""
    assert pick_equity([_row("CFS", "1,000"), _row("CFS", "2,000")]) == (1000, "CFS")


def test_fs_priority_is_consolidated_first() -> None:
    """폴백 순서가 뒤집히면 시장 전체 PBR 백분위의 기준이 통째로 바뀐다."""
    assert FS_PRIORITY == ("CFS", "OFS")


# ── 기본 회계연도 ─────────────────────────────────────────────────────────────
@pytest.mark.parametrize(
    ("today", "expected"),
    [
        (date(2026, 8, 19), 2025),  # 작년 사업보고서는 3월에 이미 나왔다
        (date(2026, 4, 1), 2025),
        (date(2026, 3, 31), 2024),  # 아직 제출 기한 전이라 재작년을 본다
        (date(2026, 1, 5), 2024),
    ],
)
def test_default_year(today: date, expected: int) -> None:
    assert default_year(today) == expected


# ── HTTP ─────────────────────────────────────────────────────────────────────
def _client(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


async def test_fetch_returns_rows_and_status() -> None:
    async with _client(
        lambda _: httpx.Response(200, json={"status": "000", "list": [_row("CFS", "1,000")]})
    ) as client:
        rows, status = await fetch(client, "00126380", 2025)
    assert status == STATUS_OK
    assert pick_equity(rows) == (1000, "CFS")


async def test_fetch_retries_on_dart_throttle_status(monkeypatch) -> None:
    """DART는 사용한도를 넘겨도 HTTP 200에 status "020"으로 답한다.

    status_code만 보면 429를 한 번도 못 보고 한도만 계속 깎는다.
    """
    monkeypatch.setattr("ingest.financials.asyncio.sleep", _no_sleep)
    calls: list[int] = []

    def handler(_: httpx.Request) -> httpx.Response:
        calls.append(1)
        if len(calls) < 3:
            return httpx.Response(200, json={"status": "020", "message": "사용한도 초과"})
        return httpx.Response(200, json={"status": "000", "list": [_row("CFS", "7")]})

    async with _client(handler) as client:
        rows, status = await fetch(client, "00126380", 2025)

    assert len(calls) == 3
    assert status == STATUS_OK
    assert pick_equity(rows) == (7, "CFS")


async def test_fetch_reports_no_data_status_without_retrying(monkeypatch) -> None:
    """미제출("013")은 정상이다. 재시도하면 없는 답을 기다리며 한도만 태운다."""
    monkeypatch.setattr("ingest.financials.asyncio.sleep", _no_sleep)
    calls: list[int] = []

    def handler(_: httpx.Request) -> httpx.Response:
        calls.append(1)
        return httpx.Response(200, json={"status": "013", "message": "조회된 데이터가 없습니다."})

    async with _client(handler) as client:
        rows, status = await fetch(client, "00126380", 2025)

    assert (len(calls), rows, status) == (1, [], "013")


async def test_fetch_gives_up_after_retry_max(monkeypatch) -> None:
    """한도가 계속 막혀 있으면 빈 결과로 넘긴다 — 그 종목만 미제출로 세고 계속 돈다."""
    monkeypatch.setattr("ingest.financials.asyncio.sleep", _no_sleep)

    async with _client(
        lambda _: httpx.Response(200, json={"status": "020"})
    ) as client:
        rows, status = await fetch(client, "00126380", 2025)

    assert (rows, status) == ([], "020")


async def _no_sleep(_seconds: float) -> None:
    """재시도 대기를 건너뛴다. 실제로 자면 테스트가 분 단위가 된다."""
