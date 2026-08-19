"""응답 로그와 피드백.

`ai_feedback`이 `ai_responses`를 외래키로 참조하므로 둘은 한 덩어리다. 응답을
남기지 않으면 피드백은 영원히 "찾을 수 없습니다"만 돌려준다.
"""

from __future__ import annotations

from typing import Any

import pytest

from app.core.enums import RiskLevel
from app.core.response_log import last_risk_level, record
from app.core.schemas import Envelope


class _Session:
    def __init__(self, payload: Any = None, *, fail: bool = False) -> None:
        self.payload = payload
        self.fail = fail
        self.added: list[Any] = []
        self.commits = 0
        self.rollbacks = 0

    def add(self, obj: Any) -> None:
        if self.fail:
            raise RuntimeError("저장소가 죽었다")
        self.added.append(obj)

    async def commit(self) -> None:
        self.commits += 1

    async def rollback(self) -> None:
        self.rollbacks += 1

    async def scalar(self, _stmt: Any) -> Any:
        return self.payload


def _envelope(**kw: Any) -> Envelope[dict]:
    return Envelope[dict](content=kw or {"ok": True})


# ── 기록 ─────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_봉투를_통째로_남긴다() -> None:
    """피드백이 참조할 행이자, 다음 진단의 히스테리시스 기준이다."""
    s = _Session()
    env = _envelope(risk_level="high")
    await record(s, env, user_id="u1", endpoint="portfolio.diagnosis")

    (row,) = s.added
    assert row.request_id == env.request_id
    assert row.endpoint == "portfolio.diagnosis"
    assert row.payload["content"]["risk_level"] == "high"
    assert s.commits == 1


@pytest.mark.asyncio
async def test_기록에_실패해도_예외를_올리지_않는다() -> None:
    """응답은 이미 만들어졌다. 로그를 못 남긴 것이 답을 못 준 것이 되면 안 된다."""
    s = _Session(fail=True)
    await record(s, _envelope(), user_id="u1", endpoint="x")
    assert s.rollbacks == 1
    assert s.commits == 0


# ── 직전 등급 ────────────────────────────────────────────
@pytest.mark.asyncio
async def test_직전_등급을_읽는다() -> None:
    s = _Session({"content": {"risk_level": "moderate"}})
    assert await last_risk_level(s, "u1") is RiskLevel.MODERATE


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "payload",
    [
        None,                                   # 첫 진단
        {},                                     # 봉투가 비었다
        {"content": {}},                        # 등급이 없다
        {"content": {"risk_level": "잘못된값"}},   # 모르는 등급
        {"content": "문자열"},                    # content가 dict가 아니다
    ],
)
async def test_읽을_수_없으면_None이지_예외가_아니다(payload: Any) -> None:
    """히스테리시스는 없으면 없는 대로 간다. 진단 자체를 막아서는 안 된다."""
    assert await last_risk_level(_Session(payload), "u1") is None
