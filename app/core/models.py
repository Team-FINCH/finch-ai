"""DB 스키마 전체.

트랙별로 테이블을 나눠 추가하면 마이그레이션이 충돌한다. 병렬 작업에 들어가기 전에
여기서 한 번에 정의한다.

포트폴리오 원장은 백엔드가 소유하며 이 저장소에 두지 않는다. 우리는 읽기만 한다.
API 명세 §11 참조.
"""

from __future__ import annotations

import uuid
from datetime import date as Date
from datetime import datetime

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    ARRAY,
    BigInteger,
    Boolean,
    CheckConstraint,
    Date as SADate,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID as PGUUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

from app.core.config import settings


class Base(DeclarativeBase):
    pass


def _uuid_pk() -> Mapped[uuid.UUID]:
    return mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid.uuid4)


TS = DateTime(timezone=True)


# ── 종목 마스터 ──────────────────────────────────────────
class Instrument(Base):
    """종목 마스터. AI 파트가 pykrx + DART corpCode.xml로 자체 구축한다.

    corp_code는 DART 고유번호로 종목코드와 다르다. 공시 검색에 반드시 필요하다.
    """

    __tablename__ = "instruments"

    ticker: Mapped[str] = mapped_column(String(6), primary_key=True)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    market: Mapped[str] = mapped_column(String(10), nullable=False)
    sector: Mapped[str | None] = mapped_column(String(60))
    sector_code: Mapped[str | None] = mapped_column(String(20))
    corp_code: Mapped[str | None] = mapped_column(String(8))
    market_cap: Mapped[int | None] = mapped_column(BigInteger)
    listed_shares: Mapped[int | None] = mapped_column(BigInteger)
    status: Mapped[str] = mapped_column(String(12), nullable=False, default="listed")
    updated_at: Mapped[datetime] = mapped_column(
        TS, server_default=func.now(), onupdate=func.now()
    )

    __table_args__ = (
        Index("ix_instruments_corp_code", "corp_code"),
        Index("ix_instruments_sector", "sector"),
        CheckConstraint("char_length(ticker) = 6", name="ck_instruments_ticker_len"),
    )


# ── 일별 시세 ────────────────────────────────────────────
class PriceDaily(Base):
    """수정주가 기준 일별 시세.

    변동성·상관관계 계산의 기반이며 최소 60거래일이 필요하다.
    거래정지 구간은 행을 넣지 않는다 — 0 수익률이 들어가면 변동성이 과소 추정된다.
    """

    __tablename__ = "price_daily"

    ticker: Mapped[str] = mapped_column(
        String(6), ForeignKey("instruments.ticker", ondelete="CASCADE"), primary_key=True
    )
    trade_date: Mapped[Date] = mapped_column(SADate, primary_key=True)
    open: Mapped[int | None] = mapped_column(Integer)
    high: Mapped[int | None] = mapped_column(Integer)
    low: Mapped[int | None] = mapped_column(Integer)
    close: Mapped[int] = mapped_column(Integer, nullable=False)
    volume: Mapped[int | None] = mapped_column(BigInteger)
    trade_value: Mapped[int | None] = mapped_column(BigInteger)

    __table_args__ = (Index("ix_price_daily_date", "trade_date"),)


class IndexDaily(Base):
    """지수 일별 시세. 시장 기여도 분해와 베타 계산에 쓴다.

    index_code 예: KOSPI, KOSDAQ, 그리고 KRX 업종지수.
    """

    __tablename__ = "index_daily"

    index_code: Mapped[str] = mapped_column(String(20), primary_key=True)
    trade_date: Mapped[Date] = mapped_column(SADate, primary_key=True)
    close: Mapped[float] = mapped_column(Float, nullable=False)


