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

from app.core.config import settings
from app.llm.client import get_llm_client


@pytest.fixture(autouse=True)
def _no_live_api(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    for name in ("anthropic_api_key", "openai_api_key"):
        monkeypatch.setattr(settings, name, "", raising=False)
    get_llm_client.cache_clear()
    yield
    get_llm_client.cache_clear()
