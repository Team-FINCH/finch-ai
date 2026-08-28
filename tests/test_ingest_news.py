"""NAVER API HUB 뉴스 수집기 테스트."""

from __future__ import annotations

import asyncio
import uuid
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
        _client(handler), "client-id", "client-secret", query="삼성전자", ticker="005930", max_docs=5
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
            _client(handler), "client-id", "client-secret", query="삼성전자", ticker="005930", max_docs=1
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


def test_기사와_검색조각을_한_트랜잭션으로_저장한다(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _Result:
        def scalar_one(self) -> uuid.UUID:
            return uuid.UUID("00000000-0000-0000-0000-000000000001")

    class _Session:
        def __init__(self) -> None:
            self.statements: list[Any] = []
            self.commits = 0

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_exc: object) -> None:
            return None

        async def execute(self, statement: Any) -> _Result:
            self.statements.append(statement)
            return _Result()

        async def commit(self) -> None:
            self.commits += 1

    session = _Session()
    monkeypatch.setattr(news_mod, "SessionFactory", lambda: session)
    article = parse_item(_item(), "005930")
    assert article is not None

    chunks = asyncio.run(save(article))

    assert chunks == 1
    assert len(session.statements) == 3  # document upsert, 기존 청크 삭제, 새 청크 insert
    assert session.commits == 1