# ── 문서 · 임베딩 ────────────────────────────────────────
class Document(Base):
    """공시·뉴스 원문. RAG의 근거 원천이다."""

    __tablename__ = "documents"

    id: Mapped[uuid.UUID] = _uuid_pk()
    doc_type: Mapped[str] = mapped_column(String(16), nullable=False)
    source: Mapped[str] = mapped_column(String(40), nullable=False)
    external_id: Mapped[str] = mapped_column(String(120), nullable=False)
    ticker: Mapped[str | None] = mapped_column(String(6), ForeignKey("instruments.ticker"))
    title: Mapped[str] = mapped_column(Text, nullable=False)
    body: Mapped[str | None] = mapped_column(Text)
    url: Mapped[str | None] = mapped_column(Text)
    publisher: Mapped[str | None] = mapped_column(String(100))
    published_at: Mapped[datetime | None] = mapped_column(TS)
    fetched_at: Mapped[datetime] = mapped_column(TS, server_default=func.now())

    chunks: Mapped[list[DocumentChunk]] = relationship(
        back_populates="document", cascade="all, delete-orphan"
    )

    __table_args__ = (
        UniqueConstraint("source", "external_id", name="uq_documents_source_external"),
        Index("ix_documents_ticker_published", "ticker", "published_at"),
    )


class DocumentChunk(Base):
    """임베딩 단위 조각.

    벡터 차원은 DDL 시점에 고정된다. 임베딩 모델을 바꾸면 마이그레이션이 필요하다.
    """

    __tablename__ = "document_chunks"

    id: Mapped[uuid.UUID] = _uuid_pk()
    document_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("documents.id", ondelete="CASCADE"), nullable=False
    )
    chunk_index: Mapped[int] = mapped_column(Integer, nullable=False)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    embedding: Mapped[list[float] | None] = mapped_column(Vector(settings.embedding_dim))

    document: Mapped[Document] = relationship(back_populates="chunks")

    __table_args__ = (
        UniqueConstraint("document_id", "chunk_index", name="uq_chunk_doc_index"),
    )


# ── 이벤트 ───────────────────────────────────────────────
class Event(Base):
    """실적·공시·거시 일정과 중요도.

    importance는 브리핑 랭킹 산식의 입력이다. 엔진 산식 §5.2 참조.
    """

    __tablename__ = "events"

    id: Mapped[uuid.UUID] = _uuid_pk()
    event_type: Mapped[str] = mapped_column(String(20), nullable=False)
    ticker: Mapped[str | None] = mapped_column(String(6), ForeignKey("instruments.ticker"))
    title: Mapped[str] = mapped_column(Text, nullable=False)
    event_date: Mapped[Date] = mapped_column(SADate, nullable=False)
    confirmed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    importance: Mapped[float] = mapped_column(Float, nullable=False, default=0.5)
    document_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("documents.id", ondelete="SET NULL")
    )
    created_at: Mapped[datetime] = mapped_column(TS, server_default=func.now())

    __table_args__ = (Index("ix_events_date_ticker", "event_date", "ticker"),)


# ── Wiki ─────────────────────────────────────────────────
class WikiFact(Base):
    """사용자 성향·목표. 수치는 절대 저장하지 않는다 — 즉시 낡기 때문이다."""

    __tablename__ = "wiki_facts"

    id: Mapped[uuid.UUID] = _uuid_pk()
    user_id: Mapped[str] = mapped_column(String(40), nullable=False)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    source: Mapped[str] = mapped_column(String(24), nullable=False)
    confidence: Mapped[str] = mapped_column(String(10), nullable=False, default="medium")
    as_of: Mapped[datetime] = mapped_column(TS, server_default=func.now())
    evidence: Mapped[dict | None] = mapped_column(JSONB)
    editable: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    deleted_at: Mapped[datetime | None] = mapped_column(TS)

    __table_args__ = (Index("ix_wiki_facts_user", "user_id", "deleted_at"),)


