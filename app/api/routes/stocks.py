"""종목 AI 분석. API 명세 §3.

담당 트랙: feat/rag-dart, feat/rag-search, feat/llm-pipeline

섹션은 두 부류로 나뉜다. `current`·`changes`·`attention`·`risks`·`next_events`는
사용자와 무관해 종목 단위로 캐시할 수 있고, `my_impact`·`thesis_check`만
사용자별이다(§3 비용 설계). 공통 섹션은 같은 프롬프트 버전의 최근 응답을
6시간 재사용하고, 개인화 섹션은 요청마다 사용자의 최신 원장·논지로 만든다.
"""

from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta
from enum import StrEnum
from typing import Annotated, Any

from fastapi import APIRouter
from pydantic import BaseModel, Field, WithJsonSchema, field_serializer
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import CurrentUser, DbSession, UsageLimit
from app.core.config import settings
from app.core.enums import EventType
from app.core.errors import InsufficientData, InvalidRequest, LLMTimeout
from app.core.models import AIResponse, Event, Instrument
from app.core.response_log import record
from app.core.schemas import Citation, ContentModel, DataAsOf, Envelope, Segment, now_kst
from app.core.usage_limits import ANALYSIS_BATCH_USER, current_usage, reset_usage, usage_values
from app.llm.client import NullLlmClient, get_llm_client
from app.llm.generate import (
    SectionOutcome,
    citations_from_hits,
    documents_block,
    generate_section,
)
from app.llm.versioning import prompt_version_for
from app.rag.search import search

log = logging.getLogger("app.api.stocks")

router = APIRouter(prefix="/stocks", tags=["stocks"])


class AnalysisSectionKey(StrEnum):
    CURRENT = "current"
    CHANGES = "changes"
    ATTENTION = "attention"
    RISKS = "risks"
    MY_IMPACT = "my_impact"
    THESIS_CHECK = "thesis_check"
    NEXT_EVENTS = "next_events"


SECTIONS = tuple(key.value for key in AnalysisSectionKey)
AnalysisSectionName = Annotated[
    str,
    WithJsonSchema({"type": "string", "enum": list(SECTIONS)}),
]

#: 명칭은 규제 대응이다. "긍정/부정 요인"은 의견 제시로 읽히므로 출처 귀속형으로 고정한다.
SECTION_TITLES: dict[str, str] = {
    "current": "현재 상황",
    "changes": "최근 변화",
    "attention": "시장이 주목하는 요인",
    "risks": "확인해볼 위험",
    "my_impact": "내 계좌에서는",
    "thesis_check": "투자 논지 점검",
    "next_events": "앞으로 확인할 일정",
}

#: 사용자별 섹션. 나머지는 종목 단위 캐시 대상이다.
PERSONAL_SECTIONS = frozenset({"my_impact", "thesis_check"})
COMMON_SECTIONS = frozenset(SECTIONS) - PERSONAL_SECTIONS

_TICKER_RE = re.compile(r"\d{6}")
_RAG_TOP_K = 6
_UPCOMING_DAYS = 90
_UPCOMING_LIMIT = 5
_COMMON_CACHE_TTL = timedelta(hours=settings.analysis_cache_ttl_h)
#: 검사에 실패해 null 로 저장된 섹션을 다시 시도하기까지의 간격. 없으면 실패한
#: 섹션은 원본이 없어 매 요청 다시 만들다 또 실패해 화면이 늦고 토큰이 샌다.
#: 요청 경로는 실패한 섹션을 다시 시도하지 않는다 — 첫 조회자가 재시도를 기다리게
#: 되기 때문이다. 재시도는 아침 배치가 `retry_failed=True` 로 한다. 상한은 캐시 TTL 과
#: 같아 24시간 뒤에는 요청 경로도 (원본이 만료돼) 자연히 다시 만든다.
_FAILED_RETRY_AFTER = _COMMON_CACHE_TTL


class AnalysisRequest(BaseModel):
    sections: list[AnalysisSectionName] | None = Field(default=None)
    #: 계약 호환용. 종목 분석은 사용자와 무관해 이 값은 무시된다.
    personalize: bool = True


class ThesisRecord(BaseModel):
    text: str
    recorded_at: datetime
    source: str


class ThesisEvidence(ContentModel):
    citation_id: str
    title: str
    source: str
    rationale: str


class UpcomingEvent(ContentModel):
    id: str
    type: EventType
    title: str
    event_date: date
    confirmed: bool
    days_until: int


