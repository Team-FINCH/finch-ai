"""종목 AI 분석 엔드포인트 테스트.

LLM도 검색도 붙지 않은 환경에서 도는 것이 요점이다. 키가 없고 임베딩이 없는
상태가 지금의 기본값이므로, 그 상태에서 무엇이 되고 무엇이 안 되는지를 고정한다.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.api.main import create_app
from app.api.routes.stocks import (
    CachedAnalysis,
    UpcomingEvent,
    _cached_common_sections,
)
from app.core.db import get_session
from app.core.enums import EventType
from app.core.models import AIFeedback, AIResponse
from app.core.schemas import DataAsOf
from app.llm.client import LlmResult, NullLlmClient

URL = "/api/ai/v1/stocks/005930/analysis"
#: 시드 픽스처에 005930을 보유한 사용자. 토큰 문자열이 곧 user_id다.
HOLDER = "golden_1_single"
STRANGER = "no_such_user"


class FakeClient:
    """자리표시자를 쓰는 통과 가능한 응답만 돌려준다."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def generate(self, **kwargs: Any) -> LlmResult:
        self.calls.append(kwargs)
        uses_weight = "{{weight}}" in kwargs["user"]
        narrative = (
            "이 종목은 포트폴리오의 {{weight}}를 차지합니다. "
            if uses_weight
            else "확인된 공시가 없습니다. "
        )
        # thesis_check가 4문장을 요구하므로 전 섹션을 4문장으로 맞춘다(§2).
        narrative += (
            "동일 업황 노출이 겹칩니다. 분산 효과는 제한적입니다. 업황 지표를 함께 확인하십시오."
        )
        payload = {
            "narrative": narrative,
            "used_placeholders": ["weight"] if uses_weight else [],
            "used_citations": [],
        }
        if "evidence_classification" in kwargs["schema"]["properties"]:
            payload["evidence_classification"] = (
                [
                    {
                        "citation_id": "cit_1",
                        "stance": "supporting",
                        "rationale": "HBM 사업 확대가 기록된 투자 이유를 뒷받침합니다.",
                    }
                ]
                if "[^cit_1]" in kwargs["user"]
                else []
            )
        return LlmResult(payload=payload)


class FeedbackSession:
    def __init__(self) -> None:
        self.added: list[Any] = []

    def add(self, obj: Any) -> None:
        self.added.append(obj)

    async def commit(self) -> None:
        return None

    async def rollback(self) -> None:
        return None

    async def scalar(self, statement: Any) -> Any:
        if "ai_responses" in str(statement):
            row = next((row for row in self.added if isinstance(row, AIResponse)), None)
            return row.user_id if row else None
        return next((row for row in self.added if isinstance(row, AIFeedback)), None)


@pytest.fixture
def client(monkeypatch):
    fake = FakeClient()
    monkeypatch.setattr("app.api.routes.stocks.get_llm_client", lambda: fake)
    monkeypatch.setattr("app.api.routes.stocks.search", _no_hits)
    monkeypatch.setattr("app.api.routes.stocks._upcoming_events", _no_upcoming_events)
    monkeypatch.setattr("app.api.routes.stocks._cached_common_sections", _no_cache)
    app = create_app()
    session = FeedbackSession()
    app.dependency_overrides[get_session] = lambda: session
    with TestClient(app) as test_client:
        test_client.llm = fake
        test_client.db = session
        yield test_client


async def _no_hits(*_: Any, **__: Any) -> list[dict]:
    return []


async def _no_upcoming_events(*_: Any, **__: Any) -> list[Any]:
    return []


async def _no_cache(*_: Any, **__: Any) -> None:
    return None


def _post(client: TestClient, body: dict, *, user: str = HOLDER):
    return client.post(URL, json=body, headers={"X-User-Id": user})


# ── 기본 동작 ────────────────────────────────────────────
def test_요청한_섹션만_돌려준다(client):
    response = _post(client, {"sections": ["current", "risks"]})
    assert response.status_code == 200
    sections = response.json()["content"]["sections"]
    assert set(sections) == {"current", "risks"}
    assert sections["current"]["title"] == "현재 상황"
    assert sections["risks"]["title"] == "확인해볼 위험"