class WikiThesis(Base):
    """매수 시점의 투자 논지. Thesis Check의 기준선이 된다.

    수집은 AI가 대화에서 직접 묻는다. 별도 입력 UI를 다른 파트에 요청하지 않는다.
    """

    __tablename__ = "wiki_theses"

    id: Mapped[uuid.UUID] = _uuid_pk()
    user_id: Mapped[str] = mapped_column(String(40), nullable=False)
    ticker: Mapped[str] = mapped_column(String(6), nullable=False)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    horizon: Mapped[str | None] = mapped_column(String(10))
    source: Mapped[str] = mapped_column(String(24), nullable=False, default="user_stated")
    status: Mapped[str] = mapped_column(String(10), nullable=False, default="active")
    linked_trade_id: Mapped[str | None] = mapped_column(String(40))
    recorded_at: Mapped[datetime] = mapped_column(TS, server_default=func.now())

    __table_args__ = (
        UniqueConstraint("user_id", "ticker", "status", name="uq_thesis_user_ticker_active"),
        Index("ix_wiki_theses_user", "user_id"),
    )


class TradeWatermark(Base):
    """거래 폴링 워터마크.

    백엔드에 웹훅 개발을 요청하지 않고 거래 조회 API를 주기 폴링해
    신규 매수를 판별한다. API 명세 §11 참조.
    """

    __tablename__ = "trade_watermarks"

    user_id: Mapped[str] = mapped_column(String(40), primary_key=True)
    last_trade_id: Mapped[str | None] = mapped_column(String(40))
    polled_at: Mapped[datetime] = mapped_column(
        TS, server_default=func.now(), onupdate=func.now()
    )


# ── 응답 로그 · 피드백 ───────────────────────────────────
class AIResponse(Base):
    """응답 로그. 평가와 프롬프트 버전 추적의 기반이다.

    prompt_version을 남기지 않으면 어떤 프롬프트가 어떤 답을 만들었는지 알 수 없다.
    응답 정책 §7.1 참조.
    """

    __tablename__ = "ai_responses"

    request_id: Mapped[str] = mapped_column(String(40), primary_key=True)
    user_id: Mapped[str | None] = mapped_column(String(40))
    endpoint: Mapped[str] = mapped_column(String(60), nullable=False)
    prompt_version: Mapped[str | None] = mapped_column(String(40))
    model: Mapped[str | None] = mapped_column(String(40))
    effort: Mapped[str | None] = mapped_column(String(10))
    cached: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    input_tokens: Mapped[int | None] = mapped_column(Integer)
    output_tokens: Mapped[int | None] = mapped_column(Integer)
    cache_read_tokens: Mapped[int | None] = mapped_column(Integer)
    latency_ms: Mapped[int | None] = mapped_column(Integer)
    guardrail_result: Mapped[dict | None] = mapped_column(JSONB)
    payload: Mapped[dict | None] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(TS, server_default=func.now())

    __table_args__ = (Index("ix_ai_responses_created", "created_at"),)


class AIFeedback(Base):
    """사용자 피드백. reasons 분포가 프롬프트 개선의 우선순위를 정한다."""

    __tablename__ = "ai_feedback"

    id: Mapped[uuid.UUID] = _uuid_pk()
    request_id: Mapped[str] = mapped_column(
        String(40), ForeignKey("ai_responses.request_id", ondelete="CASCADE"), nullable=False
    )
    user_id: Mapped[str | None] = mapped_column(String(40))
    rating: Mapped[str] = mapped_column(String(6), nullable=False)
    reasons: Mapped[list[str] | None] = mapped_column(ARRAY(String(24)))
    comment: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(TS, server_default=func.now())


__all__ = [
    "Base",
    "Instrument",
    "PriceDaily",
    "IndexDaily",
    "Document",
    "DocumentChunk",
    "Event",
    "WikiFact",
    "WikiThesis",
    "TradeWatermark",
    "AIResponse",
    "AIFeedback",
]