class AnalysisSection(ContentModel):
    title: str | None = None
    text: str
    segments: list[Segment]
    cached: bool = False
    cached_at: datetime | None = None
    thesis: ThesisRecord | None = None
    supporting: list[ThesisEvidence] | None = None
    challenging: list[ThesisEvidence] | None = None
    events: list[UpcomingEvent] | None = None


# thesis_check·next_events 전용 키다. 일반 섹션에는 실리지 않는다.
_SECTION_ONLY_KEYS = frozenset({"thesis", "supporting", "challenging", "events"})


def _section_payload(section: AnalysisSection) -> dict[str, Any]:
    """섹션 하나를 직렬화한다.

    exclude_unset을 쓰면 안 된다. 중첩 모델까지 전파돼 세그먼트의
    raw·unit·source·direction과 섹션의 cached까지 통째로 떨어진다. 계약상
    필수인 키가 사라져 프론트가 6종 전부에 방어 코드를 지게 된다 (GitLab #58).
    조건부 키만 골라 뺀다.
    """
    payload = section.model_dump(mode="json")
    for key in _SECTION_ONLY_KEYS - section.model_fields_set:
        payload.pop(key, None)
    return payload


class AnalysisSections(ContentModel):
    current: AnalysisSection | None = None
    changes: AnalysisSection | None = None
    attention: AnalysisSection | None = None
    risks: AnalysisSection | None = None
    my_impact: AnalysisSection | None = None
    thesis_check: AnalysisSection | None = None
    next_events: AnalysisSection | None = None


class AnalysisContent(BaseModel):
    ticker: str
    name: str
    sections: AnalysisSections

    @field_serializer("sections")
    def _serialize_sections(self, sections: AnalysisSections) -> dict[str, dict[str, Any] | None]:
        # 어떤 섹션 키를 실을지는 model_fields_set이 정한다 — 요청하지 않은 섹션은
        # 빠지고, 요청했으나 자료가 없는 섹션은 null로 남는다.
        out: dict[str, dict[str, Any] | None] = {}
        for key in sections.model_fields_set:
            section = getattr(sections, key)
            out[key] = None if section is None else _section_payload(section)
        return out


# ── 원장 ─────────────────────────────────────────────────
async def _upcoming_events(
    db: DbSession, ticker: str, *, today: date | None = None
) -> list[UpcomingEvent]:
    base = today or now_kst().date()
    end = base + timedelta(days=_UPCOMING_DAYS)
    rows = (
        await db.scalars(
            select(Event)
            .where(
                Event.ticker == ticker,
                Event.confirmed.is_(True),
                Event.event_date >= base,
                Event.event_date <= end,
            )
            .order_by(Event.event_date, Event.importance.desc())
            .limit(_UPCOMING_LIMIT)
        )
    ).all()
    return [
        UpcomingEvent(
            id=str(row.id),
            type=EventType(row.event_type),
            title=row.title,
            event_date=row.event_date,
            confirmed=row.confirmed,
            days_until=(row.event_date - base).days,
        )
        for row in rows
    ]


def _schedule_block(events: list[UpcomingEvent]) -> str:
    return "\n".join(
        f"- {event.event_date.year}년 {event.event_date.month}월 {event.event_date.day}일 "
        f"{event.title} ({event.type.value}, 확정)"
        for event in events
    )


@dataclass(frozen=True, slots=True)
class CachedAnalysis:
    name: str
    sections: dict[str, dict[str, Any]]
    citations: list[Citation]
    data_as_of: DataAsOf
    cached_at: datetime
    #: 최근 생성 시도에서 검사에 실패한 키. `_FAILED_RETRY_AFTER` 안에는 다시 만들지 않는다.
    failed: frozenset[str] = frozenset()