def test_공통_섹션_캐시는_LLM을_다시_호출하지_않는다(client, monkeypatch):
    cached_at = datetime(2026, 8, 28, 9, 0).astimezone()

    async def _cached(*_: Any, **__: Any) -> CachedAnalysis:
        return CachedAnalysis(
            name="삼성전자",
            sections={
                "current": {
                    "title": "현재 상황",
                    "text": "캐시된 분석입니다.",
                    "segments": [],
                    "cached": True,
                    "cached_at": cached_at.isoformat(),
                }
            },
            citations=[],
            data_as_of=DataAsOf(),
            cached_at=cached_at,
        )

    monkeypatch.setattr("app.api.routes.stocks._cached_common_sections", _cached)

    body = _post(client, {"sections": ["current"]}).json()

    assert body["cached"] is True
    assert body["content"]["name"] == "삼성전자"
    assert body["content"]["sections"]["current"]["cached"] is True
    assert client.llm.calls == []


@pytest.mark.anyio
async def test_같은_종목과_프롬프트의_최근_공통_섹션을_찾는다(monkeypatch):
    now = datetime(2026, 8, 28, 12, 0, tzinfo=timezone(timedelta(hours=9)))
    created_at = datetime(2026, 8, 28, 10, 30)
    row = SimpleNamespace(
        created_at=created_at,
        payload={
            "content": {
                "ticker": "005930",
                "name": "삼성전자",
                "sections": {
                    "current": {
                        "title": "현재 상황",
                        "text": "저장된 분석",
                        "segments": [],
                        "cached": False,
                        "cached_at": None,
                    }
                },
            },
            "citations": [],
            "data_as_of": {},
        },
    )

    class Rows:
        def all(self):
            return [row]

    class Session:
        async def scalars(self, _statement):
            return Rows()

    monkeypatch.setattr("app.api.routes.stocks.prompt_version_for", lambda _endpoint: "prompt_test")

    cached = await _cached_common_sections(Session(), "005930", {"current"}, now=now)

    assert cached is not None
    assert cached.name == "삼성전자"
    assert cached.cached_at.utcoffset() == timedelta(hours=9)
    assert cached.sections["current"]["cached"] is True
    assert cached.sections["current"]["cached_at"].endswith("+09:00")


@pytest.mark.anyio
async def test_캐시_저장소가_실패하면_새_분석으로_복귀한다():
    class BrokenSession:
        async def scalars(self, _statement):
            raise OSError("cache unavailable")

    cached = await _cached_common_sections(
        BrokenSession(),
        "005930",
        {"current"},
        now=datetime.now(UTC),
    )

    assert cached is None


def test_일반_섹션은_설정하지_않은_조건부_키를_생략한다(client):
    section = _post(client, {"sections": ["current"]}).json()["content"]["sections"]["current"]

    assert {"thesis", "supporting", "challenging", "events"}.isdisjoint(section)


def test_섹션_명칭은_출처_귀속형이다(client):
    """ "긍정/부정 요인"은 의견 제시로 읽힌다. 명세 §3."""
    sections = _post(client, {"sections": ["attention", "risks"]}).json()["content"]["sections"]
    assert sections["attention"]["title"] == "시장이 주목하는 요인"
    assert "긍정" not in sections["attention"]["title"]
    assert "부정" not in sections["risks"]["title"]


def test_생략하면_전체_섹션을_시도한다(client):
    sections = _post(client, {}).json()["content"]["sections"]
    # 개인 섹션(my_impact·thesis_check)은 종목 단위 분석이라 응답에 없다.
    assert set(sections) == {"current", "changes", "attention", "risks", "next_events"}


def test_개인_섹션을_요청해도_만들지_않고_응답에서_뺀다(client):
    """종목 분석은 사용자와 무관한 종목 단위 정보다. 공시·뉴스만으로 만든다."""
    body = _post(client, {"sections": ["my_impact", "thesis_check", "current"]}).json()
    assert set(body["content"]["sections"]) == {"current"}
    assert len(client.llm.calls) == 1  # 공통 섹션 하나만 생성한다


