"""공시목록 → events 적재기 테스트.

네트워크도 DB도 없이 돈다. DART 호출과 세션은 목으로 바꾼다 —
외부가 죽었다고 CI가 빨개지면 회귀 테스트로서 쓸모가 없다.
"""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from app.core.enums import EventType
from app.rag.dart import Filing
from ingest import events


def _filing(report_nm: str, rcept_dt: str = "20260814", ticker: str = "005930") -> Filing:
    return Filing(
        rcept_no="20260814000342",
        ticker=ticker,
        corp_name="삼성전자",
        report_nm=report_nm,
        rcept_dt=rcept_dt,
    )


TODAY = date(2026, 8, 20)


# ── 분류 ───────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "report_nm",
    [
        "사업보고서 (2025.12)",
        "반기보고서 (2026.06)",
        "분기보고서 (2026.03)",
        "매출액또는손익구조30%(대규모법인은15%)이상변동",
        "영업(잠정)실적(공정공시)",
        "연결재무제표기준영업(잠정)실적(공정공시)",
    ],
)
def test_earnings_are_top_importance(report_nm: str) -> None:
    """§5.2가 실적 발표를 1.0으로 고정한다. W16 브리핑 랭킹이 이 값을 그대로 쓴다."""
    assert events.classify(report_nm) == (EventType.EARNINGS, 1.0)


def test_correction_prefix_keeps_the_earnings_tier() -> None:
    """[기재정정]은 접두사일 뿐이다. §5.2의 1.0은 '실적 발표 · 실적 정정'을 함께 묶는다."""
    assert events.classify("[기재정정]반기보고서 (2026.06)") == (EventType.EARNINGS, 1.0)


@pytest.mark.parametrize(
    "report_nm",
    [
        # DART는 가운뎃점을 U+00B7(·)이 아니라 U+318D(ㆍ)로 쓴다. 설계 문서 표기를
        # 그대로 부분문자열로 넣으면 여기서 전부 놓친다.
        "단일판매ㆍ공급계약체결",
        "주요사항보고서(유상증자결정)",
        "주요사항보고서(자기주식취득결정)",
        "주식등의대량보유상황보고서(약식)",
        "임원ㆍ주요주주특정증권등소유상황보고서",
    ],
)
def test_major_filings_are_point_eight(report_nm: str) -> None:
    assert events.classify(report_nm) == (EventType.FILING, 0.8)


def test_dividend_gets_its_own_type_but_the_general_tier() -> None:
    """배당은 §5.2의 0.8 목록에 없다. 없는 등급을 지어내지 않는다."""
    assert events.classify("현금ㆍ현물배당결정") == (EventType.DIVIDEND, 0.4)


def test_unknown_filing_falls_back_to_general() -> None:
    assert events.classify("기업설명회(IR)개최(안내공시)") == (EventType.FILING, 0.4)


# ── 행 변환 ─────────────────────────────────────────────────────


def test_row_is_confirmed_and_carries_no_document() -> None:
    """접수 완료된 공시다. 원문은 받지 않으므로 document_id는 비운다."""
    row = events.to_row(_filing("단일판매ㆍ공급계약체결"), TODAY)
    assert row == {
        "event_type": "filing",
        "ticker": "005930",
        "title": "단일판매ㆍ공급계약체결",
        "event_date": date(2026, 8, 14),
        "confirmed": True,
        "importance": 0.8,
        "document_id": None,
    }


def test_padded_title_is_collapsed() -> None:
    """DART 제목은 공백으로 패딩돼 온다. 중복 판정 키라서 눌러두지 않으면 재실행이 또 넣는다."""
    row = events.to_row(_filing("증권신고서\n (지분증권)     "), TODAY)
    assert row is not None
    assert row["title"] == "증권신고서 (지분증권)"


def test_malformed_date_is_dropped() -> None:
    """event_date가 NOT NULL이다. 날짜 없는 건을 넣으면 배치 전체가 죽는다."""
    assert events.to_row(_filing("분기보고서", rcept_dt=""), TODAY) is None


def test_future_dated_filing_is_dropped() -> None:
    """목록 API는 접수 완료 건만 준다. 미래 일자는 데이터 오류지 예정 일정이 아니다."""
    ahead = (TODAY + timedelta(days=3)).strftime("%Y%m%d")
    assert events.to_row(_filing("분기보고서", rcept_dt=ahead), TODAY) is None


# ── 재실행 ─────────────────────────────────────────────────────


class _FakeSession:
    """events 표를 흉내내는 리스트. insert는 append, select는 키 튜플."""

    def __init__(self, store: list[dict]) -> None:
        self.store = store

    async def __aenter__(self) -> _FakeSession:
        return self

    async def __aexit__(self, *exc: object) -> bool:
        return False

    async def execute(self, statement: object, params: list[dict] | None = None):
        if params is not None:  # insert
            self.store.extend(params)
            return None
        return [(r["ticker"], r["event_date"], r["title"]) for r in self.store]

    async def commit(self) -> None:
        return None


@pytest.fixture
def _wired(monkeypatch: pytest.MonkeyPatch) -> list[dict]:
    """load_targets · fetch_filing_list · SessionFactory를 전부 가짜로 바꾼다."""
    store: list[dict] = []
    today = date.today()
    stamp = today.strftime("%Y%m%d")
    filings = [
        _filing("반기보고서 (2026.06)", stamp),
        _filing("단일판매ㆍ공급계약체결", stamp),
        _filing("[기재정정]단일판매ㆍ공급계약체결", stamp),
    ]

    async def _targets(limit: int) -> list[tuple[str, str]]:
        return [("005930", "00126380")]

    monkeypatch.setattr(events, "load_targets", _targets)
    monkeypatch.setattr(events, "fetch_filing_list", lambda *a, **k: filings)
    monkeypatch.setattr(events, "SessionFactory", lambda: _FakeSession(store))
    monkeypatch.setattr(events, "REQUEST_DELAY_S", 0)
    return store


async def test_second_run_inserts_nothing(_wired: list[dict]) -> None:
    """events에는 유니크 제약이 없다. 재실행이 전부 다시 들어가면 브리핑이 같은 건을 4번 띄운다."""
    first = await events.ingest(days=30, limit=10, api_key="k")
    assert first["rows"] == 3
    assert first["duplicates"] == 0
    assert len(_wired) == 3

    second = await events.ingest(days=30, limit=10, api_key="k")
    assert second["rows"] == 0
    assert second["duplicates"] == 3
    assert len(_wired) == 3  # 표가 자라지 않았다


async def test_correction_is_kept_as_a_separate_event(_wired: list[dict]) -> None:
    """[기재정정]은 title이 다르니 별건이다. 접두사를 떼어 합치면 정정 사실이 사라진다."""
    await events.ingest(days=30, limit=10, api_key="k")
    titles = sorted(r["title"] for r in _wired)
    assert titles == [
        "[기재정정]단일판매ㆍ공급계약체결",
        "단일판매ㆍ공급계약체결",
        "반기보고서 (2026.06)",
    ]


async def test_missing_api_key_reports_failure_without_touching_db(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """키가 없으면 조용히 0건 성공으로 끝나면 안 된다 — 0행 적재와 구별되지 않는다."""
    monkeypatch.setattr(events.settings, "dart_api_key", "", raising=False)
    stats = await events.ingest(days=30, limit=10, api_key=None)
    assert stats["failed"] == 1
    assert stats["rows"] == 0
