"""포트폴리오 진단과 수익률 원인 분석. API 명세 §5 · §6.

담당 트랙: feat/engine-risk, feat/engine-attribution

`diagnosis`는 Risk Engine이 뽑은 숫자를 그대로 내보내고 문장만 LLM에 맡긴다.
`risk_level`·`risk_score`·`findings`의 존재와 순서는 전부 엔진 판정이며(산식 §3.6·§3.7)
모델은 각 항목의 서술 세 문장만 쓴다. 항목 제목은 서버 상수다 — `NARRATIVE_SCHEMA`가
title 필드를 갖고 있지 않기도 하고, 모델이 쓴 제목에는 판정이 섞여 들어온다.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from collections import defaultdict
from collections.abc import Sequence
from datetime import date, datetime, time, timedelta
from typing import Any

from fastapi import APIRouter
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import CurrentUser, DbSession, UsageLimit
from app.core.adapters import Ledger, ledger_source
from app.core.config import settings
from app.core.enums import MetricSource, Period
from app.core.errors import InsufficientData, InvalidRequest
from app.core.models import (
    AIResponse,
    Event,
    IndexDaily,
    Instrument,
    PortfolioDiagnosisCache,
    PriceDaily,
)
from app.core.response_log import last_risk_level, record
from app.core.schemas import DataAsOf, Envelope, Section, Segment, now_kst
from app.engines.attribution import (
    AttributionResult,
    BenchmarkDay,
    ContributorRow,
    EventRecord,
    attribute,
)
from app.engines.portfolio import PortfolioEngine, PortfolioSnapshot
from app.engines.risk import Finding, RiskAssessment, assess
from app.llm.client import NullLlmClient, get_llm_client
from app.llm.generate import SectionOutcome, generate_section, ratio_segment
from app.llm.guard import Feature
from app.llm.versioning import prompt_version_for

log = logging.getLogger("app.api.portfolio")

router = APIRouter(prefix="/portfolio", tags=["portfolio"])
Number = float | int

#: 벤치마크는 §3.4 베타 계산용이다. `index_daily`가 KOSPI로 적재되어 있다.
_BENCHMARK_CODE = "KOSPI"

_SUMMARY_KEY = "summary"
_SUMMARY_TITLE = "종합 진단"
_DIAGNOSIS_ENGINE_VERSION = "risk-v1"

#: 제목은 규제 대응이다. finding id는 엔진이 정하므로 여기 없는 id가 오면 id를 그대로
#: 쓴다 — 새 finding이 추가될 때 제목이 없다고 500이 나면 안 된다.
_FINDING_TITLES: dict[str, str] = {
    "ticker_concentration": "단일 종목 집중",
    "sector_concentration": "업종 집중",
    "volatility": "높은 변동성",
    "correlation": "제한된 분산 효과",
    "liquidity": "얇은 현금 여력",
    "macro_exposure": "금리 국면 노출",
}


class AttributionRequest(BaseModel):
    period: Period = Period.D1
    benchmark: str | None = None


class DiagnosisFinding(BaseModel):
    id: str
    category: str
    severity: str
    title: str
    text: str | None
    segments: list[Segment] | None
    evidence: dict[str, Any]


class DiagnosisIndicators(BaseModel):
    sector_count: int
    hhi: Number
    top1_weight: Number
    top3_weight: Number
    sector_hhi: Number
    annualized_volatility: Number | None
    max_drawdown_1y: Number
    cash_ratio: Number
    rate_sensitivity: str
    beta: Number | None
    large_cap_weight: Number | None
    diversification_ratio: Number | None


class DiagnosisContent(BaseModel):
    risk_level: str | None
    risk_score: int | None
    insufficient_history: str | None
    summary: Section | None
    findings: list[DiagnosisFinding]
    indicators: DiagnosisIndicators


class AttributionBreakdown(BaseModel):
    market: Number
    sector: Number
    selection: Number


class AttributionEvent(BaseModel):
    citation_id: str | None
    type: str
    title: str
    summary: str
    event_date: date
    matched_confidence: Number


class AttributionContributor(BaseModel):
    # 응답 로그는 별칭 없이 `return_` 로 저장하고, 캐시 조회는 그 JSON 을 되읽는다.
    # 두 이름을 다 받아야 저장값이 검증을 통과한다.
    model_config = ConfigDict(populate_by_name=True)

    ticker: str
    name: str
    sector: str
    weight: Number
    return_: Number = Field(alias="return", serialization_alias="return")
    contribution: Number
    held_at_start: bool
    events: list[AttributionEvent]


class AttributionSector(BaseModel):
    sector: str
    portfolio_weight: Number
    benchmark_weight: Number
    allocation: Number
    selection: Number
    proxy: bool


class AttributionContent(BaseModel):
    period: str
    start: date
    end: date
    trading_days: int
    portfolio_return: Number
    total_return: Number
    benchmark_return: Number
    excess_return: Number
    breakdown: AttributionBreakdown
    contributors: list[AttributionContributor]
    detractors: list[AttributionContributor]
    sectors: list[AttributionSector]
    notes: list[str]
    summary: Section | None
    text: str | None
    segments: list[Segment] | None


# ── 원장 ──────────────────────────────────────────────────────────────────────
async def _ledger(user_id: str) -> Ledger | None:
    """원장 스냅샷. 못 읽으면 None이고, 읽은 원장은 비어 있어도 그대로 반환한다.

    어느 원장을 읽을지는 `ledger_source()` 가 정한다(설정 `LEDGER_SOURCE`). `stocks.py`는 스냅샷
    하나만 필요해서 거기서 끝나지만, Risk Engine은 `prices`와 거래일 전체를 받으므로
    여기서는 `Ledger`를 그대로 들고 나온다.
    """
    source = ledger_source()
    if source is None:
        return None
    try:
        ledger = await source.load(user_id)
    except (KeyError, FileNotFoundError, OSError):
        return None
    return ledger


# ── 엔진 입력(DB) ─────────────────────────────────────────────────────────────
async def _benchmark(db: DbSession) -> dict[Any, float] | None:
    """§3.4 베타용 벤치마크 종가. 적재 전이면 None이고 베타만 빠진다."""
    rows = (
        await db.execute(
            select(IndexDaily.trade_date, IndexDaily.close).where(
                IndexDaily.index_code == _BENCHMARK_CODE
            )
        )
    ).all()
    return {day: float(close) for day, close in rows} or None


async def _market_cap_ranks(db: DbSession, symbols: tuple[str, ...]) -> dict[str, int] | None:
    """§3.5 대형주 판정용 시가총액 순위. 1위가 최대다.

    순위는 시장 전체에서 매겨야 뜻이 있으므로 윈도 함수로 전체를 세운 뒤 보유 종목만
    가져온다. 보유분만 정렬하면 8종목 중 1위가 곧 대형주가 되어 버린다.
    """
    if not symbols:
        return None
    ranked = (
        select(
            Instrument.ticker,
            func.row_number().over(order_by=Instrument.market_cap.desc()).label("rank"),
        )
        .where(Instrument.market_cap.is_not(None))
        .subquery()
    )
    rows = (
        await db.execute(select(ranked.c.ticker, ranked.c.rank).where(ranked.c.ticker.in_(symbols)))
    ).all()
    return {ticker: int(rank) for ticker, rank in rows} or None


# ── 자리표시자 ────────────────────────────────────────────────────────────────
_SOURCE = MetricSource.RISK_ENGINE


def _indicator_segments(result: RiskAssessment) -> dict[str, Segment]:
    """요약이 쓸 수 있는 지표. None인 지표는 목록에서 빠져 모델이 못 쓴다."""
    values: dict[str, Segment] = {
        "hhi": ratio_segment(result.concentration.hhi, _SOURCE, digits=2),
        "top1_weight": ratio_segment(result.concentration.top1, _SOURCE),
        "top3_weight": ratio_segment(result.concentration.top3, _SOURCE),
        "top_sector_weight": ratio_segment(result.top_sector_weight, _SOURCE),
        "cash_ratio": ratio_segment(result.cash_weight, _SOURCE),
        "max_drawdown_1y": ratio_segment(result.drawdown.mdd, _SOURCE, signed=True),
    }
    if result.volatility is not None:
        values["annualized_volatility"] = ratio_segment(result.volatility.portfolio, _SOURCE)
    if result.diversification is not None:
        values["diversification_ratio"] = ratio_segment(
            result.diversification.ratio, _SOURCE, digits=2
        )
        if result.diversification.avg_correlation is not None:
            values["avg_pairwise_corr"] = ratio_segment(
                result.diversification.avg_correlation, _SOURCE, digits=2
            )
    if result.large_cap_weight is not None:
        values["large_cap_weight"] = ratio_segment(result.large_cap_weight, _SOURCE)
    if result.beta is not None:
        values["beta"] = ratio_segment(result.beta, _SOURCE, digits=2)
    return values


#: finding 하나가 쓸 수 있는 자리표시자. 전체 지표를 다 주면 항목과 무관한 숫자를
#: 끌어다 쓰므로 항목별로 좁힌다. 임계값은 언제나 함께 준다 — "왜 걸렸는가"의 절반이다.
_FINDING_VALUE_KEYS: dict[str, tuple[str, ...]] = {
    "ticker_concentration": ("top1_weight", "top3_weight", "hhi"),
    "sector_concentration": ("top_sector_weight", "hhi"),
    "volatility": ("annualized_volatility", "max_drawdown_1y"),
    "correlation": ("diversification_ratio", "avg_pairwise_corr"),
    "liquidity": ("cash_ratio",),
    # 금리민감도 점수(와 그 임계값)는 비율이 아니라 가중 합이다. `ratio_segment`로
    # 내보내면 "74.9%"처럼 보여 비중으로 읽힌다. 등급 자체는 [요청]으로 넘어가므로
    # 여기서는 업종 비중만 준다.
    "macro_exposure": ("top_sector_weight",),
}


def _finding_values(finding: Finding, indicators: dict[str, Segment]) -> dict[str, Segment]:
    values = {
        key: indicators[key] for key in _FINDING_VALUE_KEYS.get(finding.id, ()) if key in indicators
    }
    if finding.id != "macro_exposure":
        values["threshold"] = ratio_segment(finding.threshold, _SOURCE)
    return values


def _evidence(finding: Finding, result: RiskAssessment, symbols: list[str]) -> dict[str, Any]:
    """§5 표 — LLM 입력으로 쓰인 원시 지표. 디버깅·평가용으로 응답에 그대로 담는다."""
    evidence: dict[str, Any] = {
        "tickers": symbols,
        "metric": finding.metric,
        "value": finding.value,
        "threshold": finding.threshold,
        "hhi": result.concentration.hhi,
    }
    if result.diversification is not None:
        evidence["avg_pairwise_corr"] = result.diversification.avg_correlation
    if finding.id == "sector_concentration":
        evidence["sector"] = result.top_sector
    if finding.id == "macro_exposure":
        evidence["rate_sensitivity"] = result.rate_exposure.level.value
    return evidence


def _summary_request(result: RiskAssessment) -> str:
    """요약이 참고할 엔진 판정. 등급과 항목 순서는 여기서만 알려주고 다시 매기지 않게 한다."""
    level = result.risk_level.value if result.risk_level is not None else "판정 보류"
    titles = ", ".join(_FINDING_TITLES.get(f.id, f.id) for f in result.findings) or "없음"
    parts = [
        "포트폴리오 진단 요약을 작성하십시오.",
        f"엔진 판정 위험 수준: {level}.",
        f"엔진이 잡은 위험 항목(중요도 순): {titles}.",
    ]
    if result.insufficient_history:
        parts.append(
            f"변동성·상관 지표는 계산되지 않았습니다({result.insufficient_history}). "
            "해당 지표를 언급하거나 추정하지 마십시오."
        )
    return " ".join(parts)


def _diagnosis_fingerprint(snapshot: PortfolioSnapshot, prompt_version: str) -> str:
    """진단 결과를 바꿀 수 있는 포트폴리오·시세·코드 버전을 안정적으로 해시한다."""
    source = {
        "engine": _DIAGNOSIS_ENGINE_VERSION,
        "prompt": prompt_version,
        "model": settings.llm_model,
        "trade_date": snapshot.trade_date.isoformat(),
        "cash": format(snapshot.cash, ".8f"),
        "holdings": [
            {
                "symbol": holding.symbol,
                "quantity": format(holding.quantity, ".8f"),
                "avg_cost": format(holding.avg_cost, ".8f"),
                # 현재가는 넣지 않는다. 장중 호가가 바뀔 때마다 지문이 바뀌어 조회마다
                # 재생성됐다(운영 히트율 26%). 지표는 종가 시계열로 계산하므로 거래일이면 된다.
            }
            for holding in sorted(snapshot.holdings, key=lambda item: item.symbol)
        ],
    }
    encoded = json.dumps(source, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode()).hexdigest()


def _fallback_summary() -> dict[str, Any]:
    """GMS 문장이 차단돼도 진단 화면 전체가 비지 않게 하는 서버 설명."""
    return Section(
        title=_SUMMARY_TITLE,
        text=(
            "현재 포트폴리오의 위험 지표를 계산했습니다. "
            "아래 항목에서 집중도와 변동성 등 주요 위험을 확인해 주세요."
        ),
    ).model_dump(mode="json")


def _fallback_finding(finding: Finding) -> dict[str, Any]:
    title = _FINDING_TITLES.get(finding.id, finding.id)
    return Section(
        title=title,
        text=f"{title} 항목이 엔진 기준을 넘었습니다. 현재 포트폴리오 구성을 확인해 주세요.",
    ).model_dump(mode="json")


def _cached_content(row: PortfolioDiagnosisCache) -> DiagnosisContent | None:
    try:
        content = DiagnosisContent.model_validate(row.payload)
    except Exception:
        log.warning("저장된 포트폴리오 진단 형식이 올바르지 않다 · user_id=%s", row.user_id)
        return None
    if content.summary is not None:
        content.summary = content.summary.model_copy(
            update={"cached": True, "cached_at": row.generated_at}
        )
    return content


# ── 라우터 ────────────────────────────────────────────────────────────────────
@router.post("/diagnosis")
async def diagnosis(
    user_id: CurrentUser, db: DbSession, _usage: UsageLimit
) -> Envelope[DiagnosisContent]:
    """위험 지표를 계산하고 상위 항목을 설명한다(§5). 본문은 `build_diagnosis`."""
    # 요청 시점에는 저장된 진단을 그대로 준다. 보유가 바뀌어도 다시 만들지 않는다 —
    # 자주 매매하는 사용자가 조회마다 LLM 을 태우지 않게. 재생성은 아침 배치가 한다.
    # 저장된 것이 하나도 없을 때(첫 사용자)만 한 번 만든다.
    return await build_diagnosis(user_id, db, regenerate=False)


async def build_diagnosis(
    user_id: str, db: AsyncSession, *, regenerate: bool = True
) -> Envelope[DiagnosisContent]:
    """사용자 진단을 조회하거나 생성한다. 배치(`ingest.briefings`)와 라우터가 같이 쓴다.

    `regenerate=True`(배치)면 지문이 바뀌었을 때 다시 만들고, `False`(라우터)면 저장된
    진단이 있는 한 지문과 무관하게 그것을 돌려준다. `summary.cached_at` 이 기준 시각이다.

    히스토리가 짧으면 409로 끊지 않는다. 집중도·현금·금리민감도 진단은 그대로 유효해서
    신규 포트폴리오에도 절반은 답할 수 있다. 대신 `risk_score`·`risk_level`·
    `annualized_volatility`·`diversification_ratio`가 null이 되고, 왜 null인지는
    `insufficient_history`에 문장으로 담긴다.

    보유 0종목이거나 원장을 못 읽을 때만 409다 — 진단할 대상 자체가 없는 경우다.
    """
    ledger = await _ledger(user_id)
    if ledger is None:
        raise InsufficientData(
            "보유 내역을 불러오지 못해 진단할 수 없습니다.",
            detail={"reason": "ledger_unavailable"},
        )
    if not ledger.trading_days:
        raise InsufficientData("진단할 수 있는 거래일이 없습니다.")

    engine = PortfolioEngine(ledger)
    last = ledger.trading_days[-1]
    snapshot = engine.snapshot(last)
    if not snapshot.holdings:
        raise InsufficientData("보유 종목이 없어 진단할 대상이 없습니다.")

    prompt_version = prompt_version_for("portfolio.diagnosis") or "prompt_unknown"
    fingerprint = _diagnosis_fingerprint(snapshot, prompt_version)
    cache = await db.scalar(
        select(PortfolioDiagnosisCache).where(PortfolioDiagnosisCache.user_id == user_id)
    )
    if cache is not None and (not regenerate or cache.fingerprint == fingerprint):
        content = _cached_content(cache)
        if content is not None:
            envelope = Envelope[DiagnosisContent](
                content=content,
                data_as_of=DataAsOf.model_validate(cache.data_as_of),
                model=cache.model,
                cached=True,
            )
            await record(db, envelope, user_id=user_id, endpoint="portfolio.diagnosis")
            return envelope

    symbols = tuple(h.symbol for h in snapshot.holdings)
    result = assess(
        snapshot,
        ledger.prices,
        value_series=[(day, engine.snapshot(day).total_value) for day in ledger.trading_days],
        # §3.6 히스테리시스. 직전 진단의 등급을 응답 로그에서 읽어 넘긴다.
        # 처음 진단하는 사용자는 None이고, 그때는 등급이 점수 그대로 정해진다.
        previous_level=await last_risk_level(db, user_id),
        benchmark=await _benchmark(db),
        market_cap_ranks=await _market_cap_ranks(db, symbols),
    )

    client = get_llm_client()
    if isinstance(client, NullLlmClient):
        # 키가 없으면 전 항목이 같은 이유로 실패한다. 항목마다 null로 흩뿌리지 않는다.
        raise InsufficientData(
            "지금은 진단을 만들 수 없습니다.", detail={"reason": "llm_key_missing"}
        )

    indicators = _indicator_segments(result)
    ordered = list(result.findings)
    outcomes: list[SectionOutcome] = list(
        await asyncio.gather(
            generate_section(
                _SUMMARY_KEY,
                title=_SUMMARY_TITLE,
                feature=Feature.PORTFOLIO_DOCTOR_SUMMARY,
                prompt="portfolio_doctor",
                client=client,
                engine_values=indicators,
                request=_summary_request(result),
            ),
            *(
                generate_section(
                    finding.id,
                    title=_FINDING_TITLES.get(finding.id, finding.id),
                    feature=Feature.PORTFOLIO_DOCTOR_FINDING,
                    prompt="portfolio_doctor",
                    client=client,
                    engine_values=_finding_values(finding, indicators),
                    request=(
                        f"{finding.id} 항목을 작성하십시오. "
                        f"엔진 판정 심각도: {finding.severity.value}."
                    ),
                )
                for finding in ordered
            ),
        )
    )

    sections: dict[str, Any] = {}
    for outcome in outcomes:
        if outcome.section is None:
            log.warning("진단 항목 %s 생성 실패 · %s", outcome.key, "; ".join(outcome.reasons))
            continue
        sections[outcome.key] = outcome.section.model_dump(mode="json")

    sections.setdefault(_SUMMARY_KEY, _fallback_summary())
    for finding in ordered:
        sections.setdefault(finding.id, _fallback_finding(finding))

    envelope = Envelope[DiagnosisContent](
        content={
            "risk_level": result.risk_level.value if result.risk_level is not None else None,
            "risk_score": round(result.risk_score) if result.risk_score is not None else None,
            "insufficient_history": result.insufficient_history,
            "summary": sections.get(_SUMMARY_KEY),
            "findings": [
                _finding_payload(finding, sections.get(finding.id), result, list(symbols))
                for finding in ordered
            ],
            "indicators": _indicators(result),
        },
        data_as_of=DataAsOf(
            price=_as_datetime(snapshot),
            portfolio=_as_datetime(snapshot),
        ),
    )
    cache_values = {
        "fingerprint": fingerprint,
        "prompt_version": prompt_version,
        "model": envelope.model,
        "payload": envelope.content.model_dump(mode="json"),
        "data_as_of": envelope.data_as_of.model_dump(mode="json"),
        "generated_at": envelope.generated_at,
        "updated_at": now_kst(),
    }
    if cache is None:
        db.add(PortfolioDiagnosisCache(user_id=user_id, **cache_values))
    else:
        for key, value in cache_values.items():
            setattr(cache, key, value)
    await db.commit()
    # 다음 진단의 히스테리시스 기준이 되고, 피드백이 참조할 행이 된다.
    await record(db, envelope, user_id=user_id, endpoint="portfolio.diagnosis")
    return envelope


async def _cached_attribution(
    db: DbSession, user_id: str, period: str, end: date
) -> Envelope[AttributionContent] | None:
    """같은 사용자·기간·마지막 거래일로 오늘 이미 만든 분해가 있으면 그대로 돌려준다.

    시세는 장 마감 후 하루 한 번 확정되므로 마지막 거래일이 같으면 입력이 같다.
    전용 테이블 대신 응답 로그를 읽는다 — 종목 분석 캐시와 같은 방식이다.
    """
    version = prompt_version_for("portfolio.attribution")
    try:
        rows = (
            await db.scalars(
                select(AIResponse)
                .where(
                    AIResponse.user_id == user_id,
                    AIResponse.endpoint == "portfolio.attribution",
                    # 캐시 히트도 기록되므로 그 행을 다시 집으면 TTL 이 영원히 늘어난다.
                    # 실제 생성 행만 캐시 원본으로 친다.
                    AIResponse.cached.is_(False),
                    AIResponse.prompt_version == version,
                    AIResponse.created_at >= now_kst() - timedelta(hours=24),
                )
                .order_by(AIResponse.created_at.desc())
                .limit(10)
            )
        ).all()
    except Exception:  # 캐시는 최적화다. 조회 실패가 본래 분해를 막으면 안 된다.
        log.warning("성과 요인 캐시 조회 실패 · user=%s", user_id, exc_info=True)
        return None
    for row in rows:
        payload = row.payload
        content = payload.get("content") if isinstance(payload, dict) else None
        if not isinstance(content, dict) or content.get("period") != period:
            continue
        if content.get("end") != end.isoformat() or not content.get("summary"):
            continue
        cached_at = row.created_at
        if cached_at.tzinfo is None:
            cached_at = cached_at.replace(tzinfo=now_kst().tzinfo)
        content["summary"] |= {"cached": True, "cached_at": cached_at.isoformat()}
        return Envelope[AttributionContent](
            content=content,
            data_as_of=DataAsOf.model_validate(payload.get("data_as_of", {})),
            model=payload.get("model") or settings.llm_model,
            cached=True,
        )
    return None


@router.post("/attribution")
async def attribution(
    body: AttributionRequest, user_id: CurrentUser, db: DbSession, _usage: UsageLimit
) -> Envelope[AttributionContent]:
    """기간 수익률을 시장·섹터·선택으로 분해한다(§6). 본문은 `build_attribution`."""
    return await build_attribution(user_id, db, body.period)


async def build_attribution(
    user_id: str, db: AsyncSession, period: Period = Period.D1
) -> Envelope[AttributionContent]:
    """기간 수익률 분해를 조회하거나 생성한다. 배치(`ingest.briefings`)와 라우터가 같이 쓴다.

    사용자·기간·마지막 거래일로 캐시하므로 기간당 하루 한 번만 생성된다. 프런트가 쓰는
    `1d` 는 아침 배치가 미리 만들고, 다른 기간은 요청 때 만들어 그날 캐시된다.

    벤치마크는 시가총액으로 합성한 시장 전체다 — 왜 업종지수를 쓰지 않는지는
    `app.engines.attribution` 모듈 설명에 적혀 있다. 원장을 못 읽거나 구간에 거래일이
    없으면 409, 지원하지 않는 기간이면 400이다.
    """
    ledger = await _ledger(user_id)
    if ledger is None:
        raise InsufficientData(
            "보유 내역을 불러오지 못해 분해할 수 없습니다.",
            detail={"reason": "ledger_unavailable"},
        )
    if not ledger.trading_days:
        raise InsufficientData("수익률을 낼 수 있는 거래일이 없습니다.")

    rows = PortfolioEngine(ledger).daily_returns()
    if not rows:
        raise InsufficientData("수익률을 낼 수 있는 거래일이 없습니다.")

    window = [row for row in rows if row.trade_date >= _period_start(period, rows[-1].trade_date)]
    if not window:
        raise InsufficientData(f"{period.value} 구간에 거래일이 없습니다.")

    days = [row.trade_date for row in window]
    if (hit := await _cached_attribution(db, user_id, period.value, days[-1])) is not None:
        await record(db, hit, user_id=user_id, endpoint="portfolio.attribution")
        return hit
    weights = [{c.symbol: c.weight for c in row.contributions} for row in window]
    returns = [{c.symbol: c.return_rate for c in row.contributions} for row in window]
    symbols = sorted({s for w in weights for s in w})

    sectors = {
        h.symbol: h.sector or "기타" for h in PortfolioEngine(ledger).snapshot(days[-1]).holdings
    }
    sectors |= {s: (ledger.instrument(s).sector or "기타") for s in symbols if ledger.instrument(s)}
    names = {s: (ledger.instrument(s).name if ledger.instrument(s) else s) for s in symbols}

    benchmark, universe = await _market_benchmark(db, days)
    if benchmark is None:
        raise InsufficientData("시가총액·시세가 적재되지 않아 벤치마크를 만들 수 없습니다.")

    result = attribute(
        trading_days=days,
        portfolio_weights=weights,
        portfolio_returns=returns,
        benchmark=benchmark,
        sectors=sectors | universe,
        names=names,
        events=await _events(db, symbols, days[0], days[-1]),
    )

    client = get_llm_client()
    if isinstance(client, NullLlmClient):
        raise InsufficientData(
            "지금은 요약을 만들 수 없습니다.", detail={"reason": "llm_key_missing"}
        )

    outcome = await generate_section(
        _SUMMARY_KEY,
        title="성과 요인",
        feature=Feature.PERFORMANCE_ATTRIBUTION,
        prompt="attribution",
        client=client,
        engine_values=_attribution_segments(result),
        request=_attribution_request(result, period),
    )
    if outcome.section is None:
        log.warning("성과 요인 요약 생성 실패 · %s", "; ".join(outcome.reasons))
    summary = outcome.section.model_dump(mode="json") if outcome.section else None

    last = PortfolioEngine(ledger).snapshot(days[-1])
    envelope = Envelope[AttributionContent](
        content={
            "period": period.value,
            "start": days[0].isoformat(),
            "end": days[-1].isoformat(),
            "trading_days": result.trading_days,
            "portfolio_return": result.portfolio_return,
            "total_return": result.portfolio_return,
            "benchmark_return": result.benchmark_return,
            "excess_return": result.excess_return,
            "breakdown": {
                "market": result.market,
                "sector": result.sector,
                "selection": result.selection,
            },
            "contributors": [
                _contributor_payload(row) for row in result.contributors if row.contribution >= 0
            ],
            "detractors": [
                _contributor_payload(row)
                for row in reversed(result.contributors)
                if row.contribution < 0
            ],
            "sectors": [
                {
                    "sector": effect.sector,
                    "portfolio_weight": effect.portfolio_weight,
                    "benchmark_weight": effect.benchmark_weight,
                    "allocation": effect.allocation,
                    "selection": effect.selection,
                    "proxy": effect.proxy,
                }
                for effect in result.sector_effects
            ],
            "notes": list(result.notes),
            "summary": summary,
            "text": summary["text"] if summary else None,
            "segments": summary["segments"] if summary else None,
        },
        data_as_of=DataAsOf(price=_as_datetime(last), portfolio=_as_datetime(last)),
    )
    await record(db, envelope, user_id=user_id, endpoint="portfolio.attribution")
    return envelope


# ── §6 성과 요인 ──────────────────────────────────────────────────────────────
#: 기간별 되짚을 달력 일수. 거래일이 아니라 달력 기준으로 자른 뒤 거래일만 남긴다.
_PERIOD_DAYS: dict[Period, int] = {
    Period.D1: 1,
    Period.W1: 7,
    Period.M1: 31,
    Period.M3: 92,
}


def _period_start(period: Period, last: date) -> date:
    """구간 시작일. `ytd`는 올해 1월 1일이다."""
    if period is Period.YTD:
        return date(last.year, 1, 1)
    span = _PERIOD_DAYS.get(period)
    if span is None:
        raise InvalidRequest(f"지원하지 않는 기간입니다: {period.value}")
    return last - timedelta(days=span - 1)


async def _market_benchmark(
    db: DbSession, days: Sequence[date]
) -> tuple[list[BenchmarkDay] | None, dict[str, str]]:
    """시가총액으로 합성한 시장 벤치마크와 시장 전체의 종목→섹터 사전.

    섹터 비중은 Σ시가총액 ÷ 전체, 섹터 수익률은 그 시가총액 가중 수익률이다(§4.2의
    프록시 경로). 비중은 기간 내내 고정이다 — `instruments.market_cap`이 스냅샷 한
    장뿐이라 일자별 비중을 만들 방법이 없고, 만들 수 있다 해도 시장 구성비는 하루
    단위로는 거의 움직이지 않는다.
    """
    rows = (
        await db.execute(
            select(Instrument.ticker, Instrument.sector, Instrument.market_cap).where(
                Instrument.market_cap.is_not(None), Instrument.sector.is_not(None)
            )
        )
    ).all()
    if not rows:
        return None, {}

    universe = {ticker: sector for ticker, sector, _ in rows}
    caps = {ticker: float(cap) for ticker, _, cap in rows}
    total_cap = sum(caps.values())
    if total_cap <= 0:
        return None, universe

    sector_weight: dict[str, float] = defaultdict(float)
    for ticker, sector in universe.items():
        sector_weight[sector] += caps[ticker] / total_cap

    closes = await _closes(db, tuple(universe), days[0] - timedelta(days=14), days[-1])
    if not closes:
        return None, universe

    benchmark: list[BenchmarkDay] = []
    for index, day in enumerate(days):
        previous = days[index - 1] if index else None
        weighted: dict[str, float] = defaultdict(float)
        cap_in: dict[str, float] = defaultdict(float)
        for ticker, sector in universe.items():
            now = closes.get((ticker, day))
            before = _previous_close(closes, ticker, previous or day, day)
            if now is None or not before:
                continue
            weighted[sector] += caps[ticker] * (now / before - 1.0)
            cap_in[sector] += caps[ticker]
        returns = {s: weighted[s] / cap_in[s] for s in cap_in if cap_in[s] > 0}
        benchmark.append(BenchmarkDay(day, dict(sector_weight), returns))
    return benchmark, universe


async def _closes(
    db: DbSession, tickers: tuple[str, ...], start: date, end: date
) -> dict[tuple[str, date], float]:
    """구간 종가. 전일 종가가 필요하므로 시작일보다 조금 앞에서 긁는다."""
    rows = (
        await db.execute(
            select(PriceDaily.ticker, PriceDaily.trade_date, PriceDaily.close).where(
                PriceDaily.ticker.in_(tickers),
                PriceDaily.trade_date >= start,
                PriceDaily.trade_date <= end,
            )
        )
    ).all()
    return {(ticker, day): float(close) for ticker, day, close in rows if close}


def _previous_close(
    closes: dict[tuple[str, date], float], ticker: str, previous: date, day: date
) -> float | None:
    """직전 거래일 종가. 그 날 행이 없으면 최대 10일까지 거슬러 찾는다."""
    if previous != day:
        found = closes.get((ticker, previous))
        if found:
            return found
    cursor = day
    for _ in range(10):
        cursor -= timedelta(days=1)
        found = closes.get((ticker, cursor))
        if found:
            return found
    return None


async def _events(
    db: DbSession, symbols: Sequence[str], start: date, end: date
) -> list[EventRecord]:
    """구간에 걸친 보유 종목 이벤트와 매크로 이벤트. 없으면 빈 목록이다."""
    if not symbols:
        return []
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
                Event.event_date >= start,
                Event.event_date <= end,
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


def _attribution_segments(result: AttributionResult) -> dict[str, Segment]:
    """요약이 쓸 수 있는 수치. 상위 기여 종목 이름은 텍스트라 자리표시자가 아니다."""
    source = MetricSource.ATTRIBUTION_ENGINE
    values = {
        # 화면(?tab=cause)과 같은 표기 — 수익률은 %, 기여도·초과수익은 %p 두 자리 (이슈 #94).
        "portfolio_return": ratio_segment(result.portfolio_return, source, signed=True, digits=2),
        "benchmark_return": ratio_segment(result.benchmark_return, source, signed=True, digits=2),
        "excess_return": ratio_segment(
            result.excess_return, source, signed=True, digits=2, points=True
        ),
        "market": ratio_segment(result.market, source, signed=True, digits=2, points=True),
        "sector": ratio_segment(result.sector, source, signed=True, digits=2, points=True),
        "selection": ratio_segment(result.selection, source, signed=True, digits=2, points=True),
    }
    if result.contributors:
        top = result.contributors[0]
        values["top_contribution"] = ratio_segment(top.contribution, source, signed=True)
        values["top_weight"] = ratio_segment(top.weight, source)
        values["top_return"] = ratio_segment(top.return_rate, source, signed=True)
    return values


def _attribution_request(result: AttributionResult, period: Period) -> str:
    """요약이 참고할 엔진 판정. 세 축의 부호와 상위 기여 종목까지만 알려준다."""
    parts = [
        f"{period.value} 구간({result.start}~{result.end}, {result.trading_days}거래일) "
        "성과 요인 요약을 작성하십시오.",
    ]
    if result.contributors:
        top = result.contributors[0]
        parts.append(f"기여가 가장 컸던 종목: {top.name}({top.sector}).")
        if not top.held_at_start:
            parts.append("이 종목은 구간 중간에 편입되어 앞 구간 기여가 0입니다.")
        if top.events:
            titles = ", ".join(event.title for event in top.events)
            parts.append(f"같은 시점에 있었던 공시: {titles}. 인과로 단정하지 마십시오.")
        else:
            parts.append("매칭된 공시가 없으므로 원인을 지어내지 마십시오.")
    parts.extend(result.notes)
    return " ".join(parts)


def _contributor_payload(row: ContributorRow) -> dict[str, Any]:
    """§6 `contributors` 한 줄. 이벤트가 없으면 빈 배열이다."""
    return {
        "ticker": row.ticker,
        "name": row.name,
        "sector": row.sector,
        "weight": row.weight,
        "return": row.return_rate,
        "contribution": row.contribution,
        "held_at_start": row.held_at_start,
        "events": [
            {
                "citation_id": event.citation_id,
                "type": event.event_type,
                "title": event.title,
                "summary": event.title,
                "event_date": event.event_date.isoformat(),
                "matched_confidence": event.matched_confidence,
            }
            for event in row.events
        ],
    }


# ── 응답 조립 ─────────────────────────────────────────────────────────────────
def _finding_payload(
    finding: Finding,
    section: dict[str, Any] | None,
    result: RiskAssessment,
    symbols: list[str],
) -> dict[str, Any]:
    """항목 하나. 문장 생성이 실패해도 지표와 근거는 내보낸다 — 화면이 비지 않는다."""
    return {
        "id": finding.id,
        "category": finding.category.value,
        "severity": finding.severity.value,
        "title": _FINDING_TITLES.get(finding.id, finding.id),
        "text": section["text"] if section else None,
        "segments": section["segments"] if section else None,
        "evidence": _evidence(finding, result, symbols),
    }


def _indicators(result: RiskAssessment) -> dict[str, Any]:
    """§5 `indicators`. 계산되지 않은 지표는 0이 아니라 null이다."""
    return {
        "hhi": result.concentration.hhi,
        "top1_weight": result.concentration.top1,
        "top3_weight": result.concentration.top3,
        "sector_hhi": result.concentration.sector_hhi,
        "sector_count": result.concentration.sector_count,
        "annualized_volatility": (
            result.volatility.portfolio if result.volatility is not None else None
        ),
        "max_drawdown_1y": result.drawdown.mdd,
        "cash_ratio": result.cash_weight,
        "rate_sensitivity": result.rate_exposure.level.value,
        "beta": result.beta,
        "large_cap_weight": result.large_cap_weight,
        "diversification_ratio": (
            result.diversification.ratio if result.diversification is not None else None
        ),
    }


def _as_datetime(snapshot: PortfolioSnapshot) -> datetime:
    """스냅샷 기준일을 장 마감 시각으로 본다. 종가 기준이기 때문이다."""
    return datetime.combine(snapshot.trade_date, time(15, 30))