def test_확정된_미래_일정을_next_events에_연결한다(client, monkeypatch):
    async def _events(*_: Any, **__: Any) -> list[UpcomingEvent]:
        return [
            UpcomingEvent(
                id="evt_1",
                type=EventType.EARNINGS,
                title="3분기 실적 발표",
                event_date=date(2026, 10, 30),
                confirmed=True,
                days_until=63,
            )
        ]

    monkeypatch.setattr("app.api.routes.stocks._upcoming_events", _events)

    section = _post(client, {"sections": ["next_events"]}).json()["content"]["sections"][
        "next_events"
    ]

    assert section["events"] == [
        {
            "id": "evt_1",
            "type": "earnings",
            "title": "3분기 실적 발표",
            "event_date": "2026-10-30",
            "confirmed": True,
            "days_until": 63,
        }
    ]
    assert "2026년 10월 30일 3분기 실적 발표" in client.llm.calls[0]["user"]


# ── 봉투와 근거 ──────────────────────────────────────────
def test_검색_결과가_근거로_실린다(client, monkeypatch):
    async def _hits(*_: Any, **__: Any) -> list[dict]:
        return [
            {
                "text": "3분기 HBM 매출 비중이 확대되었다",
                "ticker": "005930",
                "title": "분기보고서",
                "published_at": datetime(2026, 8, 14, 16, 12),
                "similarity": 0.87,
            }
        ]

    monkeypatch.setattr("app.api.routes.stocks.search", _hits)
    body = _post(client, {"sections": ["current"]}).json()
    assert body["citations"][0]["id"] == "cit_1"
    assert body["citations"][0]["title"] == "분기보고서"
    assert body["data_as_of"]["filings"] is not None
    # 원문이 프롬프트에 실려야 모델이 근거를 볼 수 있다.
    assert "3분기 HBM 매출 비중이 확대되었다" in client.llm.calls[0]["user"]


def test_검색_결과가_없어도_생성은_진행한다(client):
    """§7 — RAG 0건은 실패가 아니다. 한계를 밝히고 계속한다."""
    body = _post(client, {"sections": ["current"]}).json()
    assert body["citations"] == []
    assert body["content"]["sections"]["current"] is not None
    assert "관련 자료를 찾지 못했다" in client.llm.calls[0]["user"]


def test_면책_문구와_모델이_봉투에_들어간다(client):
    body = _post(client, {"sections": ["current"]}).json()
    assert body["model"] == "gpt-5-nano"
    assert body["disclaimer"]
    assert body["request_id"].startswith("req_")


def test_응답을_저장해_피드백을_받는다(client):
    response = _post(client, {"sections": ["current"]})
    request_id = response.json()["request_id"]

    row = next(row for row in client.db.added if isinstance(row, AIResponse))
    assert row.request_id == request_id
    assert row.endpoint == "stocks.analysis"
    feedback = client.post(
        "/api/ai/v1/feedback",
        headers={"X-User-Id": HOLDER},
        json={"request_id": request_id, "rating": "up", "reasons": []},
    )
    assert feedback.status_code == 200


# ── 응답 키 계약 (GitLab #58) ────────────────────────────
def test_스칼라_키는_값이_없어도_응답에_남는다(client):
    """exclude_unset이 중첩까지 전파되면 cached·direction 같은 키가 통째로 사라진다.

    계약상 필수인 키가 응답에서 빠지면 프론트가 6종 전부에 방어 코드를 진다.
    값이 없으면 null로 실려야 한다.
    """
    section = _post(client, {"sections": ["current"]}).json()["content"]["sections"]["current"]

    assert "cached" in section
    assert "cached_at" in section
    assert "title" in section
    for segment in section["segments"]:
        for key in ("raw", "unit", "source", "direction"):
            assert key in segment, f"{key}가 세그먼트에서 빠졌다"


def test_없는_섹션은_여전히_생략된다(client):
    """섹션 단위 생략은 정상 동작이다. 키 누락을 고치면서 이걸 깨면 안 된다."""
    sections = _post(client, {"sections": ["current"]}).json()["content"]["sections"]

    assert "current" in sections
    assert "changes" not in sections
    assert "my_impact" not in sections


# ── 거부 경로 ────────────────────────────────────────────
def test_종목코드가_6자리가_아니면_거부한다(client):
    response = client.post(
        "/api/ai/v1/stocks/AAPL/analysis",
        json={},
        headers={"X-User-Id": HOLDER},
    )
    assert response.status_code == 400
    assert response.json()["code"] == "INVALID_REQUEST"


