"""Confirm HTTP contract against PostgreSQL, with request commits isolated by savepoints."""

from __future__ import annotations

import uuid

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import event, select
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.pool import NullPool

from app.api.deps import get_session
from app.api.main import create_app
from app.core.config import settings
from app.core.enums import Confidence, DeleteReason, WikiSource
from app.core.models import WikiFact
from app.core.schemas import now_kst
from app.wiki.store import add_fact, fact_payload


@pytest.fixture
async def context(monkeypatch):
    monkeypatch.setattr(settings, "app_env", "local")
    monkeypatch.setattr(settings, "backend_service_token", "")
    monkeypatch.setattr("app.api.routes.wiki.ledger_source", lambda: None)
    engine = create_async_engine(settings.database_url, poolclass=NullPool)
    try:
        async with engine.connect() as connection:
            transaction = await connection.begin()
            async with AsyncSession(
                bind=connection, expire_on_commit=False, join_transaction_mode="create_savepoint"
            ) as db:
                app = create_app()

                async def session_override():
                    yield db

                app.dependency_overrides[get_session] = session_override
                user_id = f"test-confirm-{uuid.uuid4().hex[:12]}"
                async with AsyncClient(
                    transport=ASGITransport(app=app),
                    base_url="http://test",
                    headers={"X-User-Id": user_id},
                ) as client:
                    yield db, client, user_id
            await transaction.rollback()
    finally:
        await engine.dispose()


async def _guess(db, user_id, **kwargs):
    return await add_fact(
        db,
        user_id,
        "반도체를 새로 담지 않는 편인가요?",
        source=kwargs.pop("source", WikiSource.AI_INFERRED),
        confidence=Confidence.LOW,
        evidence={"type": "conversation", "ref": "conv_confirm"},
        **kwargs,
    )


def _path(fact_id):
    return f"/api/ai/v1/wiki/facts/{fact_id}/confirm"


async def test_promotion_preserves_every_other_column_and_get_reflects_it(context):
    db, client, user_id = context
    fact = await _guess(db, user_id)
    before = {column.name: getattr(fact, column.name) for column in WikiFact.__table__.columns}
    response = await client.post(_path(fact.id))
    assert response.status_code == 200
    await db.refresh(fact)
    after = {column.name: getattr(fact, column.name) for column in WikiFact.__table__.columns}
    assert after == {**before, "source": WikiSource.USER_STATED}
    body = response.json()
    expected = fact_payload(fact)
    expected["as_of"] = fact.as_of.isoformat().replace("+00:00", "Z")
    assert body["content"] == expected
    assert body["request_id"].startswith("req_")
    assert body["cached"] is False
    assert body["citations"] == []
    assert body["freshness_warnings"] == []
    profile = (await client.get("/api/ai/v1/wiki")).json()["content"]["profile"]
    assert profile == [expected]
    rows = (await db.scalars(select(WikiFact).where(WikiFact.user_id == user_id))).all()
    assert len(rows) == 1


async def test_double_tap_succeeds_with_one_update_and_identical_fact(context):
    db, client, user_id = context
    fact = await _guess(db, user_id)
    updates = []

    def record_update(conn, cursor, statement, parameters, context, executemany):
        if statement.lstrip().upper().startswith("UPDATE WIKI_FACTS "):
            updates.append(statement)

    connection = await db.connection()
    event.listen(connection.sync_connection, "before_cursor_execute", record_update)
    try:
        fresh = await client.post(_path(fact.id))
        repeat = await client.post(_path(fact.id))
    finally:
        event.remove(connection.sync_connection, "before_cursor_execute", record_update)

    assert fresh.status_code == repeat.status_code == 200
    fresh_body, repeat_body = fresh.json(), repeat.json()
    for body in (fresh_body, repeat_body):
        assert body["request_id"].startswith("req_")
        assert body["generated_at"]
    # Request metadata is new on each call; the fact and other envelope fields are identical.
    assert {k: v for k, v in fresh_body.items() if k not in ("request_id", "generated_at")} == {
        k: v for k, v in repeat_body.items() if k not in ("request_id", "generated_at")
    }
    assert fresh_body["content"]["source"] == "user_stated"
    assert len(updates) == 1
    await db.refresh(fact)
    assert fact.source == WikiSource.USER_STATED
    rows = (await db.scalars(select(WikiFact).where(WikiFact.user_id == user_id))).all()
    assert [row.id for row in rows] == [fact.id]


