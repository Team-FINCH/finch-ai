"""Shared default target selection and coverage reporting for batch ingestion."""

import logging
from collections.abc import Sequence

from app.core.config import settings


def target_tickers(explicit: Sequence[str] | None = None, limit: int | None = None) -> list[str]:
    """Explicit tickers bypass the universe and limit; otherwise cap config order."""
    if explicit:
        return list(explicit)
    if limit is not None and limit <= 0:
        raise ValueError("limit must be positive or omitted")
    tickers = list(settings.service_tickers)
    return tickers[:limit] if limit is not None else tickers


def report_resolution(
    logger: logging.Logger,
    requested: Sequence[str],
    resolved: Sequence[str],
    *,
    explicit: bool = False,
) -> None:
    """Warn on partial coverage; an empty default run is an actionable failure.

    Never invent missing instruments or substitute stocks outside the requested set.
    Explicit selections retain their existing empty-result behavior.
    """
    missing = sorted(set(requested) - set(resolved))
    emit = logger.warning if missing or not resolved else logger.info
    emit(
        "Target coverage: requested=%d resolved=%d missing=%s (absent or ineligible instruments)",
        len(requested),
        len(resolved),
        ",".join(missing) or "none",
    )
    if not explicit and not resolved:
        raise ValueError(
            "No service universe targets resolved; check instruments and SERVICE_TICKERS"
        )