async def _cached_common_sections(
    db: DbSession,
    ticker: str,
    keys: set[str],
    *,
    now: datetime,
    user_id: str | None = None,
) -> CachedAnalysis | None:
    """`user_id` 를 주면 그 사용자의 응답만 본다 — 개인 섹션(`my_impact`·`thesis_check`) 용."""
    if not keys or db is None:
        return None
    version = prompt_version_for("stocks.analysis")
    try:
        rows = (
            await db.scalars(
                select(AIResponse)
                .where(
                    AIResponse.endpoint == "stocks.analysis",
                    AIResponse.prompt_version == version,
                    AIResponse.created_at >= now - _COMMON_CACHE_TTL,
                    *([AIResponse.user_id == user_id] if user_id else []),
                )
                .order_by(AIResponse.created_at.desc())
                .limit(20)
            )
        ).all()
    except Exception:  # 캐시는 최적화이므로 장애가 본래 분석을 막으면 안 된다.
        log.warning("종목 %s 공통 분석 캐시 조회 실패", ticker, exc_info=True)
        return None
    # 키마다 가장 최근에 성공한 행에서 가져온다. 한 행이 모든 키를 다 갖고 있어야
    # 히트로 치면, 섹션 하나가 검사에 실패해 null 로 저장된 순간 나머지 섹션까지
    # 매 요청 다시 만들게 된다. 없는 키만 호출부가 생성한다.
    selected: dict[str, dict[str, Any]] = {}
    failed: set[str] = set()
    newest: CachedAnalysis | None = None
    for row in rows:
        payload = row.payload
        if not isinstance(payload, dict):
            continue
        content = payload.get("content")
        if not isinstance(content, dict):
            continue
        sections = content.get("sections")
        if content.get("ticker") != ticker or not isinstance(sections, dict):
            continue
        cached_at = row.created_at
        if cached_at.tzinfo is None:
            cached_at = cached_at.replace(tzinfo=now.tzinfo)
        for key in keys - selected.keys():
            # 그 행에서 실제로 생성한 섹션만 원본이다. 복사된 섹션은 `cached: true` 로
            # 저장되므로 그걸 다시 집으면 TTL 이 영원히 늘어난다. 행 단위 `cached` 로
            # 가르면 일부만 생성한 행(배치의 부분 캐시 히트, 검사 실패 뒤 재생성)의
            # 새 섹션까지 버려져 매 요청 그 섹션을 다시 만드는 루프가 생긴다.
            section = sections.get(key)
            if section is not None and not section.get("cached"):
                selected[key] = {
                    **sections[key],
                    "cached": True,
                    "cached_at": cached_at.isoformat(),
                }
            elif (
                section is None
                and key in sections
                and not getattr(row, "cached", False)
                and key not in failed
                and cached_at >= now - _FAILED_RETRY_AFTER
            ):
                # 생성을 시도한 행(cached=false)에서 null 이면 그때 검사에 실패한 것이다.
                # 한 시간은 다시 시도하지 않는다. ponytail: 실패 사유는 안 본다 — 근거 부족
                # 같은 영구 실패도 한 시간마다 한 번은 다시 돈다.
                failed.add(key)
        if newest is None and (selected or failed):
            newest = CachedAnalysis(
                name=str(content.get("name") or ticker),
                sections=selected,
                citations=[Citation.model_validate(c) for c in payload.get("citations", [])],
                data_as_of=DataAsOf.model_validate(payload.get("data_as_of", {})),
                cached_at=cached_at,
            )
        if keys <= selected.keys() | failed:
            break
    if newest is not None:
        newest = replace(newest, failed=frozenset(failed - selected.keys()))
    return newest


def _hits_from_cache(cached: CachedAnalysis) -> list[dict[str, Any]]:
    return [
        {
            "text": citation.snippet or citation.title,
            "ticker": None,
            "title": citation.title,
            "published_at": citation.published_at,
            "doc_type": citation.type.value,
            "source": citation.source,
            "publisher": citation.publisher,
            "url": citation.url,
            "similarity": citation.relevance,
        }
        for citation in cached.citations
    ]


# ── 라우터 ───────────────────────────────────────────────
@router.post("/{ticker}/analysis")
async def create_analysis(
    ticker: str,
    body: AnalysisRequest,
    user_id: CurrentUser,
    db: DbSession,
    _usage: UsageLimit,
) -> Envelope[AnalysisContent]:
    """종목 분석. 본문은 `build_analysis` — 아침 배치(`ingest.briefings`)와 같은 함수다."""
    return await build_analysis(ticker, body, user_id, db)


