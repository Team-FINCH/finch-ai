"""NAVER API HUB 뉴스 수집기 테스트."""

from __future__ import annotations

import asyncio
import uuid
from copy import deepcopy
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import httpx
import pytest

from ingest import news as news_mod
from ingest.news import (
    NEWS_URL,
    NaverNewsError,
    canonical_url,
    clean_text,
    external_id,
    fetch_news,
    parse_item,
    save,
)


def _item(**overrides) -> dict:
    return {
        "title": "<b>삼성전자</b>, 신제품 공개 &amp; 공급 확대",
        "originallink": "HTTPS://News.Example.COM/article/1#section",
        "link": "https://n.news.naver.com/article/1",
        "description": "삼성전자가 <b>HBM</b> 공급을 확대한다.",
        "pubDate": "Fri, 28 Aug 2026 10:30:00 +0900",
        **overrides,
    }


def _client(handler) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))


def test_강조_태그와_html_entity를_제거한다() -> None:
    assert clean_text("<b>삼성</b> &amp; 전자") == "삼성 & 전자"


def test_원문_url을_정규화하고_안정적인_id를_만든다() -> None:
    url = canonical_url("HTTPS://News.Example.COM/a?q=1#fragment")
    assert url == "https://news.example.com/a?q=1"
    assert external_id(url) == external_id(url)
    assert len(external_id(url)) == 64


def test_검색_결과를_내부_기사로_바꾼다() -> None:
    article = parse_item(_item(), "005930")
    assert article is not None
    assert article.title == "삼성전자, 신제품 공개 & 공급 확대"
    assert article.summary == "삼성전자가 HBM 공급을 확대한다."
    assert article.url == "https://news.example.com/article/1"
    assert article.publisher == "news.example.com"
    assert article.published_at.isoformat() == "2026-08-28T10:30:00+09:00"


@pytest.mark.parametrize(
    "field",
    ["title", "description", "pubDate"],
)
def test_필수값이_없는_결과는_버린다(field: str) -> None:
    value = "" if field != "pubDate" else "not-a-date"
    assert parse_item(_item(**{field: value}), "005930") is None


def test_원문과_네이버_url이_모두_없으면_버린다() -> None:
    assert parse_item(_item(originallink="", link=""), "005930") is None


def test_api_hub_주소와_인증헤더_최신순을_사용한다() -> None:
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["id"] = request.headers.get("X-NCP-APIGW-API-KEY-ID")
        seen["secret"] = request.headers.get("X-NCP-APIGW-API-KEY")
        return httpx.Response(
            200,
            json={"total": 1, "items": [_item()]},
        )

    articles = fetch_news(
        _client(handler),
        "client-id",
        "client-secret",
        query="삼성전자",
        ticker="005930",
        max_docs=5,
    )
    assert len(articles) == 1
    assert seen["url"].startswith(NEWS_URL)
    assert "sort=date" in seen["url"]
    assert "format=json" in seen["url"]
    assert seen["id"] == "client-id"
    assert seen["secret"] == "client-secret"


def test_페이지를_따라가되_max_docs에서_멈춘다() -> None:
    starts: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        start = int(request.url.params["start"])
        display = int(request.url.params["display"])
        starts.append(start)
        items = [
            _item(
                title=f"기사 {index}",
                originallink=f"https://news.example.com/{start + index}",
            )
            for index in range(display)
        ]
        return httpx.Response(200, json={"total": 200, "items": items})

    articles = fetch_news(
        _client(handler), "id", "secret", query="삼성전자", ticker="005930", max_docs=105
    )
    assert len(articles) == 105
    assert starts == [1, 101]


def test_인증실패에_키나_응답본문을_노출하지_않는다() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            401,
            json={
                "error": {
                    "errorCode": "200",
                    "message": "Authentication Failed",
                    "details": "secret-value",
                }
            },
        )

    with pytest.raises(NaverNewsError) as caught:
        fetch_news(
            _client(handler),
            "client-id",
            "client-secret",
            query="삼성전자",
            ticker="005930",
            max_docs=1,
        )
    message = str(caught.value)
    assert "status=401" in message
    assert "code=200" in message
    assert "client-secret" not in message
    assert "secret-value" not in message


def test_json이_아닌_성공응답을_거부한다() -> None:
    client = _client(lambda _request: httpx.Response(200, content=b"not-json"))
    with pytest.raises(NaverNewsError, match="JSON"):
        fetch_news(client, "id", "secret", query="삼성전자", ticker="005930", max_docs=1)


class _Result:
    def __init__(self, value=None, rows=()):
        self.value = value
        self.rows = rows

    def scalar_one(self):
        return self.value

    def scalar_one_or_none(self):
        return self.value

    def one(self):
        return self.value

    def all(self):
        return list(self.rows)


