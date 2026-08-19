"""공통 응답 타입.

API 명세 §2.2 · §2.3 · §12를 코드로 옮긴 것이다.

핵심 불변식: `Section.segments`를 이어 붙이면 `Section.text`와 정확히 일치한다.
클라이언트가 어느 쪽을 쓰든 같은 내용을 보게 하려는 것이며,
이 규칙을 타입 수준에서 강제해 조립 단계의 실수를 막는다.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any, Generic, TypeVar

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.core.config import settings
from app.core.enums import (
    CitationType,
    Direction,
    MetricSource,
    SegmentType,
    Unit,
)
from app.core.errors import ErrorCode

KST_OFFSET_HOURS = 9


def now_kst() -> datetime:
    """모든 시각은 KST 오프셋을 명시해 직렬화한다."""
    from datetime import timedelta

    return datetime.now(timezone(timedelta(hours=KST_OFFSET_HOURS)))


def new_request_id() -> str:
    return f"req_{uuid.uuid4().hex[:16]}"


# ── 응답 조각 ────────────────────────────────────────────
class Segment(BaseModel):
    """문장을 텍스트와 수치로 쪼갠 조각.

    `type=TEXT`인 조각은 `value`만 가진다. 나머지 필드는 `METRIC`에서만 채운다.
    """

    model_config = ConfigDict(use_enum_values=False)

    type: SegmentType
    value: str
    raw: float | int | None = None
    unit: Unit | None = None
    source: MetricSource | None = None
    direction: Direction | None = None

    @model_validator(mode="after")
    def _check_metric_fields(self) -> Segment:
        if self.type is SegmentType.METRIC:
            if self.raw is None:
                raise ValueError("metric segment에는 raw가 필요하다")
            if self.source is None:
                raise ValueError("metric segment에는 source가 필요하다")
        return self

    @classmethod
    def text(cls, value: str) -> Segment:
        return cls(type=SegmentType.TEXT, value=value)

    @classmethod
    def metric(
        cls,
        value: str,
        raw: float | int,
        source: MetricSource,
        *,
        unit: Unit | None = None,
        direction: Direction | None = None,
    ) -> Segment:
        return cls(
            type=SegmentType.METRIC,
            value=value,
            raw=raw,
            unit=unit,
            source=source,
            direction=direction,
        )


class Section(BaseModel):
    """서술 한 덩어리.

    `text`만 출력해도 정상 동작한다. `segments`는 수치 스타일링이 필요할 때만 쓴다.
    """

    title: str | None = None
    text: str
    segments: list[Segment] = Field(default_factory=list)
    cached: bool = False
    cached_at: datetime | None = None

    @model_validator(mode="after")
    def _segments_reconstruct_text(self) -> Section:
        if not self.segments:
            return self
        joined = "".join(s.value for s in self.segments)
        if joined != self.text:
            raise ValueError(
                "segments를 이어 붙인 결과가 text와 다르다. "
                f"text={self.text!r} joined={joined!r}"
            )
        return self

    @classmethod
    def from_segments(
        cls,
        segments: list[Segment],
        *,
        title: str | None = None,
        cached: bool = False,
        cached_at: datetime | None = None,
    ) -> Section:
        """조각에서 Section을 만든다. text는 조각을 이어 붙여 생성하므로 어긋날 수 없다."""
        return cls(
            title=title,
            text="".join(s.value for s in segments),
            segments=segments,
            cached=cached,
            cached_at=cached_at,
        )

    def metrics(self) -> list[Segment]:
        return [s for s in self.segments if s.type is SegmentType.METRIC]


# ── 근거 ─────────────────────────────────────────────────
class Citation(BaseModel):
    id: str
    type: CitationType
    title: str
    source: str
    publisher: str | None = None
    url: str | None = None
    published_at: datetime | None = None
    snippet: str | None = None
    relevance: float | None = None


# ── 응답 봉투 ────────────────────────────────────────────
class DataAsOf(BaseModel):
    """데이터 원천별 기준 시각. UI에 반드시 노출한다.

    시세는 지연될 수 있으므로 생성 시각과 별도로 관리한다.
    """

    price: datetime | None = None
    portfolio: datetime | None = None
    filings: datetime | None = None
    news: datetime | None = None
    macro: datetime | None = None


ContentT = TypeVar("ContentT")


class Envelope(BaseModel, Generic[ContentT]):
    request_id: str = Field(default_factory=new_request_id)
    generated_at: datetime = Field(default_factory=now_kst)
    data_as_of: DataAsOf = Field(default_factory=DataAsOf)
    model: str = Field(default_factory=lambda: settings.llm_model)
    cached: bool = False
    content: ContentT
    citations: list[Citation] = Field(default_factory=list)
    disclaimer: str = Field(default_factory=lambda: settings.disclaimer)


# ── 에러 응답 ────────────────────────────────────────────
class ErrorBody(BaseModel):
    code: ErrorCode
    message: str
    detail: dict[str, Any] = Field(default_factory=dict)


class ErrorResponse(BaseModel):
    error: ErrorBody
    request_id: str = Field(default_factory=new_request_id)
