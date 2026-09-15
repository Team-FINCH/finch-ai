"""선행 블록 회귀 테스트.

DB 연결 없이 도는 테스트만 여기 둔다. 병렬 트랙이 각자 작업하는 동안
공통 계약이 깨지지 않았는지 확인하는 안전망이다.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.dialects import postgresql
from sqlalchemy.schema import CreateTable

from app.api.main import _INGEST_PROBES, API_PREFIX, app
from app.core.config import settings
from app.core.enums import MetricSource, SegmentType, Unit
from app.core.models import Base
from app.core.schemas import DataAsOf, Envelope, Section, Segment

client = TestClient(app)
AUTH = {"X-User-Id": "u_test"}

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
    "chat_messages",
    "portfolio_diagnosis_cache",
    "ai_feedback",
    "ai_request_windows",
    "ai_token_daily",
    "ai_token_reservations",
}


# ── 스키마 ───────────────────────────────────────────────
def test_all_tables_defined() -> None:
    """테이블은 선행 블록에서 한 번에 정의한다. 트랙별로 추가하면 마이그레이션이 충돌한다."""
    assert set(Base.metadata.tables) == EXPECTED_TABLES


@pytest.mark.parametrize("table_name", sorted(EXPECTED_TABLES))
def test_ddl_compiles_for_postgres(table_name: str) -> None:
    table = Base.metadata.tables[table_name]
    ddl = str(CreateTable(table).compile(dialect=postgresql.dialect()))
    assert f"CREATE TABLE {table_name}" in ddl


def test_embedding_column_uses_configured_dimension() -> None:
    ddl = str(
        CreateTable(Base.metadata.tables["document_chunks"]).compile(dialect=postgresql.dialect())
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
    assert payload["model"] == "gpt-5.4-mini"
    assert payload["disclaimer"]
    assert payload["freshness_warnings"] == []


def test_data_as_of_attaches_kst_offset_to_naive_datetime() -> None:
    payload = DataAsOf(
        price=datetime(2026, 9, 15, 15, 30),
        portfolio=datetime(2026, 9, 15, 15, 30),
    ).model_dump(mode="json")

    assert payload["price"] == "2026-09-15T15:30:00+09:00"
    assert payload["portfolio"] == "2026-09-15T15:30:00+09:00"


def test_envelope_marks_only_stale_data_sources() -> None:
    generated = datetime(2026, 8, 28, 12, 0, tzinfo=timezone(timedelta(hours=9)))
    payload = Envelope[dict](
        content={},
        generated_at=generated,
        data_as_of=DataAsOf(
            price=generated - timedelta(minutes=21),
            portfolio=generated - timedelta(minutes=5),
            news=generated - timedelta(hours=7),
        ),
    ).model_dump(mode="json")

    assert [warning["source"] for warning in payload["freshness_warnings"]] == [
        "price",
        "news",
    ]
    assert payload["freshness_warnings"][0]["age_seconds"] == 21 * 60


# ── API 계약 ─────────────────────────────────────────────
class _HealthResult:
    def __init__(self, exists: bool) -> None:
        self._exists = exists

    def first(self) -> object | None:
        return object() if self._exists else None


class _HealthSession:
    def __init__(self, exists: list[bool]) -> None:
        self._exists = iter(exists)

    async def __aenter__(self) -> _HealthSession:
        return self

    async def __aexit__(self, *_: object) -> None:
        return None

    async def execute(self, _: object) -> _HealthResult:
        return _HealthResult(next(self._exists))


@pytest.mark.parametrize(
    ("exists", "status"),
    [
        ([True, True, True], "ok"),
        ([False, False, False], "degraded"),
        ([False, True, True], "degraded"),
        ([True, False, True], "degraded"),
        ([True, True, False], "degraded"),
    ],
)
def test_health_reports_ingest_state_and_always_returns_200(
    monkeypatch, exists: list[bool], status: str
) -> None:
    monkeypatch.setattr("app.api.main.SessionFactory", lambda: _HealthSession(exists))

    response = client.get("/health")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == status
    assert body["ingest_probe_errors"] == []
    assert body["ingest"] == dict(
        zip(("documents", "embeddings", "price_daily"), exists, strict=True)
    )
    assert {"status", "env", "model"} <= body.keys()


def test_health_returns_degraded_200_when_ingest_query_fails(monkeypatch) -> None:
    class BrokenSession:
        async def __aenter__(self) -> BrokenSession:
            raise RuntimeError("database unavailable")

        async def __aexit__(self, *_: object) -> None:
            return None

    monkeypatch.setattr("app.api.main.SessionFactory", BrokenSession)

    response = client.get("/health")

    assert response.status_code == 200
    assert response.json()["status"] == "degraded"
    assert response.json()["ingest_probe_errors"] == [
        "documents", "embeddings", "price_daily",
    ]
    assert response.json()["ingest"] == {
        "documents": False,
        "embeddings": False,
        "price_daily": False,
    }


#: /metrics 가 내보내는 게이지. 이름이 바뀌면 Grafana 대시보드와 경보가 조용히 빈다.
INGEST_GAUGES = (
    "ingest_documents",
    "ingest_embeddings",
    "ingest_price_daily",
    "ingest_embedding_backfill_pending",
    "ingest_price_backfill_pending",
)


def _gauges(body: str) -> dict[str, int]:
    """노출 텍스트에서 주석을 뺀 샘플만 뽑는다."""
    return {
        name: int(value)
        for name, value in (
            line.split() for line in body.splitlines() if not line.startswith("#")
        )
    }


@pytest.mark.parametrize(
    "exists",
    [
        [True, True, True, True, True],
        [False, False, False, False, False],
        # 문서는 들어왔는데 임베딩 백필이 덜 끝난 상태.
        # embeddings 는 청크 한 건만 임베딩돼도 1 이라 이것만으로는 안 보인다 (GitLab #62).
        [True, True, True, True, False],
        # 시세는 들어왔는데 종목별 백필이 덜 끝난 상태. price_daily 만으로는 안 보인다.
        [True, True, True, False, True],
    ],
)
def test_metrics_exposes_ingest_state_as_zero_or_one_gauges(
    monkeypatch, exists: list[bool]
) -> None:
    monkeypatch.setattr("app.api.main.SessionFactory", lambda: _HealthSession(exists))

    response = client.get("/metrics")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/plain")
    assert _gauges(response.text) == dict(
        zip(INGEST_GAUGES, [int(e) for e in exists], strict=True)
    )
    for name in INGEST_GAUGES:
        assert f"# TYPE {name} gauge" in response.text
    assert response.text.endswith("\n")


def test_metrics_returns_zeros_with_200_when_db_is_down(monkeypatch) -> None:
    """스크레이퍼가 500을 받으면 지표가 통째로 사라져 장애가 침묵으로 보인다."""

    class BrokenSession:
        async def __aenter__(self) -> BrokenSession:
            raise RuntimeError("database unavailable")

        async def __aexit__(self, *_: object) -> None:
            return None

    monkeypatch.setattr("app.api.main.SessionFactory", BrokenSession)

    response = client.get("/metrics")

    assert response.status_code == 200
    assert _gauges(response.text) == dict.fromkeys(INGEST_GAUGES, 0)


def test_ingest_probes_never_count_rows() -> None:
    """게이지 하나 읽자고 전수를 훑으면 적재량이 늘수록 점검이 느려진다."""
    for name, (stmt, _) in _INGEST_PROBES.items():
        sql = str(stmt.compile(dialect=postgresql.dialect())).lower()
        assert "count(" not in sql, f"{name}: {sql}"
        assert "limit" in sql, f"{name}: {sql}"


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
    assert res.json()["code"] == "UNAUTHORIZED"


def test_blank_trusted_header_returns_unauthorized() -> None:
    """헤더는 있는데 값이 공백이면 사용자를 특정할 수 없다. 빈 문자열을 id로 쓰면 안 된다."""
    res = client.post(
        f"{API_PREFIX}/stocks/005930/analysis",
        json={},
        headers={settings.trusted_user_header: "   "},
    )
    assert res.status_code == 401
    assert res.json()["code"] == "UNAUTHORIZED"


def test_internal_token_must_match_when_configured(monkeypatch) -> None:
    """백엔드 명세 §9 — 토큰이 틀리면 사용자 헤더가 멀쩡해도 통과시키지 않는다."""
    monkeypatch.setattr(settings, "backend_service_token", "s3cret")
    headers = {settings.trusted_user_header: "u_test"}

    for token in (None, "", "wrong"):
        sent = dict(headers)
        if token is not None:
            sent[settings.internal_token_header] = token
        res = client.post(f"{API_PREFIX}/stocks/005930/analysis", json={}, headers=sent)
        assert res.status_code == 401, token
        assert res.json()["code"] == "UNAUTHORIZED"

    ok = client.post(
        f"{API_PREFIX}/stocks/005930/analysis",
        json={},
        headers={**headers, settings.internal_token_header: "s3cret"},
    )
    # 토큰은 통과했다. 그 뒤 실패는 LLM 키가 없어서지 인증 때문이 아니다.
    assert ok.status_code != 401


def test_missing_internal_token_is_rejected_outside_local(monkeypatch) -> None:
    """운영에서 토큰을 안 넣은 것은 설정 사고다. 열어 두면 인증이 없는 것과 같다."""
    monkeypatch.setattr(settings, "backend_service_token", "")
    monkeypatch.setattr(settings, "app_env", "prod")
    res = client.post(
        f"{API_PREFIX}/stocks/005930/analysis",
        json={},
        headers={settings.trusted_user_header: "u_test"},
    )
    assert res.status_code == 401


def test_bearer_token_is_no_longer_accepted() -> None:
    """JWT 검증은 백엔드가 한다. Authorization만 들고 오면 사용자를 알 수 없다."""
    res = client.post(
        f"{API_PREFIX}/stocks/005930/analysis",
        json={},
        headers={"Authorization": "Bearer u_test"},
    )
    assert res.status_code == 401


def test_app_error_becomes_structured_response() -> None:
    """AppError가 예외 핸들러를 우회해 500으로 새면 안 된다."""
    res = client.post(f"{API_PREFIX}/stocks/005930/analysis", json={}, headers=AUTH)
    assert res.status_code == 409
    assert res.json()["code"] == "INSUFFICIENT_DATA"


@pytest.mark.parametrize(
    "order",
    [
        {"ticker": "00066", "side": "buy", "quantity": 1},  # 6자리가 아님
        {"ticker": "000660", "side": "hold", "quantity": 1},  # 정의되지 않은 side
        {"ticker": "000660", "side": "buy", "quantity": 0},  # 수량 0
    ],
)
def test_validation_errors_use_invalid_request(order: dict) -> None:
    res = client.post(f"{API_PREFIX}/orders/preview", json={"orders": [order]}, headers=AUTH)
    assert res.status_code == 400
    assert res.json()["code"] == "INVALID_REQUEST"


def test_briefing_returns_envelope_when_empty() -> None:
    res = client.get(f"{API_PREFIX}/briefing", headers=AUTH)
    assert res.status_code == 200
    body = res.json()
    assert body["content"]["status"] == "empty"
    assert body["disclaimer"]


@pytest.mark.parametrize("completed", [0, 1, 2])
def test_health_reports_unchecked_probes_after_query_failure(monkeypatch, completed):
    class FailingSession(_HealthSession):
        async def execute(self, stmt):
            if len(calls) == completed:
                raise RuntimeError("private database connection details")
            calls.append(stmt)
            return _HealthResult(True)

    calls = []
    monkeypatch.setattr("app.api.main.SessionFactory", lambda: FailingSession([]))
    response = client.get("/health")
    body = response.json()
    names = ["documents", "embeddings", "price_daily"]
    assert response.status_code == 200
    assert body["status"] == "degraded"
    assert body["ingest_probe_errors"] == names[completed:]
    assert body["ingest"] == {name: i < completed for i, name in enumerate(names)}
    assert "private database" not in response.text
