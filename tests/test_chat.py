"""Ask My Portfolio 엔드포인트 테스트.

에이전트가 도구를 고르는 부분은 가짜 클라이언트가 대신 고른다. 확인하려는 것은
모델의 판단이 아니라 그 판단 뒤의 절차다 — 도구 결과가 자리표시자를 거쳐 치환되는지,
루프가 멈추는지, 없는 것을 지어내지 않는지.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.api.main import create_app
from app.core.db import get_session
from app.core.models import AIFeedback, AIResponse
from app.llm.agent import MAX_TOOL_CALLS, MAX_TOOL_TURNS
from app.llm.client import LlmResult, NullLlmClient, ToolTurn, ToolUse

URL = "/api/ai/v1/chat"
#: 시드 픽스처에서 005930을 보유한 사용자. 토큰 문자열이 곧 user_id다.
HOLDER = "golden_1_single"


def _use(name: str, **args: Any) -> ToolUse:
    return ToolUse(id=f"tu_{name}", name=name, input=args)


class FakeClient:
    """정해진 도구를 부르고, 자리표시자를 쓰는 통과 가능한 본문을 돌려준다."""

    def __init__(self, *plan: tuple[ToolUse, ...]) -> None:
        #: 턴마다 부를 도구. 다 쓰면 end_turn으로 끝낸다.
        self.plan = list(plan)
        self.step = 0
        self.turns: list[dict[str, Any]] = []
        self.calls: list[dict[str, Any]] = []
        self.narrative = "삼성전자는 포트폴리오의 {{weight_005930}}를 차지합니다."
        self.placeholders = ["weight_005930"]

    async def converse(self, **kwargs: Any) -> ToolTurn:
        self.turns.append(kwargs)
        if len(kwargs["messages"]) == 1:
            self.step = 0  # 새 대화의 첫 턴이다. 같은 계획을 다시 재생한다.
        if self.step >= len(self.plan):
            return ToolTurn(stop_reason="end_turn", content=[{"type": "text", "text": "끝"}])
        uses = self.plan[self.step]
        self.step += 1
        return ToolTurn(
            stop_reason="tool_use",
            content=[
                {"type": "tool_use", "id": u.id, "name": u.name, "input": u.input}
                for u in uses
            ],
            tool_uses=uses,
        )

    async def generate(self, **kwargs: Any) -> LlmResult:
        self.calls.append(kwargs)
        return LlmResult(
            payload={
                "narrative": self.narrative,
                "used_placeholders": self.placeholders,
                "used_citations": [],
            }
        )


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


def build(fake: FakeClient, monkeypatch, session: Any | None = None) -> TestClient:
    monkeypatch.setattr("app.api.routes.chat.get_llm_client", lambda: fake)
    app = create_app()
    db = session or FeedbackSession()
    app.dependency_overrides[get_session] = lambda: db
    client = TestClient(app)
    client.db = db
    return client


@pytest.fixture
def portfolio_client(monkeypatch):
    fake = FakeClient((_use("get_portfolio"),))
    with build(fake, monkeypatch) as client:
        client.llm = fake
        yield client


def _post(client: TestClient, body: dict, *, user: str = HOLDER):
    return client.post(URL, json=body, headers={"Authorization": f"Bearer {user}"})


# ── 기본 동작 ────────────────────────────────────────────────────────────────
def test_도구_결과의_수치가_치환되어_나온다(portfolio_client):
    body = _post(portfolio_client, {"message": "내 비중 얼마야?"}).json()
    answer = body["content"]["answer"]

    assert "{{" not in answer["text"]
    metrics = [s for s in answer["segments"] if s["type"] == "metric"]
    assert metrics[0]["source"] == "portfolio_engine"
    # 조각을 이어 붙이면 본문과 정확히 같다(§2.2 불변식).
    assert "".join(s["value"] for s in answer["segments"]) == answer["text"]


def test_부른_도구를_응답에_남긴다(portfolio_client):
    body = _post(portfolio_client, {"message": "내 비중 얼마야?"}).json()
    assert body["content"]["tools_used"] == ["get_portfolio"]
    assert body["data_as_of"]["portfolio"] is not None


def test_응답을_저장해_피드백을_받는다(portfolio_client):
    response = _post(portfolio_client, {"message": "내 비중 얼마야?"})
    request_id = response.json()["request_id"]

    row = next(row for row in portfolio_client.db.added if isinstance(row, AIResponse))
    assert row.request_id == request_id
    assert row.endpoint == "chat"
    feedback = portfolio_client.post(
        "/api/ai/v1/feedback",
        headers={"Authorization": f"Bearer {HOLDER}"},
        json={"request_id": request_id, "rating": "up", "reasons": []},
    )
    assert feedback.status_code == 200


def test_대화_id는_주면_따르고_없으면_만든다(portfolio_client):
    given = _post(portfolio_client, {"message": "안녕", "conversation_id": "conv_x"}).json()
    assert given["content"]["conversation_id"] == "conv_x"

    made = _post(portfolio_client, {"message": "안녕"}).json()
    assert made["content"]["conversation_id"].startswith("conv_")


def test_모델은_계산하지_않는다(portfolio_client):
    """수치 자리표시자 목록이 프롬프트에 실려야 모델이 쓸 수 있다."""
    _post(portfolio_client, {"message": "내 비중 얼마야?"})
    user_turn = portfolio_client.llm.calls[0]["user"]
    assert "{{weight_005930}}" in user_turn
    assert "수치를 직접 쓰지 마십시오" in user_turn


# ── 화면 문맥 ────────────────────────────────────────────────────────────────
def test_화면_종목이_대명사를_푼다(monkeypatch):
    fake = FakeClient((_use("get_price_history"),))  # 종목 인자 없이 부른다
    fake.narrative = "최근 구간 등락률은 {{change_005930}}입니다."
    fake.placeholders = ["change_005930"]
    with build(fake, monkeypatch) as client:
        body = _post(
            client,
            {
                "message": "이거 어때?",
                "context": {"screen": "stock_detail", "ticker": "005930"},
            },
        ).json()

    # 첫 수집 턴이 화면 정보를 보고 있어야 모델이 대명사를 풀 수 있다.
    opening = fake.turns[0]["messages"][0]["content"]
    assert "005930" in opening
    assert "이거" in opening
    # 인자 없이 부른 시세 도구가 화면 종목으로 해결됐다.
    assert body["content"]["tools_used"] == ["get_price_history"]


def test_종목코드가_6자리가_아니면_거부한다(portfolio_client):
    response = _post(
        portfolio_client,
        {"message": "이거 어때?", "context": {"screen": "stock_detail", "ticker": "AAPL"}},
    )
    assert response.status_code == 400
    assert response.json()["error"]["detail"]["ticker"] == "AAPL"


# ── 근거 ─────────────────────────────────────────────────────────────────────
def test_검색_결과가_근거로_실린다(monkeypatch):
    async def _hits(*_: Any, **__: Any):
        return [
            {
                "text": "3분기 HBM 매출 비중이 확대되었다",
                "ticker": "005930",
                "title": "분기보고서",
                "published_at": datetime(2026, 8, 14, 16, 12),
                "similarity": 0.87,
            }
        ]

    monkeypatch.setattr("app.llm.tools.search", _hits)
    fake = FakeClient((_use("search_filings", query="HBM"),))
    fake.narrative = "관련 공시가 확인됩니다[^cit_1]."
    fake.placeholders = []

    with build(fake, monkeypatch) as client:
        body = _post(client, {"message": "HBM 관련 공시 있어?"}).json()

    assert body["citations"][0]["id"] == "cit_1"
    assert body["citations"][0]["title"] == "분기보고서"
    assert body["data_as_of"]["filings"] is not None
    # 원문이 프롬프트에 실려야 모델이 근거를 볼 수 있다.
    assert "3분기 HBM 매출 비중이 확대되었다" in fake.calls[0]["user"]


def test_근거가_없으면_한계를_밝히고_계속한다(monkeypatch):
    async def _none(*_: Any, **__: Any):
        return []

    monkeypatch.setattr("app.llm.tools.search", _none)
    fake = FakeClient((_use("search_news", query="반도체"),))
    fake.narrative = "관련 자료를 확인하지 못했습니다."
    fake.placeholders = []

    with build(fake, monkeypatch) as client:
        body = _post(client, {"message": "무슨 일 있었어?"}).json()

    assert body["citations"] == []
    assert body["content"]["answer"]["text"]
    assert "관련 자료를 찾지 못했다" in fake.calls[0]["user"]


# ── 루프 상한 ────────────────────────────────────────────────────────────────
def test_도구_턴은_상한에서_멈춘다(monkeypatch):
    """되먹임 루프는 문맥 창과 사용자의 돈을 같은 속도로 태운다."""
    fake = FakeClient(*[(_use("get_portfolio"),)] * (MAX_TOOL_TURNS + 5))
    with build(fake, monkeypatch) as client:
        assert _post(client, {"message": "내 비중"}).status_code == 200

    assert len(fake.turns) == MAX_TOOL_TURNS
    assert len(fake.calls) >= 1  # 상한에 걸려도 답변은 만든다


def test_한_턴에_부른_도구는_한_번에_돌려준다(monkeypatch):
    fake = FakeClient((_use("get_portfolio"), _use("get_price_history", ticker="005930")))
    with build(fake, monkeypatch) as client:
        body = _post(client, {"message": "내 비중이랑 요즘 주가"}).json()

    results = fake.turns[1]["messages"][-1]
    assert results["role"] == "user"
    # 나눠 보내면 모델이 병렬 호출을 그만둔다.
    assert len(results["content"]) == 2
    assert set(body["content"]["tools_used"]) == {"get_portfolio", "get_price_history"}


def test_도구_호출_총량도_막는다(monkeypatch):
    over = tuple(_use("get_portfolio") for _ in range(MAX_TOOL_CALLS + 1))
    fake = FakeClient(*[over] * MAX_TOOL_TURNS)
    with build(fake, monkeypatch) as client:
        assert _post(client, {"message": "내 비중"}).status_code == 200

    executed = [b for turn in fake.turns[1:] for b in turn["messages"][-1]["content"]]
    refused = [b for b in executed if b.get("is_error")]
    assert refused, "상한을 넘긴 호출은 거부되어야 한다"


# ── 실패 경로 ────────────────────────────────────────────────────────────────
def test_질문이_비면_거부한다(portfolio_client):
    response = _post(portfolio_client, {"message": "   "})
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "INVALID_REQUEST"


def test_너무_긴_질문은_거부한다(portfolio_client):
    response = _post(portfolio_client, {"message": "가" * 2_001})
    assert response.status_code == 400


def test_토큰이_없으면_거부한다(portfolio_client):
    assert portfolio_client.post(URL, json={"message": "안녕"}).status_code == 401


def test_키가_없으면_지어내지_않고_실패한다(monkeypatch):
    monkeypatch.setattr("app.api.routes.chat.get_llm_client", NullLlmClient)
    app = create_app()
    app.dependency_overrides[get_session] = lambda: None
    with TestClient(app) as client:
        response = _post(client, {"message": "내 비중 얼마야?"})

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "INSUFFICIENT_DATA"
    assert "LLM 키" in response.json()["error"]["message"]


def test_검사에_걸린_답변은_내보내지_않는다(monkeypatch):
    """§5.2 — 수치를 직접 적은 본문은 폐기된다. 빈 답으로 채우지 않는다."""
    fake = FakeClient((_use("get_portfolio"),))
    fake.narrative = "삼성전자는 포트폴리오의 72.3%를 차지합니다."
    fake.placeholders = []

    with build(fake, monkeypatch) as client:
        response = _post(client, {"message": "내 비중 얼마야?"})

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "GUARDRAIL_BLOCKED"


# ── 클라이언트 계약 ──────────────────────────────────────────────────────────
def test_응답_블록을_그대로_되돌려_보낼_수_있다():
    """thinking 서명이 한 글자만 바뀌어도 다음 요청이 거부된다.

    가짜 클라이언트로는 이 경로가 검증되지 않으므로 실제 SDK 타입으로 확인한다.
    """
    from anthropic.types import Message, TextBlock, ToolUseBlock, Usage
    from anthropic.types.thinking_block import ThinkingBlock

    from app.llm.client import _to_turn

    turn = _to_turn(
        Message(
            id="msg_1",
            model="claude-opus-5",
            role="assistant",
            type="message",
            stop_reason="tool_use",
            content=[
                ThinkingBlock(type="thinking", thinking="비중을 봐야 한다", signature="sig_abc"),
                TextBlock(type="text", text="포트폴리오를 확인하겠습니다."),
                ToolUseBlock(type="tool_use", id="tu_1", name="get_portfolio", input={}),
            ],
            usage=Usage(input_tokens=120, output_tokens=40),
        )
    )

    assert turn.stop_reason == "tool_use"
    assert turn.content[0]["signature"] == "sig_abc"
    assert turn.tool_uses == (ToolUse(id="tu_1", name="get_portfolio", input={}),)
    assert turn.input_tokens == 120
