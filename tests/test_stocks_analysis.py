"""종목 AI 분석 엔드포인트 테스트.

LLM도 검색도 붙지 않은 환경에서 도는 것이 요점이다. 키가 없고 임베딩이 없는
상태가 지금의 기본값이므로, 그 상태에서 무엇이 되고 무엇이 안 되는지를 고정한다.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.api.main import create_app
from app.api.routes.stocks import UpcomingEvent
from app.core.db import get_session
from app.core.enums import EventType
from app.core.models import AIFeedback, AIResponse
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
            "동일 업황 노출이 겹칩니다. 분산 효과는 제한적입니다. "
            "업황 지표를 함께 확인하십시오."
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
    monkeypatch.setattr("app.api.routes.stocks.get_active_thesis", _no_thesis)
    monkeypatch.setattr("app.api.routes.stocks._upcoming_events", _no_upcoming_events)
    app = create_app()
    session = FeedbackSession()
    app.dependency_overrides[get_session] = lambda: session
    with TestClient(app) as test_client:
        test_client.llm = fake
        test_client.db = session
        yield test_client


async def _no_hits(*_: Any, **__: Any) -> list[dict]:
    return []


async def _no_thesis(*_: Any, **__: Any) -> None:
    return None


async def _no_upcoming_events(*_: Any, **__: Any) -> list[Any]:
    return []


def _post(client: TestClient, body: dict, *, user: str = HOLDER):
    return client.post(URL, json=body, headers={"X-User-Id": user})


# ── 기본 동작 ────────────────────────────────────────────
def test_요청한_섹션만_돌려준다(client):
    response = _post(client, {"sections": ["current", "risks"]})
    assert response.status_code == 200
    sections = response.json()["content"]["sections"]
    assert set(sections) == {"current", "risks"}
    assert sections["current"]["title"] == "현재 상황"
    assert sections["risks"]["title"] == "확인된 위험 요인"


def test_일반_섹션은_설정하지_않은_조건부_키를_생략한다(client):
    section = _post(client, {"sections": ["current"]}).json()["content"]["sections"]["current"]

    assert {"thesis", "supporting", "challenging", "events"}.isdisjoint(section)


def test_섹션_명칭은_출처_귀속형이다(client):
    """"긍정/부정 요인"은 의견 제시로 읽힌다. 명세 §3."""
    sections = _post(client, {"sections": ["attention", "risks"]}).json()["content"]["sections"]
    assert sections["attention"]["title"] == "시장이 주목하는 요인"
    assert "긍정" not in sections["attention"]["title"]
    assert "부정" not in sections["risks"]["title"]


def test_생략하면_전체_섹션을_시도한다(client):
    sections = _post(client, {}).json()["content"]["sections"]
    assert set(sections) == {
        "current", "changes", "attention", "risks",
        "my_impact", "thesis_check", "next_events",
    }


def test_보유_종목이면_수치가_치환된다(client):
    section = _post(client, {"sections": ["my_impact"]}).json()["content"]["sections"]["my_impact"]
    assert "{{" not in section["text"]
    metrics = [s for s in section["segments"] if s["type"] == "metric"]
    assert metrics
    assert metrics[0]["source"] == "portfolio_engine"
    assert "".join(s["value"] for s in section["segments"]) == section["text"]


def test_비보유_종목의_개인화_섹션은_null이다(client):
    """에러가 아니다. 명세 §3."""
    body = _post(client, {"sections": ["my_impact", "current"]}, user=STRANGER).json()
    assert body["content"]["sections"]["my_impact"] is None
    assert body["content"]["sections"]["current"] is not None


def test_personalize가_false면_개인화_섹션을_만들지_않는다(client):
    sections = _post(
        client, {"sections": ["my_impact", "thesis_check"], "personalize": False}
    ).json()["content"]["sections"]
    assert sections == {"my_impact": None, "thesis_check": None}
    assert client.llm.calls == []


def test_논지가_없으면_thesis_check는_null이다(client):
    body = _post(client, {"sections": ["thesis_check"]}).json()
    assert body["content"]["sections"]["thesis_check"] is None


def test_논지가_있으면_기록을_함께_돌려준다(client, monkeypatch):
    class Thesis:
        text = "HBM 구조적 성장에 베팅"
        recorded_at = datetime(2026, 3, 11, 10, 22)
        source = "user_stated"

    async def _thesis(*_: Any, **__: Any):
        return Thesis()

    monkeypatch.setattr("app.api.routes.stocks.get_active_thesis", _thesis)
    section = _post(client, {"sections": ["thesis_check"]}).json()["content"]["sections"]["thesis_check"]
    assert section["thesis"]["text"] == "HBM 구조적 성장에 베팅"
    assert section["supporting"] == []
    # 논지 원문이 프롬프트에 실려야 대조가 성립한다.
    assert "HBM 구조적 성장에 베팅" in client.llm.calls[0]["user"]


def test_논지와_관련된_검색_근거를_방향별로_분류한다(client, monkeypatch):
    class Thesis:
        text = "HBM 구조적 성장에 베팅"
        recorded_at = datetime(2026, 3, 11, 10, 22)
        source = "user_stated"

    async def _thesis(*_: Any, **__: Any):
        return Thesis()

    async def _hits(*_: Any, **__: Any) -> list[dict]:
        return [
            {
                "text": "HBM 사업 매출이 확대되었다",
                "ticker": "005930",
                "title": "분기보고서",
                "source": "DART",
                "published_at": datetime(2026, 8, 14, 16, 12),
                "similarity": 0.91,
            }
        ]

    monkeypatch.setattr("app.api.routes.stocks.get_active_thesis", _thesis)
    monkeypatch.setattr("app.api.routes.stocks.search", _hits)

    section = _post(client, {"sections": ["thesis_check"]}).json()["content"]["sections"][
        "thesis_check"
    ]

    assert section["supporting"] == [
        {
            "citation_id": "cit_1",
            "title": "분기보고서",
            "source": "DART",
            "rationale": "HBM 사업 확대가 기록된 투자 이유를 뒷받침합니다.",
        }
    ]
    assert section["challenging"] == []


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
    assert body["model"] == "gpt-5.4-mini"
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
