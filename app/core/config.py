"""환경 설정.

키는 .env에서만 읽는다. 기본값을 넣어두는 항목은 개발 편의를 위한 것이며,
자격증명에는 절대 기본값을 두지 않는다.
"""

from __future__ import annotations

from functools import lru_cache

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

    # ── 외부 데이터 ──────────────────────────────────────
    kis_app_key: str = ""
    kis_app_secret: str = ""
    kis_account_no: str = ""
    dart_api_key: str = ""
    naver_client_id: str = ""
    naver_client_secret: str = ""
    ecos_api_key: str = ""

    # ── 백엔드 원장 (읽기 전용) ──────────────────────────
    # 권한이 열리기 전에는 시드 어댑터로 대체해 병렬 진행한다. API 명세 §11 참조.
    backend_base_url: str = "http://localhost:8080"
    backend_service_token: str = ""
    use_seed_adapter: bool = True
    seed_fixture_path: str = "tests/fixtures/seed_portfolio.json"

    # ── 엔진 상수 ────────────────────────────────────────
    # 국내 증시 연간 개장일 실측치. 252는 미국 기준이라 쓰지 않는다.
    trading_days_per_year: int = 246
    min_history_days: int = 60

    # ── 운영 ─────────────────────────────────────────────
    app_env: str = "local"
    disclaimer: str = "본 정보는 투자 판단을 돕기 위한 참고 자료이며 투자 권유가 아닙니다."


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
