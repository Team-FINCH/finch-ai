"""포트폴리오 진단과 수익률 원인 분석. API 명세 §5 · §6.

담당 트랙: feat/engine-risk, feat/engine-attribution

`diagnosis`는 Risk Engine이 뽑은 숫자를 그대로 내보내고 문장만 LLM에 맡긴다.
`risk_level`·`risk_score`·`findings`의 존재와 순서는 전부 엔진 판정이며(산식 §3.6·§3.7)
모델은 각 항목의 서술 세 문장만 쓴다. 항목 제목은 서버 상수다 — `NARRATIVE_SCHEMA`가
title 필드를 갖고 있지 않기도 하고, 모델이 쓴 제목에는 판정이 섞여 들어온다.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, time
from functools import lru_cache
from typing import Any

from fastapi import APIRouter
from pydantic import BaseModel
from sqlalchemy import func, select

from app.api.deps import CurrentUser, DbSession
from app.core.adapters import Ledger, SeedLedgerSource
from app.core.config import settings
from app.core.enums import MetricSource, Period
from app.core.errors import InsufficientData
from app.core.models import IndexDaily, Instrument
from app.core.schemas import DataAsOf, Envelope, Segment
from app.engines.portfolio import PortfolioEngine, PortfolioSnapshot
from app.engines.risk import Finding, RiskAssessment, assess
from app.llm.client import NullLlmClient, get_llm_client
from app.llm.generate import SectionOutcome, generate_section, ratio_segment
from app.llm.guard import Feature

log = logging.getLogger("app.api.portfolio")

router = APIRouter(prefix="/portfolio", tags=["portfolio"])

#: 벤치마크는 §3.4 베타 계산용이다. `index_daily`가 KOSPI로 적재되어 있다.
_BENCHMARK_CODE = "KOSPI"

_SUMMARY_KEY = "summary"
_SUMMARY_TITLE = "종합 진단"

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


# ── 원장 ──────────────────────────────────────────────────────────────────────
@lru_cache
def _ledger_source() -> SeedLedgerSource:
    return SeedLedgerSource(settings.seed_fixture_path)


def _ledger(user_id: str) -> Ledger | None:
    """원장 스냅샷. 못 읽으면 None이다.

    백엔드 원장 읽기가 열리기 전까지는 시드 어댑터만 있다(§11). `stocks.py`는 스냅샷
    하나만 필요해서 거기서 끝나지만, Risk Engine은 `prices`와 거래일 전체를 받으므로
    여기서는 `Ledger`를 그대로 들고 나온다.
    """
    if not settings.use_seed_adapter:
        return None
    try:
        ledger = _ledger_source().load(user_id)
    except (KeyError, FileNotFoundError, OSError):
        return None
    return ledger if ledger.trading_days else None


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
        key: indicators[key]
        for key in _FINDING_VALUE_KEYS.get(finding.id, ())
        if key in indicators
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


# ── 라우터 ────────────────────────────────────────────────────────────────────
@router.post("/diagnosis")
async def diagnosis(user_id: CurrentUser, db: DbSession) -> Envelope[dict]:
    """위험 지표를 계산하고 상위 항목을 설명한다(§5).

    히스토리가 짧으면 409로 끊지 않는다. 집중도·현금·금리민감도 진단은 그대로 유효해서
    신규 포트폴리오에도 절반은 답할 수 있다. 대신 `risk_score`·`risk_level`·
    `annualized_volatility`·`diversification_ratio`가 null이 되고, 왜 null인지는
    `insufficient_history`에 문장으로 담긴다.

    보유 0종목이거나 원장을 못 읽을 때만 409다 — 진단할 대상 자체가 없는 경우다.
    """
    ledger = _ledger(user_id)
    if ledger is None:
        raise InsufficientData("원장을 읽을 수 없어 진단할 수 없습니다.")

    engine = PortfolioEngine(ledger)
    last = ledger.trading_days[-1]
    snapshot = engine.snapshot(last)
    if not snapshot.holdings:
        raise InsufficientData("보유 종목이 없어 진단할 대상이 없습니다.")

    symbols = tuple(h.symbol for h in snapshot.holdings)
    result = assess(
        snapshot,
        ledger.prices,
        value_series=[(day, engine.snapshot(day).total_value) for day in ledger.trading_days],
        # §3.6 히스테리시스는 직전 *등급*을 필요로 하는데 그것을 남기는 저장소가 아직
        # 없다. `AIResponse`가 유일한 후보지만 이 커밋 시점에 그 테이블에 행을 쓰는 코드가
        # 없어(`grep AIResponse app/` → 모델 정의뿐) 조회해도 항상 None이다. 응답 로그
        # 적재가 붙으면 여기서 마지막 진단의 `risk_level`을 읽어 넘긴다. 그때까지는 경계
        # 근처에서 등급이 한 점 차로 흔들릴 수 있다 — 빠뜨린 게 아니라 미룬 것이다.
        previous_level=None,
        benchmark=await _benchmark(db),
        market_cap_ranks=await _market_cap_ranks(db, symbols),
    )

    client = get_llm_client()
    if isinstance(client, NullLlmClient):
        # 키가 없으면 전 항목이 같은 이유로 실패한다. 항목마다 null로 흩뿌리지 않는다.
        raise InsufficientData("ANTHROPIC_API_KEY가 없어 진단을 생성할 수 없습니다.")

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

    return Envelope[dict](
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


@router.post("/attribution")
async def attribution(
    body: AttributionRequest, user_id: CurrentUser, db: DbSession
) -> Envelope[dict]:
    raise InsufficientData("Attribution Engine이 아직 연결되지 않았습니다.")


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
