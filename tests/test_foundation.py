"""선행 블록 회귀 테스트.

DB 연결 없이 도는 테스트만 여기 둔다. 병렬 트랙이 각자 작업하는 동안
공통 계약이 깨지지 않았는지 확인하는 안전망이다.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.dialects import postgresql
from sqlalchemy.schema import CreateTable

from app.api.main import API_PREFIX, app
from app.core.enums import MetricSource, SegmentType, Unit
from app.core.models import Base
from app.core.schemas import DataAsOf, Envelope, Section, Segment

client = TestClient(app)
AUTH = {"Authorization": "Bearer u_test"}

EXPECTED_TABLES = {
    "instruments",
    "price_daily",
    "index_daily",
    "financial_annual",
    "documents",
    "document_chunks",
    "events",
    "wiki_facts",
    "wiki_theses",
    "trade_watermarks",
    "ai_responses",
    "ai_feedback",
}


# ── 스키마 ───────────────────────────────────────────────
def test_all_tables_defined() -> None:
    """테이블은 선행 블록에서 한 번에 정의한다. 트랙별로 추가하면 마이그레이션이 충돌한다."""
    assert set(Base.metadata.tables) == EXPECTED_TABLES


@pytest.mark.parametrize("table_name", sorted(EXPECTED_TABLES))
def test_ddl_compiles_for_postgres(table_name: str) -> None:
    table = Base.metadata.tables[table_name]
    ddl = str(CreateTable(table).compile(dialect=postgresql.dialect()))
    assert f'CREATE TABLE {table_name}' in ddl


def test_embedding_column_uses_configured_dimension() -> None:
    ddl = str(
        CreateTable(Base.metadata.tables["document_chunks"]).compile(
            dialect=postgresql.dialect()
        )
    )
    assert "VECTOR(1024)" in ddl.upper()


# ── Section / Segment 불변식 ─────────────────────────────
def _sample_segments() -> list[Segment]:
    return [
        Segment.text("반도체 관련 자산이 포트폴리오의 "),
        Segment.metric("42.3%", 0.423, MetricSource.RISK_ENGINE, unit=Unit.RATIO),
        Segment.text("를 차지합니다."),
    ]


def test_segments_reconstruct_text() -> None:
    section = Section.from_segments(_sample_segments())
    assert section.text == "반도체 관련 자산이 포트폴리오의 42.3%를 차지합니다."
    assert "".join(s.value for s in section.segments) == section.text


def test_section_rejects_mismatched_segments() -> None:
    """segments를 이어 붙인 결과가 text와 다르면 응답을 만들 수 없어야 한다."""
    with pytest.raises(ValueError):
        Section(text="전혀 다른 문장입니다.", segments=_sample_segments())


def test_section_allows_text_only() -> None:
    """기본 렌더링 경로. segments 없이 text만으로도 유효하다."""
    assert Section(text="관련 자료를 찾지 못했습니다.").segments == []


def test_metric_segment_requires_raw_and_source() -> None:
    with pytest.raises(ValueError):
        Segment(type=SegmentType.METRIC, value="42.3%")


def test_metrics_helper_filters_text_segments() -> None:
    section = Section.from_segments(_sample_segments())
    metrics = section.metrics()
    assert len(metrics) == 1
    assert metrics[0].raw == 0.423
    assert metrics[0].source is MetricSource.RISK_ENGINE


# ── 응답 봉투 ────────────────────────────────────────────
def test_envelope_defaults() -> None:
    payload = Envelope[dict](content={}, data_as_of=DataAsOf()).model_dump(mode="json")
    assert payload["request_id"].startswith("req_")
    assert payload["generated_at"].endswith("+09:00"), "시각은 KST 오프셋을 명시한다"
    assert payload["model"] == "claude-opus-5"
    assert payload["disclaimer"]


# ── API 계약 ─────────────────────────────────────────────
def test_health() -> None:
    assert client.get("/health").status_code == 200


def test_all_endpoints_registered() -> None:
    paths = set(app.openapi()["paths"])
    for path in (
        f"{API_PREFIX}/stocks/{{ticker}}/analysis",
        f"{API_PREFIX}/chat",
        f"{API_PREFIX}/portfolio/diagnosis",
        f"{API_PREFIX}/portfolio/attribution",
        f"{API_PREFIX}/orders/preview",
        f"{API_PREFIX}/briefing",
        f"{API_PREFIX}/wiki",
        f"{API_PREFIX}/wiki/theses",
        f"{API_PREFIX}/feedback",
    ):
        assert path in paths, path


def test_missing_auth_returns_unauthorized() -> None:
    res = client.post(f"{API_PREFIX}/stocks/005930/analysis", json={})
    assert res.status_code == 401
    assert res.json()["error"]["code"] == "UNAUTHORIZED"


def test_app_error_becomes_structured_response() -> None:
    """AppError가 예외 핸들러를 우회해 500으로 새면 안 된다."""
    res = client.post(f"{API_PREFIX}/stocks/005930/analysis", json={}, headers=AUTH)
    assert res.status_code == 409
    assert res.json()["error"]["code"] == "INSUFFICIENT_DATA"


@pytest.mark.parametrize(
    "order",
    [
        {"ticker": "00066", "side": "buy", "quantity": 1},   # 6자리가 아님
        {"ticker": "000660", "side": "hold", "quantity": 1},  # 정의되지 않은 side
        {"ticker": "000660", "side": "buy", "quantity": 0},   # 수량 0
    ],
)
def test_validation_errors_use_invalid_request(order: dict) -> None:
    res = client.post(f"{API_PREFIX}/orders/preview", json={"orders": [order]}, headers=AUTH)
    assert res.status_code == 400
    assert res.json()["error"]["code"] == "INVALID_REQUEST"


def test_briefing_returns_envelope_when_empty() -> None:
    res = client.get(f"{API_PREFIX}/briefing", headers=AUTH)
    assert res.status_code == 200
    body = res.json()
    assert body["content"]["status"] == "empty"
    assert body["disclaimer"]