class NewsSession:
    """Stateful SQL boundary double; no network or shared database access."""

    def __init__(self):
        self.documents = {}
        self.chunks = {}
        self.events = []
        self.statements = []
        self.commits = 0

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc):
        return None

    async def execute(self, statement: Any, parameters=None):
        sql = str(statement)
        self.statements.append(sql)
        values = statement.compile().params
        if sql.startswith("INSERT INTO documents"):
            key = values["external_id"]
            if (
                key in self.documents
                and "ON CONFLICT ON CONSTRAINT uq_documents_source_external DO NOTHING" in sql
            ):
                return _Result()
            document = self.documents.setdefault(key, {"id": uuid.uuid4()})
            document.update({key: value for key, value in values.items() if key != "id"})
            return _Result(document["id"])
        if sql.startswith("SELECT documents.id"):
            assert "FOR UPDATE" in sql
            document = self.documents[values["external_id_1"]]
            return _Result(
                SimpleNamespace(**{key: document[key] for key in ("id", "title", "body")})
            )
        if sql.startswith("UPDATE documents"):
            document = next(doc for doc in self.documents.values() if doc["id"] == values["id_1"])
            document.update({key: value for key, value in values.items() if key != "id_1"})
        if sql.startswith("DELETE FROM document_chunks"):
            self.chunks.pop(values["document_id_1"], None)
        if sql.startswith("INSERT INTO document_chunks"):
            for index in range(sum(key.startswith("document_id_m") for key in values)):
                self.chunks.setdefault(values[f"document_id_m{index}"], []).append(
                    {
                        "id": uuid.uuid4(),
                        "chunk_index": values[f"chunk_index_m{index}"],
                        "text": values[f"text_m{index}"],
                        "embedding": None,
                    }
                )
        if sql.startswith("INSERT INTO events"):
            self.events.extend(parameters)
        if "FROM events" in sql:
            assert "LEFT OUTER JOIN documents" in sql
            return _Result(
                rows=[
                    (event["title"], event["document_id"], event.get("doc_type", "news"))
                    for event in self.events
                    if event["ticker"] == values["ticker_1"]
                    and (
                        event["event_date"] == values["event_date_1"]
                        or event["document_id"] == values["document_id_1"]
                    )
                ]
            )
        return _Result()

    async def commit(self):
        self.commits += 1


@pytest.fixture
def news_session(monkeypatch):
    session = NewsSession()
    monkeypatch.setattr(news_mod, "SessionFactory", lambda: session)
    return session


def article_for(index=0, **overrides):
    article = parse_item(
        _item(title=f"기사 {index}", originallink=f"https://news.example.com/{index}"), "005930"
    )
    assert article is not None
    return replace(article, **overrides)


def test_document_chunks_and_linked_event_commit_together(news_session):
    article = article_for(title="삼성전자 신제품 출시")
    result = asyncio.run(save(article))
    assert result == news_mod.SaveResult(1, "created")
    assert news_session.commits == 1
    assert len(news_session.statements) == 5
    assert "pg_advisory_xact_lock" in news_session.statements[0]
    document = news_session.documents[article.external_id]
    assert news_session.events == [
        {
            "ticker": "005930",
            "event_type": "product",
            "event_date": article.published_at.date(),
            "title": article.title,
            "importance": 0.5,
            "document_id": document["id"],
        }
    ]
    assert document["doc_type"] == "news"
    assert len(news_session.chunks[document["id"]]) == 1
    assert news_session.chunks[document["id"]][0]["text"] == f"{article.title}\n\n{article.summary}"
    assert news_session.chunks[document["id"]][0]["embedding"] is None


@pytest.mark.parametrize("hour", [0, 1, 8, 9])
def test_event_day_is_kst_even_when_input_is_utc(news_session, hour):
    published = datetime(2026, 8, 28, hour, 15, tzinfo=news_mod.KST)
    asyncio.run(save(article_for(published_at=published.astimezone(UTC))))
    assert news_session.events[0]["event_date"] == published.date()


def test_rerun_preserves_chunks_and_embeddings_without_duplicate_event(news_session):
    article = article_for()
    assert asyncio.run(save(article)).promotion == "created"
    document_id = news_session.documents[article.external_id]["id"]
    news_session.chunks[document_id][0]["embedding"] = [0.25, -0.5, 0.75]
    original_chunks = deepcopy(news_session.chunks)
    assert asyncio.run(save(article)) == news_mod.SaveResult(0, "duplicate")
    assert news_session.chunks == original_chunks
    assert len(news_session.events) == 1


