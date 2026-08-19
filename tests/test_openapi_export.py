"""내보낸 OpenAPI 스키마 회귀 테스트.

커밋된 docs/openapi.json이 코드와 어긋나면 저장소 밖에서 그것을 읽는 사람은
틀린 계약을 보게 된다. CI의 --check가 그 드리프트를 막지만, 여기서도 잡아
스키마를 바꾼 사람이 PR을 올리기 전에 알아채게 한다.
"""

from __future__ import annotations

import json
from pathlib import Path

from app.api.main import API_PREFIX
from scripts.export_openapi import OUT, build, dump

COMMITTED = json.loads(Path(OUT).read_text(encoding="utf-8"))


def test_committed_schema_matches_code() -> None:
    """커밋된 파일이 현재 앱과 같은지. 다르면 재생성 후 커밋해야 한다."""
    assert Path(OUT).read_text(encoding="utf-8") == dump(build()), (
        "docs/openapi.json 이 코드와 다름 — python -m scripts.export_openapi 실행 후 커밋"
    )


def test_export_is_deterministic() -> None:
    """두 번 뽑아 바이트가 같아야 --check가 매번 실패하지 않는다."""
    assert dump(build()) == dump(build())


def test_every_path_is_prefixed_or_health() -> None:
    """프리픽스를 벗어난 경로가 생기면 프론트가 baseUrl을 못 맞춘다."""
    stray = [p for p in COMMITTED["paths"] if not p.startswith(API_PREFIX) and p != "/health"]
    assert not stray, f"프리픽스 밖 경로: {stray}"


def test_bearer_auth_is_declared() -> None:
    """FastAPI가 안 적어 주는 인증을 내보내기가 채워 넣는지.

    이게 빠지면 Postman이 Authorization을 붙이지 않아 전부 401이 된다.
    """
    scheme = COMMITTED["components"]["securitySchemes"]["bearerAuth"]
    assert scheme["type"] == "http"
    assert scheme["scheme"] == "bearer"
    assert COMMITTED["security"] == [{"bearerAuth": []}]


def test_no_duplicate_authorization_parameter() -> None:
    """인증은 securitySchemes 한 군데서만. 헤더 파라미터로도 남으면 Postman이 두 벌 물어본다."""
    dupes = [
        (p, m)
        for p, ops in COMMITTED["paths"].items()
        for m, op in ops.items()
        if any(q["name"].lower() == "authorization" for q in op.get("parameters", []))
    ]
    assert not dupes, f"authorization 파라미터가 남아 있음: {dupes}"


def test_path_parameters_survive() -> None:
    """authorization만 걷어내야 한다 — ticker 같은 진짜 파라미터까지 지우면 안 된다."""
    params = COMMITTED["paths"][f"{API_PREFIX}/stocks/{{ticker}}/analysis"]["post"]["parameters"]
    assert [q["name"] for q in params] == ["ticker"]


def test_health_needs_no_token() -> None:
    """헬스체크는 토큰 없이 불러야 한다 — 임포트 직후 첫 확인용이다."""
    assert COMMITTED["paths"]["/health"]["get"]["security"] == []
