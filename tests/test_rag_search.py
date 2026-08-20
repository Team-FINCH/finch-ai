"""백필 루프 회귀 테스트.

`backfill()`은 embedding이 NULL인 행을 집어 채운다. 임베딩이 실패하면 그 행은
NULL로 남고, 다음 회차가 같은 행을 다시 집는다. 끊는 조건이 없으면 영원히 돈다 —
실제로 2,280건짜리 백필이 20,000건 시도를 찍으며 API 일당 요청 한도를 다 태웠다.
"""

from __future__ import annotations

from typing import Any

import pytest

from app.rag import search as mod
from app.rag.embedding import Embedder


class _Chunk:
    def __init__(self, text: str) -> None:
        self.text = text
        self.embedding: list[float] | None = None


class _Scalars:
    def __init__(self, rows: list[_Chunk]) -> None:
        self._rows = rows

    def all(self) -> list[_Chunk]:
        return self._rows


class _Session:
    """embedding이 NULL인 행만 돌려주는 최소 세션.

    진짜 DB와 같은 성질 하나만 흉내 낸다 — 채워지지 않은 행은 다음 조회에도
    그대로 나온다. 무한 루프는 정확히 이 성질에서 생긴다.
    """

    def __init__(self, rows: list[_Chunk]) -> None:
        self.rows = rows
        self.queries = 0

    async def __aenter__(self) -> _Session:
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None

    async def scalars(self, _stmt: Any) -> _Scalars:
        self.queries += 1
        # 상한이 없으면 회귀 시 테스트가 실패하는 대신 영원히 멈춰 선다.
        if self.queries > 5:
            raise AssertionError("같은 행을 계속 다시 집는다 — 무한 루프")
        return _Scalars([r for r in self.rows if r.embedding is None])

    async def commit(self) -> None:
        return None


class _AlwaysFails(Embedder):
    def embed(self, texts: list[str]) -> list[list[float] | None]:
        return [None] * len(texts)


class _AlwaysWorks(Embedder):
    def embed(self, texts: list[str]) -> list[list[float] | None]:
        return [[0.1] * 4 for _ in texts]


def _patch(monkeypatch: pytest.MonkeyPatch, session: _Session, embedder: Embedder) -> None:
    monkeypatch.setattr(mod, "SessionFactory", lambda: session)
    monkeypatch.setattr(mod, "get_embedder", lambda: embedder)


@pytest.mark.asyncio
async def test_한_건도_못_채우면_중단한다(monkeypatch: pytest.MonkeyPatch) -> None:
    session = _Session([_Chunk("가"), _Chunk("나")])
    _patch(monkeypatch, session, _AlwaysFails())

    tried, done = await mod.backfill()

    assert done == 0
    assert tried == 2, "같은 행을 두 번 이상 집으면 무한 루프다"
    assert session.queries == 1


@pytest.mark.asyncio
async def test_채운_만큼_줄어들고_정상_종료한다(monkeypatch: pytest.MonkeyPatch) -> None:
    session = _Session([_Chunk("가"), _Chunk("나")])
    _patch(monkeypatch, session, _AlwaysWorks())

    tried, done = await mod.backfill()

    assert (tried, done) == (2, 2)
    assert all(r.embedding is not None for r in session.rows)
