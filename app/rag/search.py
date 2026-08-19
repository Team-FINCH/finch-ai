"""공시 조각 검색과 임베딩 백필.

    python -m app.rag.search --backfill          비어 있는 조각을 채운다
    python -m app.rag.search --query "HBM 투자"   확인용 검색

검색은 pgvector 코사인 거리를 쓴다. 임베딩이 없는 조각은 애초에 후보에서 빠지므로
백필이 덜 끝난 상태에서도 있는 것만으로 동작한다.
"""

from __future__ import annotations

import argparse
import asyncio
import logging

from sqlalchemy import func, select

from app.core.db import SessionFactory, engine
from app.core.models import Document, DocumentChunk
from app.rag.embedding import NullEmbedder, get_embedder

log = logging.getLogger("app.rag.search")

BACKFILL_CHUNK = 256


async def backfill(limit: int | None = None) -> tuple[int, int]:
    """embedding이 NULL인 조각을 채운다. (시도, 성공)을 돌려준다.

    증분이다. 중단해도 다시 실행하면 남은 것부터 이어서 한다.
    """
    embedder = get_embedder()
    if isinstance(embedder, NullEmbedder):
        log.error("임베딩 제공자가 없다. OPENAI_API_KEY를 설정하라")
        return 0, 0

    tried = done = 0
    async with SessionFactory() as session:
        while True:
            take = BACKFILL_CHUNK if limit is None else min(BACKFILL_CHUNK, limit - tried)
            if take <= 0:
                break
            rows = (
                await session.scalars(
                    select(DocumentChunk)
                    .where(DocumentChunk.embedding.is_(None))
                    .order_by(DocumentChunk.id)
                    .limit(take)
                )
            ).all()
            if not rows:
                break

            vectors = await asyncio.to_thread(embedder.embed, [r.text for r in rows])
            for row, vec in zip(rows, vectors, strict=True):
                tried += 1
                if vec is not None:
                    row.embedding = vec
                    done += 1
            await session.commit()
            log.info("백필 %d건 시도 · %d건 성공", tried, done)

    return tried, done


async def search(query: str, *, top_k: int = 5, ticker: str | None = None) -> list[dict]:
    """질의와 가까운 조각을 돌려준다.

    코사인 거리(`<=>`)를 쓴다. 조각과 함께 원문 제목·공시일을 붙여, 호출부가
    근거를 구성할 때 문서를 다시 조회하지 않아도 되게 한다.
    """
    embedder = get_embedder()
    if isinstance(embedder, NullEmbedder):
        log.error("임베딩 제공자가 없다. 검색을 수행할 수 없다")
        return []

    vec = (await asyncio.to_thread(embedder.embed, [query]))[0]
    if vec is None:
        log.error("질의 임베딩에 실패했다")
        return []

    distance = DocumentChunk.embedding.cosine_distance(vec)
    stmt = (
        select(
            DocumentChunk.text,
            Document.ticker,
            Document.title,
            Document.published_at,
            distance.label("distance"),
        )
        .join(Document, Document.id == DocumentChunk.document_id)
        .where(DocumentChunk.embedding.is_not(None))
        .order_by(distance)
        .limit(top_k)
    )
    if ticker:
        stmt = stmt.where(Document.ticker == ticker)

    async with SessionFactory() as session:
        rows = (await session.execute(stmt)).all()

    return [
        {
            "text": r.text,
            "ticker": r.ticker,
            "title": r.title,
            "published_at": r.published_at,
            "similarity": round(1.0 - float(r.distance), 4),
        }
        for r in rows
    ]


async def coverage() -> tuple[int, int]:
    """(전체 조각, 임베딩 보유 조각)."""
    async with SessionFactory() as session:
        total = await session.scalar(select(func.count()).select_from(DocumentChunk))
        filled = await session.scalar(
            select(func.count()).select_from(DocumentChunk).where(DocumentChunk.embedding.is_not(None))
        )
    return int(total or 0), int(filled or 0)


async def _main() -> int:
    parser = argparse.ArgumentParser(description="공시 조각 임베딩·검색")
    parser.add_argument("--backfill", action="store_true", help="비어 있는 조각을 채운다")
    parser.add_argument("--limit", type=int, help="백필 최대 건수")
    parser.add_argument("--query", help="확인용 검색")
    parser.add_argument("--ticker", help="검색을 한 종목으로 좁힌다")
    parser.add_argument("--top-k", type=int, default=5)
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s %(name)s | %(message)s")

    try:
        total, filled = await coverage()
        log.info("조각 %d개 중 %d개에 임베딩이 있다", total, filled)

        if args.backfill:
            tried, done = await backfill(args.limit)
            log.info("백필 완료 — 시도 %d · 성공 %d", tried, done)

        if args.query:
            hits = await search(args.query, top_k=args.top_k, ticker=args.ticker)
            if not hits:
                log.warning("결과가 없다")
            for h in hits:
                print(f"  {h['similarity']:.4f}  [{h['ticker']}] {h['title'][:34]}")
                print(f"          {h['text'][:88]}")
        return 0
    finally:
        await engine.dispose()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(_main()))
