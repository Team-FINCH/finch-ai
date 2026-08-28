"""데일리 브리핑. API 명세 §8.

배치 생성 결과 조회이므로 GET이다. 랭킹은 규칙 엔진이 하고 LLM은 상위 4건만 문장화한다.
담당 트랙: feat/event-ranking
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import date as Date
from datetime import datetime, time, timedelta
from typing import Any

from fastapi import APIRouter
from pydantic import BaseModel
from sqlalchemy import or_, select

from app.api.deps import CurrentUser, DbSession, UsageLimit
from app.core.adapters import Ledger, ledger_source
from app.core.enums import BriefingStatus, CitationType, MetricSource, RateSensitivity
from app.core.errors import InsufficientData
from app.core.models import AIResponse, Document, Event
from app.core.response_log import record
from app.core.schemas import Citation, DataAsOf, Envelope, Segment, now_kst
from app.engines.attribution import EventRecord
from app.engines.briefing import (
    Candidate,
    RankedItem,
    event_candidates,
    holding_moves,
    item_key,
    rank,
    sector_shifts,
)
from app.engines.portfolio import PortfolioEngine, PortfolioSnapshot
from app.llm.client import NullLlmClient, get_llm_client
from app.llm.generate import generate_section, ratio_segment
from app.llm.guard import Feature
from app.llm.versioning import prompt_version_for

log = logging.getLogger("app.api.briefing")

router = APIRouter(prefix="/briefing", tags=["briefing"])

_SOURCE = MetricSource.PORTFOLIO_ENGINE

#: §5.2 novelty가 되짚는 구간.
_NOVELTY_DAYS = 7

#: 구조 변화의 기준 시점. 하루 전과 비교하면 매매가 없는 날은 항상 0이고, 원장
#: 첫날과 비교하면 반년 전 편입이 오늘의 뉴스가 된다. 한 달이 "구조"의 눈금이다.
_SHIFT_LOOKBACK_DAYS = 30

_ENDPOINT = "briefing"


class BriefingItem(BaseModel):
    rank: int
    category: str
    relevance_score: float | int
    title: str
    text: str
    segments: list[Segment]
    related_tickers: list[str]
    deeplink: str
    citations: list[str]


class BriefingContent(BaseModel):
    date: Date | None
    status: str
    generated_at: datetime
    items: list[BriefingItem]


# ── 원장 ──────────────────────────────────────────────────────────────────────
async def _ledger(user_id: str) -> Ledger | None:
    """원장 스냅샷. 못 읽으면 None이다(§11).

    브리핑에서 None은 오류가 아니다 — 진단과 달리 status "empty"로 나간다.
    """
    source = ledger_source()
    if source is None:
        return None
    try:
        ledger = await source.load(user_id)
    except (KeyError, FileNotFoundError, OSError):
        return None
    return ledger if ledger.trading_days else None


# ── 라우터 ────────────────────────────────────────────────────────────────────
@router.get("")
async def get_briefing(
    user_id: CurrentUser, db: DbSession, _usage: UsageLimit, date: Date | None = None
) -> Envelope[BriefingContent]:
    """그날 알아야 할 일 최대 4건(§8).

    보유 종목이 없거나 유의미한 항목이 없으면 409가 아니라 200 + status "empty"다.
    프런트가 그 값으로 섹션을 숨기기로 되어 있어서, 여기서 끊으면 화면이 에러를 문다.

    배치가 먼저 만든 결과가 있으면 재사용하고, 아직 없다면 요청 시점에 만들어
    저장한다. 따라서 배치가 잠시 늦어도 기존 화면은 계속 동작한다.
    """
    return await build_briefing(user_id, db, date)


async def build_briefing(
    user_id: str,
    db: DbSession,
    requested: Date | None = None,
    *,
    use_cache: bool = True,
) -> Envelope[BriefingContent]:
    """사용자·거래일별 브리핑을 조회하거나 생성한다.

    배치와 HTTP 라우터가 같은 함수를 써서 순위·문장·저장 규칙이 갈라지지 않는다.
    """
    now = now_kst()
    ledger = await _ledger(user_id)
    if ledger is None:
        return await _empty(db, user_id, requested, now)

    day = _resolve_day(ledger, requested)
    if day is None:
        return await _empty(db, user_id, requested, now)

    if use_cache and (cached := await _cached_briefing(db, user_id, day)):
        await record(db, cached, user_id=user_id, endpoint=_ENDPOINT)
        return cached

    engine = PortfolioEngine(ledger)
    snapshot = engine.snapshot(day)
    if not snapshot.holdings:
        return await _empty(db, user_id, day, now)

    rows = {row.trade_date: row for row in engine.daily_returns()}
    baseline = _baseline_snapshot(engine, ledger, day)
    candidates = [
        *holding_moves(snapshot, rows.get(day)),
        *sector_shifts(baseline, snapshot),
        *event_candidates(
            await _events(db, snapshot, day),
            snapshot,
            day,
            rate_sensitivity=_rate_sensitivity(snapshot),
        ),
    ]
    top = rank(candidates, seen_keys=await _seen_keys(db, user_id, now))
    if not top:
        return await _empty(db, user_id, day, now)

    citations_by_document, citations = await _briefing_citations(db, top)

    client = get_llm_client()
    if isinstance(client, NullLlmClient):
        # 키가 없으면 네 건이 같은 이유로 실패한다. 항목마다 null로 흩뿌리지 않는다.
        raise InsufficientData(
            "지금은 브리핑을 만들 수 없습니다.", detail={"reason": "llm_key_missing"}
        )

    outcomes = await asyncio.gather(
        *(
            generate_section(
                str(item.rank),
                title=item.candidate.title,
                feature=Feature.DAILY_BRIEFING_ITEM,
                prompt="daily_briefing",
                client=client,
                engine_values=_values(item.candidate),
                request=item.candidate.request,
            )
            for item in top
        )
    )

    items: list[dict[str, Any]] = []
    for item, outcome in zip(top, outcomes, strict=True):
        if outcome.section is None:
            # 문장이 없으면 항목 자체를 내리고 뒤를 당긴다. 화면에 제목만 남은 빈
            # 카드를 띄우느니 세 건짜리 브리핑이 낫다.
            log.warning(
                "브리핑 항목 %s 생성 실패 · %s", item.candidate.key, "; ".join(outcome.reasons)
            )
            continue
        items.append(
            _item_payload(
                item,
                outcome.section.model_dump(mode="json"),
                len(items) + 1,
                citations_by_document=citations_by_document,
            )
        )

    envelope = Envelope[BriefingContent](
        content={
            "date": day.isoformat(),
            "status": (BriefingStatus.READY if items else BriefingStatus.EMPTY).value,
            "generated_at": now.isoformat(),
            "items": items,
        },
        citations=citations,
        data_as_of=DataAsOf(
            price=_as_datetime(day),
            portfolio=_as_datetime(day),
            filings=max(
                (
                    citation.published_at
                    for citation in citations
                    if citation.type in {CitationType.FILING, CitationType.FINANCIAL}
                    and citation.published_at is not None
                ),
                default=None,
            ),
            news=max(
                (
                    citation.published_at
                    for citation in citations
                    if citation.type is CitationType.NEWS and citation.published_at is not None
                ),
                default=None,
            ),
            macro=max(
                (
                    citation.published_at
                    for citation in citations
                    if citation.type is CitationType.MACRO and citation.published_at is not None
                ),
                default=None,
            ),
        ),
    )
    # 다음 날 novelty가 읽을 행이 되고, 피드백이 참조할 행이 된다(§10).
    await record(db, envelope, user_id=user_id, endpoint=_ENDPOINT)
    return envelope


async def _cached_briefing(
    db: DbSession, user_id: str, day: Date
) -> Envelope[BriefingContent] | None:
    """같은 사용자·거래일·프롬프트 버전의 가장 최근 브리핑."""
    try:
        payloads = (
            await db.scalars(
                select(AIResponse.payload)
                .where(
                    AIResponse.user_id == user_id,
                    AIResponse.endpoint == _ENDPOINT,
                    AIResponse.prompt_version == prompt_version_for(_ENDPOINT),
                )
                .order_by(AIResponse.created_at.desc())
                .limit(20)
            )
        ).all()
    except Exception:  # 캐시는 최적화다. 조회 장애 시 원래 생성 경로로 복귀한다.
        log.warning("사용자 %s 브리핑 캐시 조회 실패", user_id, exc_info=True)
        return None

    for payload in payloads:
        if not isinstance(payload, dict):
            continue
        content = payload.get("content")
        if not isinstance(content, dict) or content.get("date") != day.isoformat():
            continue
        try:
            previous = Envelope[BriefingContent].model_validate(payload)
        except (TypeError, ValueError):
            continue
        return Envelope[BriefingContent](
            content=previous.content,
            citations=previous.citations,
            data_as_of=previous.data_as_of,
            cached=True,
        )
    return None


# ── 응답 조립 ─────────────────────────────────────────────────────────────────
async def _empty(
    db: DbSession, user_id: str, day: Date | None, now: datetime
) -> Envelope[BriefingContent]:
    """보유 종목이 없거나 내보낼 항목이 없을 때. 오류가 아니다."""
    envelope = Envelope[BriefingContent](
        content={
            "date": day.isoformat() if day else None,
            "status": BriefingStatus.EMPTY.value,
            "generated_at": now.isoformat(),
            "items": [],
        }
    )
    await record(db, envelope, user_id=user_id, endpoint=_ENDPOINT)
    return envelope


def _item_payload(
    item: RankedItem,
    section: dict[str, Any],
    rank_: int,
    *,
    citations_by_document: dict[str, str] | None = None,
) -> dict[str, Any]:
    """§8 `items` 한 줄. rank는 생성에 실패한 항목을 뺀 뒤 다시 매긴다."""
    candidate = item.candidate
    return {
        "rank": rank_,
        "category": candidate.category.value,
        "relevance_score": round(item.relevance, 4),
        "title": candidate.title,
        "text": section["text"],
        "segments": section["segments"],
        "related_tickers": list(candidate.related_tickers),
        "deeplink": candidate.deeplink,
        "citations": (
            [citation_id]
            if candidate.document_id
            and (citation_id := (citations_by_document or {}).get(candidate.document_id))
            else []
        ),
    }


async def _briefing_citations(
    db: DbSession, items: list[RankedItem]
) -> tuple[dict[str, str], list[Citation]]:
    """상위 브리핑 이벤트가 직접 연결한 문서를 한 번에 근거로 바꾼다."""
    ordered_ids: list[uuid.UUID] = []
    seen: set[uuid.UUID] = set()
    for item in items:
        raw = item.candidate.document_id
        if not raw:
            continue
        try:
            document_id = uuid.UUID(raw)
        except (TypeError, ValueError, AttributeError):
            log.warning("브리핑 이벤트의 document_id가 UUID가 아니다: %r", raw)
            continue
        if document_id not in seen:
            seen.add(document_id)
            ordered_ids.append(document_id)

    if not ordered_ids:
        return {}, []

    rows = (
        await db.execute(
            select(
                Document.id,
                Document.doc_type,
                Document.title,
                Document.source,
                Document.publisher,
                Document.url,
                Document.published_at,
                Document.body,
            ).where(Document.id.in_(ordered_ids))
        )
    ).all()
    documents = {row[0]: row for row in rows}
    mapping: dict[str, str] = {}
    citations: list[Citation] = []
    for document_id in ordered_ids:
        row = documents.get(document_id)
        if row is None:
            continue
        citation_id = f"cit_{len(citations) + 1}"
        mapping[str(document_id)] = citation_id
        citations.append(
            Citation(
                id=citation_id,
                type=CitationType(row[1]),
                title=row[2],
                source=row[3],
                publisher=row[4],
                url=row[5],
                published_at=row[6],
                snippet=(row[7] or "")[:180] or None,
            )
        )
    return mapping, citations


def _values(candidate: Candidate) -> dict[str, Segment]:
    """항목이 쓸 수 있는 자리표시자. 항목과 무관한 수치는 주지 않는다."""
    signed = {"return_rate", "sector_shift"}
    return {
        name: ratio_segment(value, _SOURCE, signed=name in signed, digits=2)
        for name, value in candidate.values.items()
    }


# ── 엔진 입력 ─────────────────────────────────────────────────────────────────
def _resolve_day(ledger: Ledger, requested: Date | None) -> Date | None:
    """기준 거래일. 요청일이 휴장일이면 그 이전 마지막 거래일로 내린다."""
    if requested is None:
        return ledger.trading_days[-1]
    past = [day for day in ledger.trading_days if day <= requested]
    return past[-1] if past else None


def _baseline_snapshot(engine: PortfolioEngine, ledger: Ledger, day: Date) -> PortfolioSnapshot:
    """구조 변화의 비교 기준. 한 달 안에 거래일이 하나뿐이면 자기 자신이라 변화가 0이다."""
    window = day - timedelta(days=_SHIFT_LOOKBACK_DAYS)
    earlier = [d for d in ledger.trading_days if window <= d <= day]
    return engine.snapshot(earlier[0] if earlier else day)


def _rate_sensitivity(snapshot: PortfolioSnapshot) -> RateSensitivity:
    """§5.2 거시 이벤트의 계수. 금리민감도 판정은 Risk Engine이 유일한 출처다."""
    from app.engines.risk import _rate_exposure

    return _rate_exposure(snapshot).level


async def _events(db: DbSession, snapshot: PortfolioSnapshot, day: Date) -> list[EventRecord]:
    """보유 종목 이벤트와 매크로 이벤트. 표가 비어 있으면 빈 목록이다.

    되짚는 구간은 novelty와 같은 7일이다 — recency가 exp(−days/2)라 그보다 오래된
    이벤트는 0.03배로 눌려 어차피 4위 안에 못 든다.
    """
    symbols = [h.symbol for h in snapshot.holdings]
    rows = (
        await db.execute(
            select(
                Event.id,
                Event.event_type,
                Event.ticker,
                Event.title,
                Event.event_date,
                Event.document_id,
                Event.importance,
            ).where(
                Event.event_date > day - timedelta(days=_NOVELTY_DAYS),
                Event.event_date <= day,
                or_(Event.ticker.in_(symbols), Event.ticker.is_(None)),
            )
        )
    ).all()
    return [
        EventRecord(
            event_id=str(event_id),
            event_type=event_type,
            title=title,
            event_date=event_date,
            ticker=ticker,
            document_id=str(document_id) if document_id else None,
            importance=float(importance),
        )
        for event_id, event_type, ticker, title, event_date, document_id, importance in rows
    ]


async def _seen_keys(db: DbSession, user_id: str, now: datetime) -> set[str]:
    """최근 7일 브리핑에 이미 나갔던 항목 key(§5.2 novelty).

    응답 로그의 payload에서 읽는다. 노출 이력 하나 때문에 스키마를 넓히는 대신
    `record()`가 이미 통째로 남기는 봉투를 되읽는다 — `last_risk_level`과 같은 수다.
    """
    stmt = select(AIResponse.payload).where(
        AIResponse.user_id == user_id,
        AIResponse.endpoint == _ENDPOINT,
        AIResponse.created_at >= now - timedelta(days=_NOVELTY_DAYS),
    )
    keys: set[str] = set()
    for payload in (await db.execute(stmt)).scalars():
        if not isinstance(payload, dict):
            continue
        content = payload.get("content")
        items = content.get("items") if isinstance(content, dict) else None
        for item in items or ():
            if not isinstance(item, dict):
                continue
            category, title = item.get("category"), item.get("title")
            if category and title:
                keys.add(item_key(category, item.get("related_tickers") or [], title))
    return keys


def _as_datetime(day: Date) -> datetime:
    """기준일을 장 마감 시각으로 본다. 종가 기준이기 때문이다."""
    return datetime.combine(day, time(15, 30))