def test_모르는_섹션은_거부한다(client):
    response = _post(client, {"sections": ["moon_phase"]})
    assert response.status_code == 400
    assert response.json()["detail"]["sections"] == ["moon_phase"]


def test_토큰이_없으면_거부한다(client):
    assert client.post(URL, json={}).status_code == 401


def test_키가_없으면_지어내지_않고_실패한다(monkeypatch):
    monkeypatch.setattr("app.api.routes.stocks.get_llm_client", NullLlmClient)
    app = create_app()
    app.dependency_overrides[get_session] = lambda: None
    with TestClient(app) as test_client:
        response = _post(test_client, {"sections": ["current"]})
    assert response.status_code == 409
    assert response.json()["code"] == "INSUFFICIENT_DATA"
    body = response.json()
    # 사유는 detail 로만 나간다. message 는 사용자에게 그대로 보이는 문구다(백엔드 §1.3).
    assert body["detail"]["reason"] == "llm_key_missing"
    assert "LLM" not in body["message"]


@pytest.mark.anyio
async def test_공통_캐시는_키마다_최근_성공_행에서_가져온다(monkeypatch):
    """한 섹션이 null 로 저장돼도 나머지 섹션의 캐시는 살아야 한다."""
    now = datetime(2026, 8, 28, 12, 0, tzinfo=timezone(timedelta(hours=9)))

    def _row(hour: int, sections: dict[str, Any]) -> SimpleNamespace:
        return SimpleNamespace(
            created_at=datetime(2026, 8, 28, hour, 0),
            payload={
                "content": {"ticker": "005930", "name": "삼성전자", "sections": sections},
                "citations": [],
                "data_as_of": {},
            },
        )

    section = {"title": "t", "text": "x", "segments": [], "cached": False, "cached_at": None}
    rows = [
        _row(11, {"current": None, "changes": section}),  # 최신 행은 current 실패
        _row(10, {"current": section, "changes": section}),
    ]

    class Session:
        async def scalars(self, _statement):
            return SimpleNamespace(all=lambda: rows)

    monkeypatch.setattr("app.api.routes.stocks.prompt_version_for", lambda _endpoint: "prompt_test")

    cached = await _cached_common_sections(
        Session(), "005930", {"current", "changes", "risks"}, now=now
    )

    assert cached is not None
    assert set(cached.sections) == {"current", "changes"}  # risks 는 호출부가 생성
    assert cached.sections["changes"]["cached_at"].startswith("2026-08-28T11")
    assert cached.sections["current"]["cached_at"].startswith("2026-08-28T10")


@pytest.mark.anyio
async def test_공통_캐시는_복사된_섹션을_원본으로_치지_않는다(monkeypatch):
    """캐시 히트 행의 섹션은 `cached: true` 다. 그걸 다시 집으면 TTL 이 영원히 늘어난다.

    반대로 일부만 새로 만든 행의 새 섹션(`cached: false`)은 원본이어야 한다 — 행 단위
    플래그로 거르면 그 섹션을 매 요청 다시 만든다.
    """
    now = datetime(2026, 8, 28, 12, 0, tzinfo=timezone(timedelta(hours=9)))
    fresh = {"title": "t", "text": "새로 만듦", "segments": [], "cached": False, "cached_at": None}
    copied = {"title": "t", "text": "복사본", "segments": [], "cached": True, "cached_at": "x"}
    rows = [
        SimpleNamespace(
            created_at=datetime(2026, 8, 28, 11, 0),
            payload={
                "content": {
                    "ticker": "005930",
                    "name": "삼성전자",
                    "sections": {"current": copied, "risks": fresh},
                },
                "citations": [],
                "data_as_of": {},
            },
        ),
        SimpleNamespace(
            created_at=datetime(2026, 8, 28, 10, 0),
            payload={
                "content": {
                    "ticker": "005930",
                    "name": "삼성전자",
                    "sections": {"current": fresh, "risks": fresh},
                },
                "citations": [],
                "data_as_of": {},
            },
        ),
    ]

    class Session:
        async def scalars(self, _statement):
            return SimpleNamespace(all=lambda: rows)

    monkeypatch.setattr("app.api.routes.stocks.prompt_version_for", lambda _endpoint: "prompt_test")
    cached = await _cached_common_sections(Session(), "005930", {"current", "risks"}, now=now)

    assert cached is not None
    assert cached.sections["risks"]["cached_at"].startswith("2026-08-28T11")  # 새 섹션은 원본
    assert cached.sections["current"]["cached_at"].startswith("2026-08-28T10")  # 복사본은 건너뜀


