"""피드백 엔드포인트 — API 명세 §10."""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.api.deps import get_session
from app.api.main import create_app
from app.core.models import AIFeedback

HOLDER = "u1"


class _Session:
    """`ai_responses` 소유자 조회와 기존 평가 조회에 답하는 최소 세션."""

    def __init__(self, *, owner: str | None = HOLDER, existing: AIFeedback | None = None):
        self.owner = owner
        self.existing = existing
        self.added: list[Any] = []
        self.commits = 0

    async def scalar(self, statement: Any) -> Any:
        sql = str(statement)
        if "ai_responses" in sql:
            return self.owner
        return self.existing

    def add(self, obj: Any) -> None:
        self.added.append(obj)

    async def commit(self) -> None:
        self.commits += 1

    async def rollback(self) -> None:
        return None


def _client(session: _Session) -> TestClient:
    app = create_app()
    app.dependency_overrides[get_session] = lambda: session
    return TestClient(app)


AUTH = {"Authorization": f"Bearer {HOLDER}"}


def _body(**kw: Any) -> dict:
    return {"request_id": "req_1", "rating": "up", "reasons": [], **kw}


def test_평가를_기록한다() -> None:
    s = _Session()
    r = _client(s).post("/api/ai/v1/feedback", headers=AUTH, json=_body())
    assert r.status_code == 200
    assert r.json()["content"] == {"recorded": True}
    (row,) = s.added
    assert row.request_id == "req_1"
    assert s.commits == 1


def test_없는_응답에는_평가할_수_없다() -> None:
    s = _Session(owner=None)
    r = _client(s).post("/api/ai/v1/feedback", headers=AUTH, json=_body())
    assert r.status_code == 400
    assert not s.added


def test_남의_응답에는_평가할_수_없다() -> None:
    """존재하지 않는 경우와 같은 응답을 준다 — 구분해 주면 남의 id를 캐낼 수 있다."""
    s = _Session(owner="다른사람")
    r = _client(s).post("/api/ai/v1/feedback", headers=AUTH, json=_body())
    assert r.status_code == 400
    assert not s.added


def test_같은_응답에_다시_평가하면_덮어쓴다() -> None:
    """별점을 고쳐 다는 것은 정상이다. 행을 쌓으면 집계가 평가 횟수를 센다."""
    old = AIFeedback(request_id="req_1", user_id=HOLDER)
    s = _Session(existing=old)
    r = _client(s).post("/api/ai/v1/feedback", headers=AUTH, json=_body(rating="down", reasons=["wrong_number"]))
    assert r.status_code == 200
    assert not s.added, "새 행을 만들지 않아야 한다"
    assert old.rating == "down"
    assert old.reasons == ["wrong_number"]


@pytest.mark.parametrize(
    "bad",
    [
        {"rating": "아무거나"},
        {"reasons": ["없는사유"]},
        {"comment": "가" * 1001},
    ],
)
def test_잘못된_입력은_400이다(bad: dict) -> None:
    """이 저장소는 FastAPI 검증 오류도 InvalidRequest와 같은 400으로 통일한다."""
    r = _client(_Session()).post("/api/ai/v1/feedback", headers=AUTH, json=_body(**bad))
    assert r.status_code == 400
