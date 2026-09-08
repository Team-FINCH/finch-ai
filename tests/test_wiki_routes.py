"""위키 조회 응답의 원장 종목명 계약."""

from __future__ import annotations

import uuid
from datetime import datetime

from fastapi.testclient import TestClient

from app.api.deps import get_session
from app.api.main import create_app
from app.core.adapters import Instrument, Ledger
from app.core.enums import ThesisStatus, WikiSource
from app.core.models import WikiThesis


class _LedgerSource:
    async def load(self, user_id: str) -> Ledger:
        return Ledger(
            user_id=user_id,
            trading_days=(),
            instruments={"005930": Instrument("005930", "삼성전자", "반도체")},
            prices={},
        )


def _thesis(ticker: str) -> WikiThesis:
    return WikiThesis(
        id=uuid.uuid4(),
        user_id="u1",
        ticker=ticker,
        text="장기 보유",
        source=WikiSource.USER_STATED,
        status=ThesisStatus.ACTIVE,
        recorded_at=datetime.now(),
    )


def test_위키_논지에_원장_종목명을_싣고_미보유는_ticker로_대체한다(monkeypatch) -> None:
    theses = [_thesis("005930"), _thesis("999999")]

    async def _list_theses(_db, _user_id):
        return theses

    async def _list_facts(_db, _user_id):
        return []

    monkeypatch.setattr("app.api.routes.wiki.ledger_source", lambda: _LedgerSource())
    monkeypatch.setattr("app.api.routes.wiki.list_theses", _list_theses)
    monkeypatch.setattr("app.api.routes.wiki.list_facts", _list_facts)
    app = create_app()
    app.dependency_overrides[get_session] = lambda: object()

    with TestClient(app) as client:
        response = client.get("/api/ai/v1/wiki", headers={"X-User-Id": "u1"})

    assert response.status_code == 200
    payloads = response.json()["content"]["theses"]
    assert [(payload["ticker"], payload["name"]) for payload in payloads] == [
        ("005930", "삼성전자"),
        ("999999", "999999"),
    ]
