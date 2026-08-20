"""환경 설정.

키는 .env에서만 읽는다. 기본값을 넣어두는 항목은 개발 편의를 위한 것이며,
자격증명에는 절대 기본값을 두지 않는다.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # ── 저장소 ───────────────────────────────────────────
    database_url: str = (
        "postgresql+asyncpg://ai_invest:ai_invest@localhost:5432/ai_invest"
    )
    db_echo: bool = False

    # ── LLM ──────────────────────────────────────────────
    anthropic_api_key: str = ""
    # 어느 공급자로 붙을지. 고른 쪽 키가 비어 있으면 다른 쪽으로 넘어간다.
    llm_provider: str = "openai"
    llm_model: str = "claude-opus-5"
    llm_max_tokens: int = 16_000
    llm_timeout_s: int = 30

    # ── 임베딩 ───────────────────────────────────────────
    # DDL 시점에 벡터 차원이 고정되므로, 모델을 바꾸면 마이그레이션이 필요하다.
    # text-embedding-3 계열은 dimensions 파라미터로 축소를 지원해(Matryoshka)
    # 기본 1536 대신 1024를 요청한다. 스키마를 건드리지 않기 위해서다.
    embedding_dim: int = 1024
    embedding_model: str = "text-embedding-3-small"
    # 한 요청이 분당 토큰 한도를 넘으면 즉시 429가 난다. 청크가 약 1,100토큰이라
    # 30건이면 33K로 Tier 1의 여유 안에 들어온다. 128로 두었더니 한 요청이
    # 14만 토큰이 되어 매번 거부당했다.
    embedding_batch_size: int = 30

    openai_api_key: str = ""
    # 사내 게이트웨이를 쓰면 여기를 바꾼다. 비우면 순정 OpenAI로 붙는다.
    openai_base_url: str = ""
    # ANTHROPIC_API_KEY가 없는 동안 설명 생성을 대신 맡는 모델. 임시다.
    openai_llm_model: str = "gpt-5.4-mini"

    # ── 외부 데이터 ──────────────────────────────────────
    kis_app_key: str = ""
    kis_app_secret: str = ""
    kis_account_no: str = ""
    dart_api_key: str = ""
    naver_client_id: str = ""
    naver_client_secret: str = ""
    ecos_api_key: str = ""
    # KRX OpenAPI. 지수 시계열과 시가총액 스냅샷을 받는다.
    # 키는 AUTH_KEY 헤더로만 보낸다 — httpx가 전체 URL을 로그에 남기므로
    # 쿼리 파라미터에 실으면 로그로 샌다.
    krx_api_key: str = ""

    # ── 백엔드 원장 (읽기 전용) ──────────────────────────
    # 권한이 열리기 전에는 시드 어댑터로 대체해 병렬 진행한다. API 명세 §11 참조.
    backend_base_url: str = "http://localhost:8080"
    # 서비스 간 공유 토큰. 백엔드가 X-Internal-Token 으로 보내고 우리가 대조한다.
    # 우리가 백엔드 /internal/v1 을 부를 때 실어 보내는 값이기도 하다 — 같은 토큰이다.
    # 비어 있으면 로컬에서만 검증을 건너뛴다(deps._check_internal_token).
    backend_service_token: str = ""
    # 백엔드가 JWT를 검증한 뒤 사용자 식별자를 실어 보내는 헤더. 백엔드가 다른
    # 이름을 쓰면 여기 한 줄만 바꾼다. 값을 검증 없이 신뢰하므로 AI 서버는
    # 내부 네트워크에서만 접근 가능해야 한다 — API 계약 §4 참조.
    trusted_user_header: str = "X-User-Id"
    internal_token_header: str = "X-Internal-Token"
    # 원장을 어디서 읽을지. 백엔드 /internal/v1 이 열리기 전에는 seed 로 병렬 진행한다.
    #   seed    — tests/fixtures 의 JSON. 지금 기본값이다
    #   backend — 보유·거래는 백엔드, 시계열·섹터는 우리 DB (BackendLedgerSource)
    #   none    — 원장 없음. 개인화 섹션이 조용히 비활성된다
    # Literal 이라 오타는 기동 시점에 걸린다. 불리언 하나로는 세 상태를 못 담는다.
    ledger_source: Literal["seed", "backend", "none"] = "seed"
    seed_fixture_path: str = "tests/fixtures/seed_portfolio.json"

    # ── 엔진 상수 ────────────────────────────────────────
    # 국내 증시 연간 개장일 실측치. 252는 미국 기준이라 쓰지 않는다.
    trading_days_per_year: int = 246
    min_history_days: int = 60
    # 종가가 빠진 날 직전 종가로 거슬러 올라갈 최대 달력일(Ledger.price).
    # 국내 최장 휴장이 설·추석 + 임시공휴일 + 주말로 7일이라 여유를 두고 10일로
    # 잡았다. 거래일이 아니라 달력일로 세는 것은 우리에게 장 달력이 없어서다.
    # 실측으로 조정할 값이라 설정으로 뺐다 — 넘으면 메우지 않고 명시적으로 실패한다.
    price_asof_max_days: int = 10

    # ── 운영 ─────────────────────────────────────────────
    app_env: str = "local"
    disclaimer: str = "본 정보는 투자 판단을 돕기 위한 참고 자료이며 투자 권유가 아닙니다."


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
