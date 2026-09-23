"""NAVER API HUB 뉴스 검색 결과를 RAG 문서로 적재한다.

검색 API가 제공하는 제목·요약·원문 링크만 저장한다. 언론사 페이지의 본문을
추가로 크롤링하지 않는다.

    python -m ingest.news --days 2

종목을 명시하지 않으면 Settings.service_tickers의 확정 30종목을 사용한다. 저장한
documents/document_chunks는 채팅 RAG가, 같은 문서에서 승격한 events는 데일리
브리핑이 함께 사용한다.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import html
import logging
import re
import time
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import urldefrag, urlsplit, urlunsplit

import httpx
from sqlalchemy import delete, func, or_, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.core.config import settings
from app.core.db import SessionFactory
from app.core.enums import DocumentType, EventType
from app.core.models import Document, DocumentChunk, Event, Instrument
from app.rag.chunking import chunk
from app.rag.lexical import weighted_tsvector
from ingest.universe import report_resolution, target_tickers

log = logging.getLogger("ingest.news")

NEWS_URL = "https://naverapihub.apigw.ntruss.com/search/v1/news"
SOURCE = "NAVER_API_HUB"
HTTP_TIMEOUT = 30.0
PAGE_SIZE = 100
PAGE_LIMIT = 10
RETRY_MAX = 3
KST = timezone(timedelta(hours=9))
# Bound daily news volume relative to the four-item briefing; keep all documents for RAG.
MAX_EVENTS_PER_TICKER_DAY = 3
RULES: list[tuple[tuple[str, ...], EventType, float]] = [
    (("실적", "영업이익", "순이익", "매출"), EventType.EARNINGS, 0.6),
    (("배당", "자사주"), EventType.DIVIDEND, 0.5),
    (("수주", "공급계약", "신제품", "출시"), EventType.PRODUCT, 0.5),
    (("기준금리", "통화정책", "환율"), EventType.MACRO, 0.4),
]
# Headlines matching no rule are plain news, not filings (FINCH-346). The
# briefing picks its label from event_type, so FILING made every article read 공시.
DEFAULT = (EventType.NEWS, 0.3)


def classify(title: str) -> tuple[EventType, float]:
    """Conservative headline signals; even earnings news ranks below a 0.7 filing."""
    for keywords, event_type, importance in RULES:
        if any(keyword in title for keyword in keywords):
            return event_type, importance
    return DEFAULT


def priority(article: NewsArticle) -> tuple[float, float, str]:
    """Highest importance, newest publication, then stable URL identity."""
    return (-classify(article.title)[1], -article.published_at.timestamp(), article.external_id)


@dataclass(frozen=True, slots=True)
class SaveResult:
    chunks: int
    promotion: str  # created | duplicate | capped


_TAGS = re.compile(r"<[^>]+>")


class NaverNewsError(RuntimeError):
    """API HUB 요청이나 응답을 처리할 수 없을 때."""


def clean_text(value: str | None) -> str:
    """검색어 강조 태그와 HTML 엔티티를 제거한다."""
    return " ".join(_TAGS.sub("", html.unescape(value or "")).split())


def canonical_url(value: str | None) -> str:
    """중복 판정용 URL. fragment와 호스트 표기 차이만 제거한다."""
    raw, _fragment = urldefrag((value or "").strip())
    if not raw:
        return ""
    parts = urlsplit(raw)
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), parts.path, parts.query, ""))


def external_id(url: str) -> str:
    return hashlib.sha256(url.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class NewsArticle:
    external_id: str
    ticker: str
    title: str
    summary: str
    url: str
    publisher: str | None
    published_at: datetime


def parse_item(item: dict, ticker: str) -> NewsArticle | None:
    """네이버 검색 결과 한 건을 저장 가능한 내부 모양으로 바꾼다."""
    title = clean_text(item.get("title"))
    summary = clean_text(item.get("description"))
    url = canonical_url(item.get("originallink") or item.get("link"))
    if not title or not summary or not url:
        return None
    try:
        published_at = parsedate_to_datetime(item["pubDate"])
    except (KeyError, TypeError, ValueError):
        return None
    if published_at.tzinfo is None:
        published_at = published_at.replace(tzinfo=KST)
    host = urlsplit(url).hostname
    return NewsArticle(
        external_id=external_id(url),
        ticker=ticker,
        title=title,
        summary=summary,
        url=url,
        publisher=host.lower() if host else None,
        published_at=published_at,
    )


def fetch_news(
    client: httpx.Client,
    client_id: str,
    client_secret: str,
    *,
    query: str,
    ticker: str,
    max_docs: int,
) -> list[NewsArticle]:
    """한 검색어의 최신 뉴스. API 페이지 한계 안에서 max_docs까지 가져온다."""
    if max_docs <= 0:
        return []
    headers = {
        "X-NCP-APIGW-API-KEY-ID": client_id,
        "X-NCP-APIGW-API-KEY": client_secret,
    }
    articles: list[NewsArticle] = []
    start = 1
    for _page in range(PAGE_LIMIT):
        display = min(PAGE_SIZE, max_docs - len(articles))
        if display <= 0 or start > 1000:
            break
        response: httpx.Response | None = None
        for attempt in range(1, RETRY_MAX + 1):
            try:
                response = client.get(
                    NEWS_URL,
                    params={
                        "query": query,
                        "display": display,
                        "start": start,
                        "sort": "date",
                        "format": "json",
                    },
                    headers=headers,
                    timeout=HTTP_TIMEOUT,
                )
            except httpx.HTTPError as exc:
                if attempt == RETRY_MAX:
                    raise NaverNewsError("뉴스 검색 요청에 실패했습니다.") from exc
                time.sleep(float(attempt))
                continue
            if response.status_code != 429:
                break
            if attempt == RETRY_MAX:
                break
            wait = float(response.headers.get("retry-after") or attempt)
            time.sleep(min(wait, 30.0))

        assert response is not None
        if response.status_code != 200:
            try:
                payload = response.json()
            except ValueError:
                payload = {}
            error = payload.get("error") or payload
            code = error.get("errorCode") or error.get("code") or response.status_code
            raise NaverNewsError(
                f"뉴스 검색이 거부됐습니다: status={response.status_code} code={code}"
            )
        try:
            payload = response.json()
        except ValueError as exc:
            raise NaverNewsError("뉴스 검색 응답이 JSON이 아닙니다.") from exc

        items = payload.get("items") or []
        for item in items:
            article = parse_item(item, ticker)
            if article is not None:
                articles.append(article)
                if len(articles) >= max_docs:
                    break
        if len(items) < display or start + display > min(int(payload.get("total") or 0), 1000):
            break
        start += display
    return articles


async def load_targets(
    limit: int | None = None, tickers: Sequence[str] | None = None
) -> list[tuple[str, str]]:
    """뉴스·공시 대상 (ticker, name). 기본은 서비스 종목이다."""
    targets = target_tickers(tickers, limit)
    async with SessionFactory() as session:
        stmt = select(Instrument.ticker, Instrument.name).where(
            Instrument.status == "listed", Instrument.ticker.in_(targets)
        )
        names = {row.ticker: row.name for row in await session.execute(stmt)}
        report_resolution(log, targets, list(names), explicit=bool(tickers))
        return [(ticker, names[ticker]) for ticker in targets if ticker in names]


async def existing_ids(ids: Sequence[str]) -> set[str]:
    if not ids:
        return set()
    async with SessionFactory() as session:
        result = await session.execute(
            select(Document.external_id).where(
                Document.source == SOURCE, Document.external_id.in_(ids)
            )
        )
        return set(result.scalars())


async def save(article: NewsArticle) -> SaveResult:
    """Upsert documents and admit events atomically.

    Unchanged content keeps its chunks and embeddings; promotion runs either way.

    Call in priority order to fill remaining daily slots with the best headlines.
    Existing events retain their slots, including across subsequent runs.
    """
    statement = pg_insert(Document).values(
        doc_type=DocumentType.NEWS,
        source=SOURCE,
        external_id=article.external_id,
        ticker=article.ticker,
        title=article.title,
        body=article.summary,
        url=article.url,
        publisher=article.publisher,
        published_at=article.published_at,
    )
    statement = statement.on_conflict_do_nothing(
        constraint="uq_documents_source_external",
    ).returning(Document.id)

    pieces = []
    async with SessionFactory() as session:
        event_date = article.published_at.astimezone(KST).date()
        # Events have no unique constraint. Serialize admission for this ticker/day.
        lock_key = int.from_bytes(
            hashlib.sha256(f"news:{article.ticker}:{event_date}".encode()).digest()[:8],
            signed=True,
        )
        await session.execute(select(func.pg_advisory_xact_lock(lock_key)))
        document_id = (await session.execute(statement)).scalar_one_or_none()
        rewrite_chunks = document_id is not None
        if document_id is None:
            document = (
                await session.execute(
                    select(Document.id, Document.title, Document.body)
                    .where(Document.source == SOURCE, Document.external_id == article.external_id)
                    .with_for_update()
                )
            ).one()
            document_id = document.id
            # These are the inputs to both chunk text and its weighted tsvector.
            rewrite_chunks = (document.title, document.body) != (article.title, article.summary)
            await session.execute(
                update(Document)
                .where(Document.id == document_id)
                .values(
                    title=article.title,
                    body=article.summary,
                    url=article.url,
                    publisher=article.publisher,
                    published_at=article.published_at,
                )
            )
            if rewrite_chunks:
                await session.execute(
                    delete(DocumentChunk).where(DocumentChunk.document_id == document_id)
                )
        if rewrite_chunks:
            pieces = chunk(f"{article.title}\n\n{article.summary}")
        if pieces:
            await session.execute(
                pg_insert(DocumentChunk).values(
                    [
                        {
                            "document_id": document_id,
                            "chunk_index": index,
                            "text": text,
                            "text_tsv": weighted_tsvector(article.title, text),
                        }
                        for index, text in enumerate(pieces)
                    ]
                )
            )
        rows = (
            await session.execute(
                select(Event.title, Event.document_id, Document.doc_type)
                .outerjoin(Document, Event.document_id == Document.id)
                .where(
                    Event.ticker == article.ticker,
                    or_(Event.event_date == event_date, Event.document_id == document_id),
                )
            )
        ).all()
        if any(title == article.title or linked_id == document_id for title, linked_id, _ in rows):
            promotion = "duplicate"
        elif (
            sum(doc_type == DocumentType.NEWS for _, _, doc_type in rows)
            >= MAX_EVENTS_PER_TICKER_DAY
        ):
            promotion = "capped"
        else:
            event_type, importance = classify(article.title)
            await session.execute(
                Event.__table__.insert(),
                [
                    {
                        "ticker": article.ticker,
                        "event_type": event_type.value,
                        "event_date": event_date,
                        "title": article.title,
                        "importance": importance,
                        "document_id": document_id,
                    }
                ],
            )
            promotion = "created"
        await session.commit()
    return SaveResult(len(pieces), promotion)


async def run(
    days: int, limit: int | None, max_docs: int, tickers: Sequence[str] | None = None
) -> tuple[int, int, int]:
    """(적재 기사 수, 청크 수, 실패 종목 수)."""
    client_id = (settings.naver_client_id or "").strip()
    client_secret = (settings.naver_client_secret or "").strip()
    if not client_id or not client_secret:
        log.error("NAVER_CLIENT_ID와 NAVER_CLIENT_SECRET이 필요하다")
        return 0, 0, 1
    targets = await load_targets(limit, tickers)
    if not targets:
        log.error("뉴스 검색 대상 종목이 없다. 먼저 종목 마스터를 적재하라")
        return 0, 0, 1

    cutoff = datetime.now(KST) - timedelta(days=max(days, 1))
    saved = chunks = failed = 0
    with httpx.Client() as client:
        for index, (ticker, name) in enumerate(targets, start=1):
            try:
                articles = fetch_news(
                    client,
                    client_id,
                    client_secret,
                    query=name,
                    ticker=ticker,
                    max_docs=max_docs,
                )
                recent = [article for article in articles if article.published_at >= cutoff]
                known = await existing_ids([article.external_id for article in recent])
                pending = [article for article in recent if article.external_id not in known]
                log.info(
                    "[%d/%d] %s(%s) 검색 %d건 · 최근 %d건 · 신규 %d건",
                    index,
                    len(targets),
                    name,
                    ticker,
                    len(articles),
                    len(recent),
                    len(pending),
                )
                counts = {"created": 0, "duplicate": 0, "capped": 0}
                processed = 0
                try:
                    # Known documents also need promotion: earlier ingest versions made no events.
                    for article in sorted(recent, key=priority):
                        result = await save(article)
                        chunks += result.chunks
                        counts[result.promotion] += 1
                        processed += 1
                        if article.external_id not in known:
                            saved += 1
                            known.add(article.external_id)
                finally:
                    log.info(
                        "news promotion ticker=%s read=%d created=%d duplicates=%d "
                        "not_promoted=%d (daily_cap=%d outside_window=%d failed_or_unattempted=%d)",
                        ticker,
                        len(articles),
                        counts["created"],
                        counts["duplicate"],
                        counts["capped"] + len(articles) - processed,
                        counts["capped"],
                        len(articles) - len(recent),
                        len(recent) - processed,
                    )
            except Exception:  # noqa: BLE001 — 한 종목 실패가 전체 수집을 막지 않는다
                log.exception("뉴스 적재 실패: ticker=%s", ticker)
                failed += 1
    return saved, chunks, failed


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="NAVER API HUB 뉴스 적재기")
    parser.add_argument("--days", type=int, default=7, help="최근 며칠 기사. 기본 7")
    parser.add_argument("--limit", type=int, default=None, help="서비스 목록의 앞 N종목만 (기본 전체)")
    parser.add_argument("--tickers", help="쉼표 구분 종목코드")
    parser.add_argument("--max-docs", type=int, default=20, help="종목당 최대 기사 수")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s | %(message)s",
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    tickers = [x.strip() for x in args.tickers.split(",") if x.strip()] if args.tickers else None
    _, _, failed = asyncio.run(run(args.days, args.limit, args.max_docs, tickers))
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
