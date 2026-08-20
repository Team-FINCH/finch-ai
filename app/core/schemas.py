"""공통 응답 타입.

API 명세 §2.2 · §2.3 · §12를 코드로 옮긴 것이다.

핵심 불변식: `Section.segments`를 이어 붙이면 `Section.text`와 정확히 일치한다.
클라이언트가 어느 쪽을 쓰든 같은 내용을 보게 하려는 것이며,
이 규칙을 타입 수준에서 강제해 조립 단계의 실수를 막는다.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any

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


class Envelope[ContentT](BaseModel):
    request_id: str = Field(default_factory=new_request_id)
    generated_at: datetime = Field(default_factory=now_kst)
    data_as_of: DataAsOf = Field(default_factory=DataAsOf)
    model: str = Field(default_factory=lambda: settings.llm_model)
    cached: bool = False
    content: ContentT
    citations: list[Citation] = Field(default_factory=list)
    disclaimer: str = Field(default_factory=lambda: settings.disclaimer)


# ── 에러 응답 ────────────────────────────────────────────
class ErrorResponse(BaseModel):
    """실패 응답. 백엔드 API 명세 §1.3 의 형태를 그대로 따른다.

    전에는 `{"error": {...}, "request_id"}` 로 한 겹 감쌌는데, 백엔드는 최상위
    `code`·`message`·`detail` 을 읽어 프런트로 넘긴다. 봉투를 벗기는 쪽이
    백엔드에 매핑 코드를 만들게 하는 것보다 싸므로 우리가 맞춘다.

    `message` 는 **사용자에게 그대로 보여도 되는 한국어 문구**여야 한다.
    백엔드가 화면마다 문구를 다시 만들지 않는 전제다(§1.3).

    `request_id` 는 §1.3 에 없지만 남긴다. `POST /feedback` 이 이 값으로 응답을
    찾으므로, 에러에서만 빠지면 "이 답변 이상해요" 를 응답에 맞출 수 없다.
    성공 봉투와 같은 이름을 쓴다 — 한 개념을 두 이름으로 부르지 않는다.
    """

    code: ErrorCode
    message: str
    detail: dict[str, Any] = Field(default_factory=dict)
    request_id: str = Field(default_factory=new_request_id)