async def test_already_user_stated_fact_succeeds_unchanged(context):
    db, client, user_id = context
    fact = await _guess(db, user_id, source=WikiSource.USER_STATED)
    before = {column.name: getattr(fact, column.name) for column in WikiFact.__table__.columns}
    response = await client.post(_path(fact.id))
    assert response.status_code == 200
    assert response.json()["content"]["source"] == "user_stated"
    await db.refresh(fact)
    assert {
        column.name: getattr(fact, column.name) for column in WikiFact.__table__.columns
    } == before


@pytest.mark.parametrize("source", [WikiSource.AI_INFERRED, WikiSource.USER_STATED])
async def test_soft_deleted_fact_is_never_promoted_or_restored(context, source):
    db, client, user_id = context
    fact = await _guess(db, user_id, source=source)
    fact.deleted_at = now_kst()
    fact.deleted_reason = DeleteReason.GUESS_REJECTED
    await db.flush()
    before = {column.name: getattr(fact, column.name) for column in WikiFact.__table__.columns}
    response = await client.post(_path(fact.id))
    assert response.status_code == 400
    assert response.json()["code"] == "INVALID_REQUEST"
    assert response.json()["message"] == "해당 항목을 찾을 수 없습니다."
    await db.refresh(fact)
    assert {
        column.name: getattr(fact, column.name) for column in WikiFact.__table__.columns
    } == before
    assert (await client.get("/api/ai/v1/wiki")).json()["content"]["profile"] == []


async def test_foreign_guess_is_not_modified(context):
    db, client, user_id = context
    fact = await _guess(db, f"{user_id}-other")
    response = await client.post(_path(fact.id))
    assert response.status_code == 400
    assert response.json()["code"] == "INVALID_REQUEST"
    assert response.json()["message"] == "해당 항목을 찾을 수 없습니다."
    await db.refresh(fact)
    assert fact.source == WikiSource.AI_INFERRED
    assert fact.user_id == f"{user_id}-other"


@pytest.mark.parametrize("fact_id", [str(uuid.uuid4()), "invalid-uuid"])
async def test_missing_or_malformed_id_is_rejected(context, fact_id):
    _, client, _ = context
    response = await client.post(_path(fact_id))
    assert response.status_code == 400
    assert response.json()["code"] == "INVALID_REQUEST"
    assert response.json()["message"] == "해당 항목을 찾을 수 없습니다."


@pytest.mark.parametrize(
    ("source", "editable"),
    [
        (WikiSource.AI_INFERRED, False),
        (WikiSource.USER_STATED, False),
        (WikiSource.DERIVED_FROM_TRADES, True),
    ],
)
async def test_noneditable_guess_or_trade_derived_fact_is_not_promoted(context, source, editable):
    db, client, user_id = context
    fact = await _guess(db, user_id, source=source, editable=editable)
    response = await client.post(_path(fact.id))
    assert response.status_code == 400
    assert response.json()["code"] == "INVALID_REQUEST"
    await db.refresh(fact)
    assert fact.source == source
    assert fact.editable == editable
    assert response.json()["message"] == (
        "사용자가 확인할 수 없는 항목입니다."
        if not editable
        else "AI 추측 항목만 확인할 수 있습니다."
    )


async def test_missing_auth_does_not_promote(context):
    db, client, user_id = context
    fact = await _guess(db, user_id)
    client.headers.clear()
    response = await client.post(_path(fact.id))
    assert response.status_code == 401
    assert response.json()["code"] == "UNAUTHORIZED"
    await db.refresh(fact)
    assert fact.source == WikiSource.AI_INFERRED
