"""데일리 브리핑 배치 실행 계약."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from ingest import briefings


class SessionContext:
    async def __aenter__(self) -> object:
        return object()

    async def __aexit__(self, *_: Any) -> None:
        return None


@pytest.mark.anyio
async def test_배치는_사용자별_결과를_집계하고_중복_ID를_한번만_처리한다(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, bool]] = []

    async def _build(user_id: str, _db: Any, _day: Any, *, use_cache: bool):
        calls.append((user_id, use_cache))
        return SimpleNamespace(
            cached=user_id == "cached",
            content=SimpleNamespace(status="empty" if user_id == "empty" else "ready"),
        )

    monkeypatch.setattr(briefings, "SessionFactory", SessionContext)
    monkeypatch.setattr(briefings, "build_briefing", _build)

    result = await briefings.run(["new", "cached", "empty", "new"])

    assert result.generated == 1
    assert result.cached == 1
    assert result.empty == 1
    assert result.failed == 0
    assert result.targeted == 3
    assert result.retries == 0
    assert result.failed_users == ()
    assert calls == [("new", True), ("cached", True), ("empty", True)]


@pytest.mark.anyio
async def test_force는_저장된_결과를_무시한다(monkeypatch: pytest.MonkeyPatch) -> None:
    use_cache_values: list[bool] = []

    async def _build(_user_id: str, _db: Any, _day: Any, *, use_cache: bool):
        use_cache_values.append(use_cache)
        return SimpleNamespace(cached=False, content=SimpleNamespace(status="ready"))

    monkeypatch.setattr(briefings, "SessionFactory", SessionContext)
    monkeypatch.setattr(briefings, "build_briefing", _build)

    await briefings.run(["user"], force=True)

    assert use_cache_values == [False]


@pytest.mark.anyio
async def test_실패한_사용자만_재시도하고_운영_지표를_남긴다(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: dict[str, int] = {"ok": 0, "recover": 0, "fail": 0}

    async def _build(user_id: str, _db: Any, _day: Any, *, use_cache: bool):
        calls[user_id] += 1
        if user_id == "fail" or (user_id == "recover" and calls[user_id] == 1):
            raise RuntimeError("temporary")
        return SimpleNamespace(cached=False, content=SimpleNamespace(status="ready"))

    async def _no_wait(_: float) -> None:
        return None

    monkeypatch.setattr(briefings, "SessionFactory", SessionContext)
    monkeypatch.setattr(briefings, "build_briefing", _build)
    monkeypatch.setattr(briefings.asyncio, "sleep", _no_wait)

    result = await briefings.run(["ok", "recover", "fail"], attempts=3, backoff_s=1)

    assert calls == {"ok": 1, "recover": 2, "fail": 3}
    assert result.generated == 2
    assert result.failed == 1
    assert result.retries == 3
    assert result.failed_users == ("fail",)
    assert result.as_dict()["targeted"] == 3
