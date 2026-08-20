#!/usr/bin/env python3
"""FastAPI가 만든 OpenAPI 스키마를 docs/openapi.json으로 내보낸다.

프론트 팀은 Postman을 쓰는데 지금 계약이라고 부를 만한 것은 파이썬 코드와
설계 문서 HTML뿐이다. 둘 다 이 저장소를 클론해야 읽을 수 있고, Postman에
넣을 수도 없다. 스키마는 FastAPI가 이미 만들고 있으니 파일로 떨어뜨려
커밋해 두면 저장소 밖에서도 계약을 볼 수 있다.

내보낸 파일이 코드와 어긋나면 없느니만 못하다. --check가 CI에서 그것을 막는다.

저장소 루트에서 모듈로 실행한다. app을 임포트해야 해서 스크립트 경로로
직접 부르면 sys.path에 루트가 없다 — eval.run과 같은 형태다.

    python -m scripts.export_openapi            # docs/openapi.json 생성
    python -m scripts.export_openapi --check    # 최신 여부만 확인 (CI용)
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

OUT = Path(__file__).resolve().parent.parent / "docs" / "openapi.json"

# 로컬 개발 서버. FastAPI는 servers를 비워 두는데, 그러면 Postman이 baseUrl을
# 빈 값으로 잡아 임포트 직후 아무 요청도 보낼 수 없다.
SERVERS = [{"url": "http://localhost:8000", "description": "로컬 개발 서버"}]

BEARER_AUTH = {
    "type": "http",
    "scheme": "bearer",
    "description": (
        "사용자 식별자는 토큰에서만 읽는다. 경로나 본문으로 받지 않는다. "
        "개발 환경에서는 비어 있지 않은 아무 문자열이나 통과한다."
    ),
}

# 인증이 필요 없는 경로. 나머지는 전부 Bearer 토큰을 요구한다.
PUBLIC_PATHS = {"/health"}


def build() -> dict[str, Any]:
    """앱에서 스키마를 뽑아 Postman이 바로 쓸 수 있게 손본다.

    app 임포트는 여기 안에서 한다. 모듈 스코프에서 하면 이 파일을 임포트하는
    것만으로 DB 엔진이 딸려 온다. 이 스크립트는 Postgres 없이 돌아야 한다.
    """
    try:
        from app.api.main import app
    except ModuleNotFoundError as exc:  # pragma: no cover - 실행 방식 안내
        if exc.name != "app":
            raise
        raise SystemExit(
            "저장소 루트에서 모듈로 실행해야 한다: python -m scripts.export_openapi\n"
            "직접 경로로 부르면 sys.path에 루트가 없어 app 패키지를 찾지 못한다."
        ) from exc

    schema = app.openapi()
    schema["servers"] = SERVERS

    # 인증은 평범한 Header 의존성이라 FastAPI가 스키마에 적어 주지 않는다.
    # 그대로 내보내면 Postman이 Authorization을 붙이지 않아 전부 401로 떨어지고,
    # 읽는 사람은 내보낸 스키마가 깨진 줄 안다. 여기서 한 번 채운다.
    schema.setdefault("components", {})["securitySchemes"] = {"bearerAuth": BEARER_AUTH}
    schema["security"] = [{"bearerAuth": []}]
    for path in PUBLIC_PATHS & schema["paths"].keys():
        for op in schema["paths"][path].values():
            op["security"] = []

    # 같은 이유로 authorization이 오퍼레이션마다 헤더 파라미터로도 잡혀 있다.
    # 위에서 securitySchemes로 선언했으니 남겨 두면 Postman이 요청 12개마다
    # 인증 입력을 두 벌 보여 준다. 인증은 한 군데서만 말한다.
    for ops in schema["paths"].values():
        for op in ops.values():
            params = [q for q in op.get("parameters", []) if q["name"].lower() != "authorization"]
            if params:
                op["parameters"] = params
            else:
                op.pop("parameters", None)

    return schema


def dump(schema: dict[str, Any]) -> str:
    """바이트까지 재현 가능하게 직렬화한다.

    --check는 문자열 비교라 키 순서가 한 번이라도 흔들리면 매 실행이 실패한다.
    sort_keys로 파이썬/FastAPI 버전이 바뀌어도 순서가 고정된다.
    스키마 설명이 한글이라 ensure_ascii는 끈다 — 읽으라고 커밋하는 파일이다.
    """
    return json.dumps(schema, indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def main() -> int:
    ap = argparse.ArgumentParser(description="OpenAPI 스키마 내보내기")
    ap.add_argument("--check", action="store_true", help="내보낸 파일이 최신인지만 확인")
    args = ap.parse_args()

    text = dump(build())

    if args.check:
        if not OUT.exists() or OUT.read_text(encoding="utf-8") != text:
            print(f"{OUT.name} 이(가) 코드와 다름", file=sys.stderr)
            print("python -m scripts.export_openapi 를 실행하고 커밋할 것", file=sys.stderr)
            return 1
        return 0

    OUT.write_text(text, encoding="utf-8")
    paths = json.loads(text)["paths"]
    ops = sum(len(v) for v in paths.values())
    print(f"  {OUT.relative_to(OUT.parent.parent)}  경로 {len(paths)}개 · 오퍼레이션 {ops}개  {len(text.encode()):,}B")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
