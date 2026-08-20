#!/usr/bin/env bash
#
# 워크트리 부트스트랩.
#
# git에 없는 것(.env)과 로컬 환경(.venv)을 준비한다.
# 새 worktree는 추적 파일만 복제되므로 이 둘이 비어 있고, 그대로 두면
# 작업 시작과 동시에 "키가 없다"로 멈춘다.
#
# Orca setup 훅, 수동 worktree, 신규 팀원, CI가 모두 이 스크립트를 쓴다.
# 여러 번 실행해도 안전하다.

set -euo pipefail

cd "$(dirname "$0")/.."
REPO_ROOT="$PWD"

# git worktree list의 첫 항목이 항상 메인 워크트리다.
MAIN_WT="$(git worktree list --porcelain | awk 'NR==1 {print $2}')"

echo "▸ 부트스트랩: $REPO_ROOT"

# ── .env ────────────────────────────────────────────────
# 키는 커밋하지 않으므로 메인 워크트리에서 링크해 온다.
# 링크로 두면 키를 한 번만 갱신해도 모든 워크트리에 반영된다.
if [ -e .env ]; then
  echo "  · .env 있음"
elif [ -f "$MAIN_WT/.env" ] && [ "$MAIN_WT" != "$REPO_ROOT" ]; then
  ln -s "$MAIN_WT/.env" .env
  echo "  ✓ .env → $MAIN_WT/.env (링크)"
elif [ -f .env.example ]; then
  cp .env.example .env
  echo "  ✓ .env 생성 — .env.example 복사본이므로 키를 채워야 한다"
else
  echo "  ! .env도 .env.example도 없다"
fi

# ── 파이썬 환경 ─────────────────────────────────────────
# 워크트리마다 별도로 만든다. 공유하면 한쪽의 pip install이 다른 쪽을 오염시킨다.
# pip 캐시가 있어 두 번째부터는 빠르다.
if [ ! -x .venv/bin/python ]; then
  python3 -m venv .venv
  echo "  ✓ .venv 생성"
else
  echo "  · .venv 있음"
fi

.venv/bin/python -m pip install -q --upgrade pip
.venv/bin/python -m pip install -q -r requirements.txt
echo "  ✓ 의존성 설치 완료"

# ── DB ──────────────────────────────────────────────────
# 컨테이너는 호스트에 하나만 띄운다. 워크트리마다 띄우면 5432 포트가 충돌한다.
if command -v podman >/dev/null 2>&1 \
   && podman ps --format '{{.Names}}' 2>/dev/null | grep -q '^ai_invest_db$'; then
  echo "  · DB 실행 중 (호스트 공용)"
  .venv/bin/alembic upgrade head >/dev/null 2>&1 \
    && echo "  ✓ 마이그레이션 최신" \
    || echo "  ! 마이그레이션 실패 — alembic upgrade head 를 직접 확인할 것"
else
  echo "  ! DB 미실행 — podman machine start && podman-compose up -d"
fi

echo "▸ 완료. 테스트: .venv/bin/python -m pytest -q"
