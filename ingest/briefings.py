"""활성 사용자의 데일리 브리핑을 미리 생성한다.

    python -m ingest.briefings
    python -m ingest.briefings --users user_a,user_b --date 2026-08-28
    python -m ingest.briefings --force

기본 대상은 최근 30일 안에 AI 기능을 사용한 사용자다. 백엔드가 별도 사용자 목록을
넘길 수 있는 환경에서는 ``--users``로 대상을 명시한다.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
from dataclasses import dataclass
from datetime import date, timedelta

from sqlalchemy import distinct, select

from app.api.routes.briefing import build_briefing
from app.core.db import SessionFactory, engine
from app.core.models import AIResponse
from app.core.schemas import now_kst

log = logging.getLogger("ingest.briefings")


@dataclass(frozen=True, slots=True)
class BatchResult:
    generated: int = 0
    cached: int = 0
    empty: int = 0
    failed: int = 0


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


async def run(
    users: list[str], *, day: date | None = None, force: bool = False
) -> BatchResult:
    generated = cached = empty = failed = 0
    for user_id in dict.fromkeys(users):
        try:
            async with SessionFactory() as session:
                envelope = await build_briefing(
                    user_id, session, day, use_cache=not force
                )
            if envelope.content.status == "empty":
                empty += 1
            elif envelope.cached:
                cached += 1
            else:
                generated += 1
        except Exception:
            failed += 1
            log.exception("사용자 %s 브리핑 생성 실패", user_id)
    return BatchResult(
        generated=generated,
        cached=cached,
        empty=empty,
        failed=failed,
    )


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
    print(
        f"대상={len(set(users))} 생성={result.generated} 캐시={result.cached} "
        f"빈결과={result.empty} 실패={result.failed}"
    )
    await engine.dispose()
    return 1 if result.failed else 0


def main() -> int:
    parser = argparse.ArgumentParser(description="활성 사용자 데일리 브리핑 선생성")
    parser.add_argument("--users", help="쉼표로 구분한 사용자 ID. 생략 시 최근 활성 사용자")
    parser.add_argument("--active-days", type=int, default=30, help="활성 사용자 조회 기간")
    parser.add_argument("--date", type=_parse_date, help="기준일(YYYY-MM-DD)")
    parser.add_argument("--force", action="store_true", help="기존 일일 결과를 무시하고 재생성")
    return asyncio.run(_main(parser.parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