@pytest.mark.parametrize("field,value", [("title", "수정 제목"), ("summary", "수정 요약")])
def test_changed_content_replaces_chunks_without_duplicate_event(news_session, field, value):
    article = article_for()
    asyncio.run(save(article))
    document_id = news_session.documents[article.external_id]["id"]
    news_session.chunks[document_id][0]["embedding"] = [0.25, -0.5, 0.75]
    original_chunk_id = news_session.chunks[document_id][0]["id"]
    revised = replace(article, **{field: value})

    assert asyncio.run(save(revised)) == news_mod.SaveResult(1, "duplicate")
    document = news_session.documents[article.external_id]
    assert document["id"] == document_id
    assert (document["title"], document["body"]) == (revised.title, revised.summary)
    pieces = news_session.chunks[document_id]
    assert len(pieces) == 1
    assert pieces[0]["id"] != original_chunk_id
    assert pieces[0]["text"] == f"{revised.title}\n\n{revised.summary}"
    assert pieces[0]["embedding"] is None  # Revised text needs a fresh embedding.
    assert len(news_session.events) == 1

    pieces[0]["embedding"] = [0.75, -0.5, 0.25]
    revised_chunks = deepcopy(pieces)
    assert asyncio.run(save(revised)) == news_mod.SaveResult(0, "duplicate")
    assert news_session.chunks[document_id] == revised_chunks


def test_metadata_changes_preserve_chunks_and_embeddings(news_session):
    article = article_for()
    asyncio.run(save(article))
    document = news_session.documents[article.external_id]
    news_session.chunks[document["id"]][0]["embedding"] = [0.25, -0.5, 0.75]
    original_chunks = deepcopy(news_session.chunks)
    revised = replace(
        article,
        publisher="updated publisher",
        published_at=article.published_at + timedelta(days=1),
    )

    assert asyncio.run(save(revised)) == news_mod.SaveResult(0, "duplicate")
    assert document["publisher"] == revised.publisher
    assert document["published_at"] == revised.published_at
    assert news_session.chunks == original_chunks
    assert len(news_session.events) == 1


def test_same_ticker_day_title_at_another_url_is_duplicate(news_session):
    first = article_for(1)
    asyncio.run(save(first))
    assert asyncio.run(save(article_for(2, title=first.title))).promotion == "duplicate"
    assert len(news_session.documents) == 2
    assert len(news_session.events) == 1


def test_cap_counts_existing_news_but_not_filings_and_is_per_ticker_day(news_session):
    article = article_for()
    news_session.events.append(
        {
            "ticker": article.ticker,
            "event_date": article.published_at.date(),
            "title": "공시",
            "document_id": uuid.uuid4(),
            "doc_type": "filing",
        }
    )
    for i in range(3):
        assert asyncio.run(save(article_for(i))).promotion == "created"
    assert asyncio.run(save(article_for(3))).promotion == "capped"
    assert asyncio.run(save(article_for(4, ticker="000660"))).promotion == "created"
    assert (
        asyncio.run(
            save(article_for(5, published_at=article.published_at + timedelta(days=1)))
        ).promotion
        == "created"
    )
    assert len(news_session.documents) == 6  # Capped news remains available for RAG.


@pytest.mark.parametrize(
    "title,kind,importance",
    [
        ("영업이익 증가", "earnings", 0.6),
        ("배당 발표", "dividend", 0.5),
        ("신제품 출시", "product", 0.5),
        ("기준금리 인하", "macro", 0.4),
        ("일반 기사", "filing", 0.3),
    ],
)
def test_classification_stays_below_earnings_filings(title, kind, importance):
    assert news_mod.classify(title) == (kind, importance)
    assert importance < 0.7


