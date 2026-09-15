"""테스트 공통 설정.

지금까지 테스트가 실제 API를 부르지 않은 이유는 `.env`에 키가 없어서였다.
키를 넣는 순간 `get_llm_client()`가 진짜 클라이언트를 돌려주며 일부 테스트가
바깥으로 나가려 했다 — 환경에 따라 결과가 달라지는 상태였다.

여기서 키를 지워 그 우연을 규칙으로 바꾼다. 공급자 구현을 직접 시험하는
테스트는 가짜 HTTP 클라이언트를 주입해 만들면 되므로 이 픽스처와 부딪히지 않는다.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest

from app.core.adapters import ledger_source
from app.core.config import settings
from app.llm.client import get_llm_client


@pytest.fixture(autouse=True)
def _no_live_api(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    # API 테스트는 고정 seed 원장을 사용한다. 운영 기본값은 backend이므로
    # 테스트가 운영 DB 상태에 따라 달라지지 않도록 명시적으로 격리한다.
    monkeypatch.setattr(settings, "ledger_source", "seed")
    ledger_source.cache_clear()
    monkeypatch.setattr(settings, "gms_key", "")
    monkeypatch.setattr(settings, "naver_client_id", "")
    monkeypatch.setattr(settings, "naver_client_secret", "")
    get_llm_client.cache_clear()
    ledger_source.cache_clear()
    yield
    get_llm_client.cache_clear()
