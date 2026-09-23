"""채팅 비동기 작업 (#90) — 라우트 계약과 실행기."""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.api.main import create_app
from app.chat_jobs import RETRYABLE, is_expired, run_job
from app.core.db import get_session
from app.core.errors import ErrorCode, LLMTimeout
from app.core.models import ChatJob
from app.core.schemas import now_kst

AUTH = {"X-User-Id": "u1"}
URL = "/api/ai/v1/chat/jobs"


class JobSession:
    """chat_jobs 한 테이블만 흉내 낸다. 가장 최근 add 된 행을 조회에 돌려준다."""

    def __init__(self) -> None:
        self.rows: list[ChatJob] = []

    def add(self, obj: Any) -> None:
        self.rows.append(obj)

    async def commit(self) -> None:
        return None

    async def rollback(self) -> None:
        return None

    async def scalar(self, statement: Any) -> Any:
        params = statement.compile().params  # WHERE 절의 바인드 값만 본다
        for row in reversed(self.rows):
            if "idempotency_key_1" in params:
                if row.idempotency_key == params["idempotency_key_1"]:
                    return row
            elif row.id == params.get("id_1"):
                return row if row.user_id == params.get("user_id_1") else None
        return None


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(
        "app.api.routes.chat.get_llm_client", lambda: object()
    )  # NullLlmClient 만 아니면 된다
    app = create_app()
    db = JobSession()
    app.dependency_overrides[get_session] = lambda: db
    c = TestClient(app)
    c.db = db
    return c


def test_생성은_202_와_봉투를_준다(client):
    res = client.post(URL, json={"message": "삼성전자 어때?"}, headers=AUTH)
    assert res.status_code == 202
    body = res.json()
    assert body["content"]["status"] == "queued"
    assert body["content"]["job_id"].startswith("job_")
    assert body["content"]["conversation_id"].startswith("conv_")
    assert "request_id" in body and "disclaimer" in body  # §2 봉투


def test_같은_멱등_키면_같은_job_id_를_202_로_준다(client):
    headers = {**AUTH, "X-Idempotency-Key": "k1"}
    first = client.post(URL, json={"message": "질문"}, headers=headers).json()
    second = client.post(URL, json={"message": "질문"}, headers=headers)
    assert second.status_code == 202
    assert second.json()["content"]["job_id"] == first["content"]["job_id"]


def test_조회는_본인_것만_보이고_없으면_404(client):
    job_id = client.post(URL, json={"message": "질문"}, headers=AUTH).json()["content"]["job_id"]
    assert client.get(f"{URL}/{job_id}", headers=AUTH).status_code == 200
    other = client.get(f"{URL}/{job_id}", headers={"X-User-Id": "u2"})
    assert other.status_code == 404
    assert other.json()["code"] == "RESOURCE_NOT_FOUND"
    assert client.get(f"{URL}/job_none", headers=AUTH).status_code == 404


def test_완료된_작업은_result_와_근거를_실어_준다(client):
    job_id = client.post(URL, json={"message": "질문"}, headers=AUTH).json()["content"]["job_id"]
    job = client.db.rows[-1]
    job.status = "completed"
    job.result = {
        "content": {
            "conversation_id": job.conversation_id,
            "answer": {"title": None, "text": "답", "segments": []},
            "tools_used": [],
        },
        "citations": [],
        "data_as_of": {},
    }
    body = client.get(f"{URL}/{job_id}", headers=AUTH).json()
    assert body["content"]["status"] == "completed"
    assert body["content"]["result"]["answer"]["text"] == "답"
    assert body["content"]["error"] is None


def test_완료된_작업의_request_id_는_저장된_값이고_조회마다_같다(client):
    """피드백(POST /ai/feedback)이 이 값으로 원본 응답을 찾는다 (이슈 #102).

    Envelope 의 기본값은 조회할 때마다 새로 발급되므로, 저장분에서 옮겨 오지 않으면
    두 번 조회한 값이 서로 다르고 응답 로그의 값과도 어긋난다.
    """
    job_id = client.post(URL, json={"message": "질문"}, headers=AUTH).json()["content"]["job_id"]
    job = client.db.rows[-1]
    job.status = "completed"
    job.result = {
        "request_id": "req_원본",
        "content": {
            "conversation_id": job.conversation_id,
            "answer": {"title": None, "text": "답", "segments": []},
            "tools_used": [],
        },
        "citations": [],
        "data_as_of": {},
    }
    first = client.get(f"{URL}/{job_id}", headers=AUTH).json()["request_id"]
    second = client.get(f"{URL}/{job_id}", headers=AUTH).json()["request_id"]
    assert first == "req_원본"
    assert first == second


def test_실패한_작업은_200_본문의_error_로_온다(client):
    job_id = client.post(URL, json={"message": "질문"}, headers=AUTH).json()["content"]["job_id"]
    job = client.db.rows[-1]
    job.status = "failed"
    job.error = {"code": "LLM_TIMEOUT", "message": "지연", "retryable": True}
    res = client.get(f"{URL}/{job_id}", headers=AUTH)
    assert res.status_code == 200
    assert res.json()["content"]["error"]["retryable"] is True
    assert res.json()["content"]["result"] is None


def test_24시간_지난_작업은_만료다():
    job = ChatJob(id="j", user_id="u", conversation_id="c", question="q", context={})
    job.created_at = now_kst() - timedelta(hours=25)
    assert is_expired(job)
    job.created_at = now_kst() - timedelta(hours=1)
    assert not is_expired(job)


@pytest.mark.anyio
async def test_실행기는_예외를_error_로_옮기고_retryable_을_표시한다(monkeypatch):
    from app import chat_jobs

    finished: list[dict[str, Any]] = []

    async def fake_answer(body, user_id, db):
        raise LLMTimeout("지연")

    async def fake_finish(job_id, **values):
        finished.append(values)

    class Guard:
        async def enter_system(self, *a, **k):
            return object()

    class Session:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

    monkeypatch.setattr("app.api.routes.chat.answer_question", fake_answer)
    monkeypatch.setattr(chat_jobs, "_finish", fake_finish)
    monkeypatch.setattr(chat_jobs, "SessionFactory", lambda: Session())
    monkeypatch.setattr(chat_jobs, "reset_usage", lambda token: None)

    job = ChatJob(id="j", user_id="u", conversation_id="c", question="q", context={})
    status, result, error = await run_job(job, Guard())

    assert status == "failed" and result is None
    assert error["code"] == "LLM_TIMEOUT" and error["retryable"] is True
    assert finished[-1]["status"] == "failed"
    assert ErrorCode.GUARDRAIL_BLOCKED not in RETRYABLE