def test_400_articles_ranked_capped_and_rerun_accounted_for(news_session, monkeypatch, caplog):
    now = datetime.now(news_mod.KST)
    articles = [article_for(i, published_at=now) for i in range(400)]
    articles[-1] = replace(articles[-1], title="실적 발표")
    articles[-2] = replace(articles[-2], title="배당 발표")
    articles[-3] = replace(articles[-3], title="신제품 출시")
    # An already-ingested document must still acquire its missing event.
    existing = articles[-1]
    existing_id = uuid.uuid4()
    news_session.documents[existing.external_id] = {
        "id": existing_id,
        "title": existing.title,
        "body": existing.summary,
    }
    news_session.chunks[existing_id] = [
        {"id": uuid.uuid4(), "chunk_index": 0, "text": "기존 청크", "embedding": [0.25, -0.5, 0.75]}
    ]
    existing_chunks = deepcopy(news_session.chunks[existing_id])

    async def targets(*args):
        return [("005930", "삼성전자")]

    async def known(*args):
        return set(news_session.documents)

    monkeypatch.setattr(news_mod, "load_targets", targets)
    monkeypatch.setattr(news_mod, "existing_ids", known)
    monkeypatch.setattr(news_mod, "fetch_news", lambda *args, **kwargs: articles)
    monkeypatch.setattr(news_mod.settings, "naver_client_id", "test")
    monkeypatch.setattr(news_mod.settings, "naver_client_secret", "test")
    with caplog.at_level("INFO", logger="ingest.news"):
        assert asyncio.run(news_mod.run(7, 1, 400)) == (399, 399, 0)
    assert [event["importance"] for event in news_session.events] == [0.6, 0.5, 0.5]
    assert news_session.events[0]["document_id"] == existing_id
    assert "read=400 created=3 duplicates=0 not_promoted=397" in caplog.text
    assert "daily_cap=397 outside_window=0 failed_or_unattempted=0" in caplog.text
    assert news_session.chunks[existing_id] == existing_chunks
    assert len(news_session.chunks) == 400
    for chunks in news_session.chunks.values():
        for piece in chunks:
            piece["embedding"] = [0.25, -0.5, 0.75]
    original_chunks = deepcopy(news_session.chunks)
    caplog.clear()
    with caplog.at_level("INFO", logger="ingest.news"):
        assert asyncio.run(news_mod.run(7, 1, 400)) == (0, 0, 0)
    assert news_session.chunks == original_chunks
    assert len(news_session.events) == 3
    assert "read=400 created=0 duplicates=3 not_promoted=397" in caplog.text


@pytest.mark.parametrize("with_news", [True, False])
def test_promoted_news_reaches_briefing_freshness(news_session, monkeypatch, with_news):
    from tests.test_briefing import DAY, HOLDER, StubSession, _get, _make_client

    published = datetime(DAY.year, DAY.month, DAY.day, 0, 30, tzinfo=news_mod.KST)
    article = article_for(title="삼성전자 신제품 출시", published_at=published)
    asyncio.run(save(article))
    event = news_session.events[0]
    document = news_session.documents[article.external_id]
    events = (
        [
            (
                uuid.uuid4(),
                event["event_type"],
                event["ticker"],
                event["title"],
                event["event_date"],
                event["document_id"],
                event["importance"],
            )
        ]
        if with_news
        else []
    )
    documents = (
        [
            (
                document["id"],
                document["doc_type"],
                document["title"],
                document["source"],
                document["publisher"],
                document["url"],
                document["published_at"],
                document["body"],
            )
        ]
        if with_news
        else []
    )
    with _make_client(monkeypatch, StubSession(events=events, documents=documents)) as client:
        response = _get(client, HOLDER)
    assert response.status_code == 200
    body = response.json()
    assert body["data_as_of"]["news"] == ("2025-09-12T00:30:00+09:00" if with_news else None)
    if with_news:
        assert body["citations"][0]["type"] == "news"
        cited = [item for item in body["content"]["items"] if item["citations"]]
        assert cited[0]["category"] == "filing"
        assert cited[0]["event_type"] == "product"
        assert cited[0]["publisher"] == document["publisher"]
        assert cited[0]["citations"] == [body["citations"][0]["id"]]


def test_event_insert_failure_does_not_commit_document(news_session, monkeypatch):
    execute = news_session.execute

    async def failing_execute(statement, parameters=None):
        if str(statement).startswith("INSERT INTO events"):
            raise RuntimeError("event insert failed")
        return await execute(statement, parameters)

    monkeypatch.setattr(news_session, "execute", failing_execute)
    with pytest.raises(RuntimeError, match="event insert failed"):
        asyncio.run(save(article_for()))
    assert news_session.commits == 0


def test_expired_and_failed_articles_are_reported(monkeypatch, caplog):
    now = datetime.now(news_mod.KST)
    articles = [article_for(i, published_at=now) for i in range(3)]
    articles.append(article_for(3, published_at=now - timedelta(days=10)))

    async def targets(*args):
        return [("005930", "삼성전자")]

    async def known(*args):
        return set()

    calls = 0

    async def failing_save(article):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError("test failure")
        return news_mod.SaveResult(1, "created")

    monkeypatch.setattr(news_mod, "load_targets", targets)
    monkeypatch.setattr(news_mod, "existing_ids", known)
    monkeypatch.setattr(news_mod, "save", failing_save)
    monkeypatch.setattr(news_mod, "fetch_news", lambda *args, **kwargs: articles)
    monkeypatch.setattr(news_mod.settings, "naver_client_id", "test")
    monkeypatch.setattr(news_mod.settings, "naver_client_secret", "test")
    with caplog.at_level("INFO", logger="ingest.news"):
        assert asyncio.run(news_mod.run(7, 1, 4)) == (1, 1, 1)
    assert "read=4 created=1 duplicates=0 not_promoted=3" in caplog.text
    assert "daily_cap=0 outside_window=1 failed_or_unattempted=2" in caplog.text