def test_요청_시점_생성은_요청자_예산이_아니라_배치_장부에서_나간다(client, monkeypatch):
    """종목 분석은 종목 단위 정보라 첫 조회자가 대신 만들어도 그 사람 예산을 깎지 않는다."""
    from app.api.routes import stocks

    entered: list[str] = []

    class Guard:
        async def enter_system(self, user_id, endpoint, *, now, budget):
            entered.append(user_id)
            return object()

    class Counter:
        guard = Guard()

    monkeypatch.setattr(stocks, "current_usage", lambda: Counter())
    monkeypatch.setattr(stocks, "reset_usage", lambda token: None)

    _post(client, {"sections": ["current"]})

    assert entered == ["system:analysis-batch"]


@pytest.mark.anyio
async def test_검사에_실패한_섹션은_한_시간_동안_다시_만들지_않는다(monkeypatch):
    """null 로 저장된 섹션을 매 요청 다시 만들다 또 실패하면 화면이 늦고 토큰이 샌다."""
    now = datetime(2026, 8, 28, 12, 0, tzinfo=timezone(timedelta(hours=9)))
    fresh = {"title": "t", "text": "새로 만듦", "segments": [], "cached": False, "cached_at": None}
    row = SimpleNamespace(
        created_at=datetime(2026, 8, 28, 11, 30),
        cached=False,  # 생성을 시도한 행. attention 은 검사에 걸려 null
        payload={
            "content": {
                "ticker": "005930",
                "name": "삼성전자",
                "sections": {"current": fresh, "attention": None},
            },
            "citations": [],
            "data_as_of": {},
        },
    )

    class Session:
        async def scalars(self, _statement):
            return SimpleNamespace(all=lambda: [row])

    monkeypatch.setattr("app.api.routes.stocks.prompt_version_for", lambda _endpoint: "prompt_test")
    cached = await _cached_common_sections(Session(), "005930", {"current", "attention"}, now=now)

    assert cached is not None
    assert set(cached.sections) == {"current"}
    assert cached.failed == {"attention"}  # 호출부가 생성 대상에서 뺀다

    # 캐시 TTL 이 지나면 원본이 만료돼 자연히 다시 만든다 (그 전 재시도는 배치 몫)
    later = now + timedelta(hours=25)
    cached = await _cached_common_sections(Session(), "005930", {"current", "attention"}, now=later)
    assert cached is not None and cached.failed == frozenset()


def test_요청_경로는_실패한_섹션을_다시_만들지_않고_배치만_다시_시도한다(client, monkeypatch):
    from app.api.routes import stocks

    async def _cached(*_: Any, **__: Any) -> CachedAnalysis:
        return CachedAnalysis(
            name="삼성전자",
            sections={},
            citations=[],
            data_as_of=DataAsOf(),
            cached_at=datetime(2026, 8, 28, 9, 0).astimezone(),
            failed=frozenset({"current"}),
        )

    monkeypatch.setattr(stocks, "_cached_common_sections", _cached)
    monkeypatch.setattr(stocks, "current_usage", lambda: None)

    body = _post(client, {"sections": ["current"]}).json()
    assert body["content"]["sections"]["current"] is None
    assert client.llm.calls == []  # 요청 경로는 기다리지 않는다


@pytest.mark.anyio
async def test_공통_캐시_조회는_SQL_에서_종목으로_거른다(monkeypatch):
    """다른 종목의 히트 기록 20행에 밀려 원본을 못 찾던 버그. 조회 조건에 종목이 있어야 한다."""
    captured: list[str] = []

    class Session:
        async def scalars(self, statement):
            captured.append(str(statement.compile(compile_kwargs={"literal_binds": True})))
            return SimpleNamespace(all=lambda: [])

    monkeypatch.setattr("app.api.routes.stocks.prompt_version_for", lambda _endpoint: "prompt_test")
    now = datetime(2026, 8, 28, 12, 0, tzinfo=timezone(timedelta(hours=9)))
    await _cached_common_sections(Session(), "005930", {"current"}, now=now)

    assert captured and "'005930'" in captured[0] and "ticker" in captured[0]
