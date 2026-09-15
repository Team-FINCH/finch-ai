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
    database_url: str = "postgresql+asyncpg://ai_invest:ai_invest@localhost:5432/ai_invest"
    db_echo: bool = False

    # ── LLM ──────────────────────────────────────────────
    # GMS OpenAI 호환 Chat Completions API를 사용한다.
    gms_key: str = ""
    gms_base_url: str = "https://api.openai.com/v1"
    llm_model: str = "gpt-5.4-mini"
    llm_max_tokens: int = 16_000
    llm_timeout_s: int = 30
    #: 최초 1회 + 재생성. GMS 크레딧의 대부분이 재생성에서 나가므로
    #: 배포 없이 조일 수 있게 설정으로 뺀다 (GitLab #64).
    llm_max_attempts: int = 2
    #: 종목분석 공용 섹션 캐시. 길수록 같은 종목 재생성이 줄어든다.
    analysis_cache_ttl_h: int = 24

    # 사용자 요청은 분당 횟수와 일일 GMS 토큰 예산을 함께 제한한다. 토큰은 실제
    # GMS 호출 직전에 예약하므로 캐시 응답은 일일 예산을 소비하지 않는다.
    ai_rate_limit_window_s: int = 60
    ai_rate_limit_stocks_per_minute: int = 10
    ai_rate_limit_chat_per_minute: int = 20
    ai_rate_limit_portfolio_per_minute: int = 10
    ai_rate_limit_orders_per_minute: int = 30
    ai_rate_limit_briefing_per_minute: int = 60
    ai_daily_token_budget: int = 500_000
    ai_gms_reservation_tokens: int = 20_000
    ai_gms_reservation_ttl_s: int = 5 * 60

    # 배치·스케줄러는 사용자가 누른 요청이 아니므로 개인 예산을 깎지 않는다. 대신
    # 시스템 몫의 장부를 따로 두어, 상한 없이 GMS 요금이 새는 경로를 막는다.
    ai_batch_daily_token_budget: int = 2_000_000

    # 데일리 브리핑 배치: 최초 시도를 포함한 횟수와 재시도 간 기본 대기 시간.
    briefing_batch_attempts: int = 3
    briefing_batch_backoff_s: float = 1.0

    # 응답 데이터 기준 시각이 이보다 오래되면 freshness_warnings에 표시한다.
    freshness_price_s: int = 20 * 60
    freshness_portfolio_s: int = 15 * 60
    freshness_filings_s: int = 24 * 60 * 60
    freshness_news_s: int = 6 * 60 * 60
    freshness_macro_s: int = 7 * 24 * 60 * 60

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
    #   seed    — tests/fixtures 의 JSON. 테스트에서만 명시적으로 선택한다
    #   backend — 보유·거래는 백엔드, 시계열·섹터는 우리 DB (BackendLedgerSource)
    #   none    — 원장 없음. 개인화 섹션이 조용히 비활성된다
    # Literal 이라 오타는 기동 시점에 걸린다. 불리언 하나로는 세 상태를 못 담는다.
    # 실사용 원장은 백엔드만 소유한다. seed는 테스트에서 명시적으로 선택한다.
    ledger_source: Literal["seed", "backend", "none"] = "backend"
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
