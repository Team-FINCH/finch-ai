"""저장소는 진짜 Postgres에 붙여 확인한다.

JSONB·UUID·UNIQUE 제약이 전부 Postgres 것이라, 다른 엔진으로 흉내 내면 정작 깨질
자리(UNIQUE(user_id, ticker, status))가 테스트에서 안 깨진다. DB가 없으면 건너뛴다.

각 테스트는 커밋하지 않고 롤백으로 끝난다. 저장소 함수도 flush까지만 하므로
테이블에는 아무것도 남지 않는다.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.core.config import settings
from app.core.enums import Confidence, ThesisHorizon, ThesisStatus, WikiSource
from app.core.errors import InvalidRequest
from app.wiki.store import (
    add_fact,
    fact_payload,
    get_active_thesis,
    list_facts,
    list_theses,
    record_thesis,
    soft_delete_fact,
    thesis_payload,
    update_active_thesis,
)

TICKER = "005930"


@pytest.fixture
async def db() -> AsyncIterator[AsyncSession]:
    """테스트마다 엔진을 새로 만든다.

    app.core.db의 엔진은 모듈 전역이라 커넥션 풀이 처음 만들어진 이벤트 루프에 묶인다.
    pytest-asyncio는 테스트마다 루프를 새로 열기 때문에, 그 풀을 재사용하면 두 번째
    테스트부터 죽은 루프의 커넥션을 잡는다. NullPool이면 빌려둔 커넥션 자체가 없다.
    """
    engine = create_async_engine(settings.database_url, poolclass=NullPool)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    session = factory()
    try:
        await session.execute(text("select 1"))
    except Exception:  # noqa: BLE001 - 드라이버가 뭘 던지든 결론은 같다
        await session.close()
        await engine.dispose()
        pytest.skip("Postgres가 없으면 저장소 테스트는 건너뛴다.")
    try:
        yield session
    finally:
        await session.rollback()
        await session.close()
        await engine.dispose()


@pytest.fixture
def user_id() -> str:
    """테스트끼리 섞이지 않게 매번 새 사용자를 쓴다."""
    return f"test-{uuid.uuid4().hex[:12]}"


# ── facts ─────────────────────────────────────────────────────────────
async def test_소프트_삭제된_항목은_읽기에서_빠진다(db: AsyncSession, user_id: str) -> None:
    keep = await add_fact(db, user_id, "장기 보유 선호", source=WikiSource.USER_STATED)
    drop = await add_fact(db, user_id, "배당주 관심", source=WikiSource.USER_STATED)

    await soft_delete_fact(db, user_id, str(drop.id))

    assert [f.id for f in await list_facts(db, user_id)] == [keep.id]
    assert drop.deleted_at is not None  # 행은 남아 있다


async def test_추론_항목은_source를_잃지_않는다(db: AsyncSession, user_id: str) -> None:
    """ai_inferred가 지워지면 화면이 추측을 사실처럼 렌더링한다."""
    await add_fact(
        db,
        user_id,
        "반도체 섹터를 선호하시는 것 같습니다",
        source=WikiSource.AI_INFERRED,
        confidence=Confidence.LOW,
    )

    (fact,) = await list_facts(db, user_id)

    assert fact.source == WikiSource.AI_INFERRED
    assert fact_payload(fact)["source"] == WikiSource.AI_INFERRED


async def test_남의_항목은_지울_수_없다(db: AsyncSession, user_id: str) -> None:
    mine = await add_fact(db, user_id, "내 항목", source=WikiSource.USER_STATED)

    with pytest.raises(InvalidRequest):
        await soft_delete_fact(db, "다른-사용자", str(mine.id))


async def test_이미_지운_항목을_또_지우면_거절한다(db: AsyncSession, user_id: str) -> None:
    fact = await add_fact(db, user_id, "한 번만", source=WikiSource.USER_STATED)
    await soft_delete_fact(db, user_id, str(fact.id))

    with pytest.raises(InvalidRequest):
        await soft_delete_fact(db, user_id, str(fact.id))


async def test_editable이_아니면_지울_수_없다(db: AsyncSession, user_id: str) -> None:
    fact = await add_fact(
        db, user_id, "시스템 고정 항목", source=WikiSource.DERIVED_FROM_TRADES, editable=False
    )

    with pytest.raises(InvalidRequest):
        await soft_delete_fact(db, user_id, str(fact.id))


async def test_없는_id는_형식이_틀려도_같은_에러다(db: AsyncSession, user_id: str) -> None:
    with pytest.raises(InvalidRequest):
        await soft_delete_fact(db, user_id, "uuid가-아님")
    with pytest.raises(InvalidRequest):
        await soft_delete_fact(db, user_id, str(uuid.uuid4()))


# ── theses ────────────────────────────────────────────────────────────
async def test_같은_종목_두_번째_논지는_이전_것을_닫는다(db: AsyncSession, user_id: str) -> None:
    first = await record_thesis(db, user_id, TICKER, "HBM 수요를 보고 샀다")

    second = await record_thesis(db, user_id, TICKER, "파운드리 회복을 본다")

    assert [t.id for t in await list_theses(db, user_id)] == [second.id]
    closed = await list_theses(db, user_id, status=ThesisStatus.CLOSED)
    assert [t.id for t in closed] == [first.id]


async def test_세_번째_논지도_UNIQUE에_걸리지_않는다(db: AsyncSession, user_id: str) -> None:
    """UNIQUE(user_id, ticker, status)는 닫힌 논지도 한 건까지다.

    이전 닫힌 논지를 먼저 치우지 않으면 세 번째 기록에서 제약 위반이 난다.
    """
    await record_thesis(db, user_id, TICKER, "첫 번째")
    await record_thesis(db, user_id, TICKER, "두 번째")

    third = await record_thesis(db, user_id, TICKER, "세 번째")

    assert [t.text for t in await list_theses(db, user_id)] == ["세 번째"]
    closed = await list_theses(db, user_id, status=ThesisStatus.CLOSED)
    assert [t.text for t in closed] == ["두 번째"]
    assert third.status == ThesisStatus.ACTIVE


async def test_다른_종목_논지는_서로_닫지_않는다(db: AsyncSession, user_id: str) -> None:
    await record_thesis(db, user_id, TICKER, "삼성전자 논지")
    await record_thesis(db, user_id, "000660", "하이닉스 논지")

    assert len(await list_theses(db, user_id)) == 2


async def test_논지_수정은_새_행을_만들지_않는다(db: AsyncSession, user_id: str) -> None:
    original = await record_thesis(
        db, user_id, TICKER, "오타 있는 논지", horizon=ThesisHorizon.LONG
    )

    updated = await update_active_thesis(db, user_id, TICKER, "고친 논지")

    assert updated.id == original.id
    assert updated.horizon == ThesisHorizon.LONG  # 안 준 값은 유지된다
    assert await list_theses(db, user_id, status=ThesisStatus.CLOSED) == []


async def test_사용자가_고친_논지는_추론이_아니게_된다(db: AsyncSession, user_id: str) -> None:
    await record_thesis(
        db, user_id, TICKER, "이런 이유로 사신 듯합니다", source=WikiSource.AI_INFERRED
    )

    updated = await update_active_thesis(db, user_id, TICKER, "실은 배당 때문이다")

    assert updated.source == WikiSource.USER_STATED


async def test_활성_논지가_없으면_수정을_거절한다(db: AsyncSession, user_id: str) -> None:
    with pytest.raises(InvalidRequest):
        await update_active_thesis(db, user_id, TICKER, "없는 논지")


async def test_논지_직렬화는_source와_status를_싣는다(db: AsyncSession, user_id: str) -> None:
    thesis = await record_thesis(
        db,
        user_id,
        TICKER,
        "장기 보유",
        horizon=ThesisHorizon.LONG,
        linked_trade_id="2025-09-01#0000",
    )

    payload = thesis_payload(thesis)

    assert payload["source"] == WikiSource.USER_STATED
    assert payload["status"] == ThesisStatus.ACTIVE
    assert payload["linked_trade_id"] == "2025-09-01#0000"
    assert payload["ticker"] == TICKER


async def test_활성_논지_조회는_닫힌_것을_주지_않는다(db: AsyncSession, user_id: str) -> None:
    await record_thesis(db, user_id, TICKER, "옛 논지")
    await record_thesis(db, user_id, TICKER, "새 논지")

    active = await get_active_thesis(db, user_id, TICKER)

    assert active is not None
    assert active.text == "새 논지"


async def test_위키는_사용자별로_격리된다(db: AsyncSession, user_id: str) -> None:
    await add_fact(db, user_id, "내 성향", source=WikiSource.USER_STATED)
    await record_thesis(db, user_id, TICKER, "내 논지")

    other = f"test-{uuid.uuid4().hex[:12]}"

    assert await list_facts(db, other) == []
    assert await list_theses(db, other) == []
