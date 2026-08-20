#!/usr/bin/env bash
# AI 파트 검사. CI 와 로컬이 같은 목록을 쓰도록 여기 한 곳에만 정의한다.
# 사용법: ai/scripts/check.sh
set -euo pipefail

cd "$(dirname "$0")/.."

# .env 가 있으면 읽는다. 없으면 compose.yaml 기본값으로 돈다.
if [ -f .env ]; then
  set -a
  # shellcheck disable=SC1091
  . ./.env
  set +a
fi
: "${DATABASE_URL:=postgresql+asyncpg://ai_invest:ai_invest@localhost:5432/ai_invest}"
export DATABASE_URL

# 의존성이 없으면 command not found 로 죽는다. 먼저 알려주고 끝낸다.
missing=()
for tool in ruff alembic pytest; do
  command -v "$tool" >/dev/null 2>&1 || missing+=("$tool")
done
if [ ${#missing[@]} -gt 0 ]; then
  echo "✗ 실행에 필요한 도구가 없습니다: ${missing[*]}" >&2
  echo "  가상환경을 만들고 의존성을 설치하세요:" >&2
  echo "    python3 -m venv .venv && . .venv/bin/activate && pip install -r requirements.txt" >&2
  exit 1
fi

# DB 가 안 떠 있으면 alembic 이 스택트레이스로 죽는다. 먼저 알려주고 끝낸다.
db_host_port=$(printf '%s' "$DATABASE_URL" | sed -E 's|.*@([^/]+)/.*|\1|')
db_host=${db_host_port%%:*}
db_port=${db_host_port##*:}
[ "$db_port" = "$db_host" ] && db_port=5432
if ! nc -z "$db_host" "$db_port" 2>/dev/null; then
  echo "✗ DB 에 연결할 수 없습니다: ${db_host}:${db_port}" >&2
  echo "  먼저 실행하세요:" >&2
  echo "    podman machine start   # 이미 떠 있으면 생략" >&2
  echo "    podman compose up -d" >&2
  exit 1
fi

failed=()
step() {
  local name=$1
  shift
  printf '\n▶ %s\n' "$name"
  if "$@"; then
    printf '  ✓ %s\n' "$name"
  else
    printf '  ✗ %s\n' "$name" >&2
    failed+=("$name")
  fi
}

step "린트"                  ruff check .
step "마이그레이션 적용"      alembic upgrade head
# 모델과 마이그레이션이 어긋나면 실패한다.
step "마이그레이션 드리프트"  alembic check
step "테스트"                pytest -q
step "평가 지표"             python -m eval.run --metrics
# 라우터를 고치고 스키마 재생성을 잊으면 실패한다.
step "OpenAPI 스키마 최신"   python -m scripts.export_openapi --check
# HTML 을 고치고 Markdown 재생성을 잊으면 에이전트가 낡은 문서를 읽는다.
if [ -f scripts/html2md.py ]; then
  step "설계 문서 최신"      python scripts/html2md.py --check
else
  printf '\n▶ 설계 문서 최신\n  - scripts/html2md.py 없음 — 건너뜀\n'
fi

# 첫 실패에서 멈추지 않고 전부 돌린 뒤 한 번에 보고한다.
# 한 번 실행으로 고칠 거리를 모두 보는 편이 왕복보다 빠르다.
printf '\n'
if [ ${#failed[@]} -eq 0 ]; then
  echo "✓ 전체 통과"
else
  printf '✗ 실패 %d건: %s\n' "${#failed[@]}" "${failed[*]}" >&2
  exit 1
fi
