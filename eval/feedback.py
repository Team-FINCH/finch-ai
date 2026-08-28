"""사용자 피드백을 프롬프트 버전별 품질 신호로 집계한다."""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any

from sqlalchemy import select

from app.core.db import SessionFactory
from app.core.models import AIFeedback, AIResponse
from app.core.schemas import now_kst


@dataclass(frozen=True, slots=True)
class FeedbackObservation:
    endpoint: str
    prompt_version: str | None
    rating: str | None
    reasons: tuple[str, ...] = ()
    input_tokens: int = 0
    output_tokens: int = 0
    latency_ms: int | None = None


@dataclass(slots=True)
class VersionStats:
    endpoint: str
    prompt_version: str
    responses: int = 0
    feedback: int = 0
    up: int = 0
    down: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    latency_total_ms: int = 0
    latency_samples: int = 0
    reasons: Counter[str] = field(default_factory=Counter)

    @property
    def feedback_rate(self) -> float:
        return self.feedback / self.responses if self.responses else 0.0

    @property
    def approval_rate(self) -> float | None:
        return self.up / self.feedback if self.feedback else None

    @property
    def average_latency_ms(self) -> float | None:
        return self.latency_total_ms / self.latency_samples if self.latency_samples else None


@dataclass(frozen=True, slots=True)
class VersionComparison:
    endpoint: str
    baseline: str
    candidate: str
    approval_delta: float | None
    feedback_rate_delta: float
    wrong_number_rate_delta: float | None


def aggregate(observations: Iterable[FeedbackObservation]) -> list[VersionStats]:
    grouped: dict[tuple[str, str], VersionStats] = {}
    for row in observations:
        version = row.prompt_version or "unversioned"
        key = (row.endpoint, version)
        stats = grouped.setdefault(key, VersionStats(row.endpoint, version))
        stats.responses += 1
        stats.input_tokens += row.input_tokens
        stats.output_tokens += row.output_tokens
        if row.latency_ms is not None:
            stats.latency_total_ms += row.latency_ms
            stats.latency_samples += 1
        if row.rating is None:
            continue
        stats.feedback += 1
        if row.rating == "up":
            stats.up += 1
        elif row.rating == "down":
            stats.down += 1
        stats.reasons.update(row.reasons)
    return sorted(grouped.values(), key=lambda item: (item.endpoint, item.prompt_version))


def compare(
    baseline: VersionStats, candidate: VersionStats
) -> VersionComparison:
    def _reason_rate(stats: VersionStats, reason: str) -> float | None:
        return stats.reasons[reason] / stats.feedback if stats.feedback else None

    baseline_wrong = _reason_rate(baseline, "wrong_number")
    candidate_wrong = _reason_rate(candidate, "wrong_number")
    return VersionComparison(
        endpoint=candidate.endpoint,
        baseline=baseline.prompt_version,
        candidate=candidate.prompt_version,
        approval_delta=(
            candidate.approval_rate - baseline.approval_rate
            if candidate.approval_rate is not None and baseline.approval_rate is not None
            else None
        ),
        feedback_rate_delta=candidate.feedback_rate - baseline.feedback_rate,
        wrong_number_rate_delta=(
            candidate_wrong - baseline_wrong
            if candidate_wrong is not None and baseline_wrong is not None
            else None
        ),
    )


async def load(days: int = 30) -> list[FeedbackObservation]:
    since = now_kst() - timedelta(days=days)
    stmt = (
        select(AIResponse, AIFeedback)
        .outerjoin(AIFeedback, AIFeedback.request_id == AIResponse.request_id)
        .where(AIResponse.created_at >= since)
        .order_by(AIResponse.created_at)
    )
    async with SessionFactory() as session:
        rows = (await session.execute(stmt)).all()
    return [
        FeedbackObservation(
            endpoint=response.endpoint,
            prompt_version=response.prompt_version,
            rating=feedback.rating if feedback else None,
            reasons=tuple(feedback.reasons or ()) if feedback else (),
            input_tokens=int(response.input_tokens or 0),
            output_tokens=int(response.output_tokens or 0),
            latency_ms=response.latency_ms,
        )
        for response, feedback in rows
    ]


def render(stats: list[VersionStats]) -> str:
    lines = [
        "endpoint | prompt_version | responses | feedback | approval | avg_latency_ms | reasons",
        "--- | --- | ---: | ---: | ---: | ---: | ---",
    ]
    for item in stats:
        approval = "-" if item.approval_rate is None else f"{item.approval_rate:.1%}"
        latency = "-" if item.average_latency_ms is None else f"{item.average_latency_ms:.0f}"
        reasons = ", ".join(f"{key}:{value}" for key, value in item.reasons.most_common()) or "-"
        lines.append(
            f"{item.endpoint} | {item.prompt_version} | {item.responses} | "
            f"{item.feedback} | {approval} | {latency} | {reasons}"
        )
    return "\n".join(lines)


def comparisons(
    stats: list[VersionStats], baseline: str, candidate: str
) -> list[VersionComparison]:
    indexed = {(item.endpoint, item.prompt_version): item for item in stats}
    endpoints = sorted(
        endpoint
        for endpoint, version in indexed
        if version == candidate and (endpoint, baseline) in indexed
    )
    return [
        compare(indexed[(endpoint, baseline)], indexed[(endpoint, candidate)])
        for endpoint in endpoints
    ]


def render_comparisons(items: list[VersionComparison]) -> str:
    lines = [
        "endpoint | baseline | candidate | approval_delta | feedback_rate_delta | wrong_number_delta",
        "--- | --- | --- | ---: | ---: | ---:",
    ]

    def _rate(value: float | None) -> str:
        return "-" if value is None else f"{value:+.1%}"

    for item in items:
        lines.append(
            f"{item.endpoint} | {item.baseline} | {item.candidate} | "
            f"{_rate(item.approval_delta)} | {_rate(item.feedback_rate_delta)} | "
            f"{_rate(item.wrong_number_rate_delta)}"
        )
    return "\n".join(lines)


def as_dict(stats: VersionStats) -> dict[str, Any]:
    return {
        "endpoint": stats.endpoint,
        "prompt_version": stats.prompt_version,
        "responses": stats.responses,
        "feedback": stats.feedback,
        "feedback_rate": stats.feedback_rate,
        "approval_rate": stats.approval_rate,
        "average_latency_ms": stats.average_latency_ms,
        "input_tokens": stats.input_tokens,
        "output_tokens": stats.output_tokens,
        "reasons": dict(stats.reasons),
    }