async def build_analysis(
    ticker: str,
    body: AnalysisRequest,
    user_id: str,
    db: AsyncSession,
    *,
    retry_failed: bool = False,
) -> Envelope[AnalysisContent]:
    if not _TICKER_RE.fullmatch(ticker):
        raise InvalidRequest("종목코드는 6자리 숫자입니다.", detail={"ticker": ticker})

    # 종목 분석은 종목 단위 정보다 — 공시·뉴스만으로 만들고 사용자 보유·논지에
    # 따라 달라지지 않는다. 개인 섹션(my_impact·thesis_check)은 요청해도 만들지
    # 않고 응답에서 빠진다. 사용자별 생성이 요청마다 LLM 을 태우고 검사에 자주
    # 걸려 화면이 늦어지던 것이 이유다. 아침 배치가 30종목 공통 섹션을 미리 만든다.
    requested = [key for key in (body.sections or SECTIONS) if key not in PERSONAL_SECTIONS]
    unknown = [key for key in requested if key not in SECTION_TITLES]
    if unknown:
        raise InvalidRequest("알 수 없는 섹션입니다.", detail={"sections": unknown})

    now = now_kst()
    cached = await _cached_common_sections(db, ticker, set(requested), now=now)
    generation_keys = [
        key
        for key in requested
        if not (
            cached is not None
            and (key in cached.sections or (key in cached.failed and not retry_failed))
        )
    ]

    client = get_llm_client()
    if generation_keys and isinstance(client, NullLlmClient):
        raise InsufficientData(
            "지금은 분석을 만들 수 없습니다.", detail={"reason": "llm_key_missing"}
        )

    hits = _hits_from_cache(cached) if cached else []
    if generation_keys and not cached:
        hits = await search(
            f"{ticker} 최근 실적 공시 위험 요인",
            top_k=_RAG_TOP_K,
            ticker=ticker,
        )
    citations = cached.citations if cached else citations_from_hits(hits)
    documents = documents_block(hits, citations)
    if generation_keys and not hits:
        log.warning("종목 %s 검색 결과 0건 — 근거 없이 생성한다", ticker)

    upcoming_events = await _upcoming_events(db, ticker) if "next_events" in generation_keys else []

    shared = {"citations": citations, "documents": documents, "client": client}
    tasks: dict[str, Any] = {}
    for key in generation_keys:
        tasks[key] = generate_section(
            key,
            title=SECTION_TITLES[key],
            schedule=_schedule_block(upcoming_events) if key == "next_events" else "",
            **shared,
        )

    # 종목 분석은 사용자와 무관한 종목 단위 정보라 생성 비용을 요청자 개인 예산에
    # 물리지 않는다. 배치가 못 채운 종목을 첫 조회자가 대신 만드는 셈이라 배치 장부
    # (system:analysis-batch) 에서 나간다. 분당 요청 한도는 그대로 요청자 몫이다.
    counter = current_usage()
    token = (
        await counter.guard.enter_system(
            ANALYSIS_BATCH_USER,
            "stocks.analysis",
            now=now,
            budget=settings.ai_batch_daily_token_budget,
        )
        if counter is not None and tasks
        else None
    )
    usage = None
    try:
        outcomes: list[SectionOutcome] = list(await asyncio.gather(*tasks.values()))
    finally:
        if token is not None:
            # 장부는 배치 몫이지만 응답 로그의 토큰은 이 응답 것이어야 대시보드가 맞는다.
            usage = usage_values()
            reset_usage(token)
    if (
        tasks
        and all(o.section is None for o in outcomes)
        and any(r.startswith("llm_error:") for o in outcomes for r in o.reasons)
    ):
        # 일부만 실패하면 빈 섹션으로 내려 나머지를 살리지만, 전부 LLM 장애면
        # 살릴 것이 없다. 504 로 끝내 빈 응답이 캐시되지 않게 한다.
        raise LLMTimeout("분석 응답이 지연되어 중단했습니다. 다시 시도해 주세요.")

    sections: dict[str, Any] = dict.fromkeys(requested)
    if cached:
        sections.update(cached.sections)
    for outcome in outcomes:
        if outcome.section is None:
            log.warning("섹션 %s 생성 실패 · %s", outcome.key, "; ".join(outcome.reasons))
            continue
        sections[outcome.key] = outcome.section.model_dump(mode="json")

    if "next_events" in generation_keys and sections.get("next_events"):
        sections["next_events"]["events"] = [
            event.model_dump(mode="json") for event in upcoming_events
        ]

    cached_data = cached.data_as_of if cached else DataAsOf()
    envelope = Envelope[AnalysisContent](
        content={
            "ticker": ticker,
            "name": await _display_name(db, ticker, cached.name if cached else None),
            "sections": sections,
        },
        citations=citations,
        # 요청한 섹션을 하나도 새로 만들지 않았을 때만 캐시 히트다. 일부만 생성한
        # 응답을 true 로 남기면 대시보드 히트율이 부풀고 토큰이 0 이 아닌 히트가 생긴다.
        cached=bool(cached) and not tasks,
        data_as_of=DataAsOf(
            price=cached_data.price,
            filings=max(
                (h["published_at"] for h in hits if h.get("published_at")),
                default=cached_data.filings,
            ),
            news=cached_data.news,
            macro=cached_data.macro,
        ),
    )
    await record(db, envelope, user_id=user_id, endpoint="stocks.analysis", **(usage or {}))
    return envelope


async def _display_name(db: DbSession, ticker: str, fallback: str | None) -> str:
    name = await db.scalar(select(Instrument.name).where(Instrument.ticker == ticker))
    return name or fallback or ticker
