"""활성 사용자의 데일리 브리핑을 미리 생성한다.

    python -m ingest.briefings
    python -m ingest.briefings --users user_a,user_b --date 2026-08-28
    python -m ingest.briefings --force

기본 대상은 최근 30일 안에 AI 기능을 사용한 사용자다. 백엔드가 별도 사용자 목록을
넘길 수 있는 환경에서는 ``--users``로 대상을 명시한다.

토큰은 사용자 개인 예산이 아니라 배치 전용 장부에서 나간다. 사용자가 누른 요청이
아니므로 개인 몫을 미리 깎으면 안 되고, 그렇다고 상한이 없으면 하루 중 GMS 요금이
가장 많이 나가는 경로가 무방비로 남는다.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import time
from dataclasses import dataclass
from datetime import date, timedelta

from sqlalchemy import distinct, select

from app.api.routes.briefing import BriefingContent, build_briefing
from app.core.config import settings
from app.core.db import SessionFactory, engine
from app.core.errors import RateLimited
from app.core.models import AIResponse
from app.core.schemas import Envelope, now_kst
from app.core.usage_limits import (
    BRIEFING_BATCH_USER,
    UsageGuard,
    bind_guard,
    default_guard,
    reset_usage,
    unbind_guard,
)

log = logging.getLogger("ingest.briefings")

#: 배치 장부에 남는 문맥 이름. 사용자 요청의 "briefing"과 구분한다.
_ENDPOINT = "briefing.batch"


@dataclass(frozen=True, slots=True)
class BatchResult:
    targeted: int = 0
    generated: int = 0
    cached: int = 0
    empty: int = 0
    failed: int = 0
    skipped: int = 0
    retries: int = 0
    duration_ms: int = 0
    budget_exhausted: bool = False
    failed_users: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, int | bool | list[str]]:
        return {
            "targeted": self.targeted,
            "generated": self.generated,
            "cached": self.cached,
            "empty": self.empty,
            "failed": self.failed,
            "skipped": self.skipped,
            "retries": self.retries,
            "duration_ms": self.duration_ms,
            "budget_exhausted": self.budget_exhausted,
            "failed_users": list(self.failed_users),
        }


async def active_users(*, days: int = 30) -> list[str]:
    """최근 AI 사용 이력이 있는 사용자 목록. 익명 행은 제외한다."""
    since = now_kst() - timedelta(days=days)
    async with SessionFactory() as session:
        rows = await session.scalars(
            select(distinct(AIResponse.user_id)).where(
                AIResponse.user_id.is_not(None),
                AIResponse.created_at >= since,
            )
        )
        return sorted(user_id for user_id in rows if user_id)


async def _generate(
    guard: UsageGuard, user_id: str, day: date | None, *, force: bool
) -> Envelope[BriefingContent]:
    """브리핑 하나를 배치 장부 문맥 안에서 만든다.

    문맥을 사용자마다 새로 열어야 `record`가 그 사용자의 토큰만 기록한다. 배치
    전체를 한 문맥으로 묶으면 누적값이 사용자마다 다시 저장된다.
    """
    token = await guard.enter_system(
        BRIEFING_BATCH_USER,
        _ENDPOINT,
        now=now_kst(),
        budget=settings.ai_batch_daily_token_budget,
    )
    bind_guard(guard, token)
    try:
        async with SessionFactory() as session:
            return await build_briefing(user_id, session, day, use_cache=not force)
    finally:
        unbind_guard()
        reset_usage(token)


async def run(
    users: list[str],
    *,
    day: date | None = None,
    force: bool = False,
    attempts: int | None = None,
    backoff_s: float | None = None,
) -> BatchResult:
    """사용자별 브리핑을 만들고 실패한 사용자만 다시 시도한다."""
    started = time.monotonic()
    unique_users = list(dict.fromkeys(users))
    max_attempts = max(attempts or settings.briefing_batch_attempts, 1)
    delay = settings.briefing_batch_backoff_s if backoff_s is None else max(backoff_s, 0)
    guard = default_guard()
    generated = cached = empty = failed = skipped = 0
    retries = 0
    budget_exhausted = False
    failed_users: list[str] = []
    for index, user_id in enumerate(unique_users):
        for attempt in range(1, max_attempts + 1):
            try:
                envelope = await _generate(guard, user_id, day, force=force)
                if envelope.content.status == "empty":
                    empty += 1
                elif envelope.cached:
                    cached += 1
                else:
                    generated += 1
                break
            except RateLimited as exc:
                # 배치에는 분당 횟수 한도가 걸려 있지 않으므로 이건 토큰 예산이다.
                # 예산 소진은 일시 오류가 아니다 — 재시도해도, 다음 사용자로
                # 넘어가도 같은 벽에 부딪힌다. 남은 대상을 남겨 두고 멈춘다.
                budget_exhausted = True
                log.error(
                    "배치 토큰 예산 소진 · user=%s detail=%s", user_id, exc.detail
                )
                break
            except Exception:
                if attempt == max_attempts:
                    failed += 1
                    failed_users.append(user_id)
                    log.exception(
                        "사용자 %s 브리핑 생성 최종 실패 · attempts=%d",
                        user_id,
                        max_attempts,
                    )
                    break
                retries += 1
                wait = delay * (2 ** (attempt - 1))
                log.warning(
                    "사용자 %s 브리핑 재시도 · attempt=%d/%d wait=%.1fs",
                    user_id,
                    attempt + 1,
                    max_attempts,
                    wait,
                )
                await asyncio.sleep(wait)
        if budget_exhausted:
            # 방금 그 사용자도 결과를 남기지 못했으므로 건너뛴 쪽에 함께 센다.
            skipped = len(unique_users) - index
            break
    result = BatchResult(
        targeted=len(unique_users),
        generated=generated,
        cached=cached,
        empty=empty,
        failed=failed,
        skipped=skipped,
        retries=retries,
        duration_ms=round((time.monotonic() - started) * 1000),
        budget_exhausted=budget_exhausted,
        failed_users=tuple(failed_users),
    )
    log.info("briefing_batch_metrics %s", json.dumps(result.as_dict(), ensure_ascii=False))
    return result


def _parse_date(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("날짜는 YYYY-MM-DD 형식이어야 합니다") from exc


async def _main(args: argparse.Namespace) -> int:
    users = (
        [item.strip() for item in args.users.split(",") if item.strip()]
        if args.users
        else await active_users(days=args.active_days)
    )
    result = await run(users, day=args.date, force=args.force)
    print(json.dumps(result.as_dict(), ensure_ascii=False))
    await engine.dispose()
    # 예산 소진은 대상을 남기고 끝난 것이다. 0을 돌려주면 스케줄러가 성공으로 본다.
    return 1 if result.failed or result.budget_exhausted else 0


def main() -> int:
    parser = argparse.ArgumentParser(description="활성 사용자 데일리 브리핑 선생성")
    parser.add_argument("--users", help="쉼표로 구분한 사용자 ID. 생략 시 최근 활성 사용자")
    parser.add_argument("--active-days", type=int, default=30, help="활성 사용자 조회 기간")
    parser.add_argument("--date", type=_parse_date, help="기준일(YYYY-MM-DD)")
    parser.add_argument("--force", action="store_true", help="기존 일일 결과를 무시하고 재생성")
    return asyncio.run(_main(parser.parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
