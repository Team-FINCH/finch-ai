"""임베딩.

인터페이스와 OpenAI 구현. 제공자를 바꿔도 호출부는 `get_embedder()`만 본다.

text-embedding-3 계열은 `dimensions`로 축소를 지원한다(Matryoshka). 기본 1536 대신
1024를 요청해 `document_chunks.embedding`의 DDL을 건드리지 않는다.
"""

from __future__ import annotations

import logging
import math
import re
import time
from abc import ABC, abstractmethod
from collections.abc import Iterator, Mapping, Sequence

import httpx

from app.core.config import settings

log = logging.getLogger("app.rag.embedding")

OPENAI_DEFAULT_BASE_URL = "https://api.openai.com/v1"
HTTP_TIMEOUT = 60.0

# 429를 맞고 재시도하는 것도 일당 요청 한도를 깎는다. 그래서 재시도는 최후의
# 수단으로 두고, 평소에는 응답 헤더의 잔량을 보며 미리 쉰다.
RETRY_MAX = 5
RETRY_CAP = 90.0

_DURATION = re.compile(r"(\d+(?:\.\d+)?)\s*(ms|h|m|s)")
_UNIT = {"ms": 0.001, "s": 1.0, "m": 60.0, "h": 3600.0}


def _seconds(value: str | None) -> float:
    """`Retry-After`와 `x-ratelimit-reset-*`를 초로 바꾼다.

    전자는 맨숫자(`1`), 후자는 기간 표기(`35.161s`, `23h59m48.732s`)로 온다.
    """
    if not value:
        return 0.0
    parts = _DURATION.findall(value)
    if parts:
        return sum(float(n) * _UNIT[u] for n, u in parts)
    try:
        return float(value)
    except ValueError:
        return 0.0


class Embedder(ABC):
    """텍스트 조각을 벡터로 바꾼다.

    차원은 `document_chunks.embedding`의 DDL과 같아야 한다. 다르면 INSERT가 거부된다.
    """

    dim: int = settings.embedding_dim
    model: str = settings.embedding_model

    @abstractmethod
    def embed(self, texts: Sequence[str]) -> list[list[float] | None]:
        """입력과 같은 길이의 벡터 목록. 만들지 못한 자리는 None."""


class NullEmbedder(Embedder):
    """아무것도 만들지 않는다.

    키가 없을 때의 기본값이다. 0 벡터를 채우면 유사도 검색이 조용히 엉뚱한 결과를
    내므로, 비어 있음을 NULL로 남긴다.
    """

    def embed(self, texts: Sequence[str]) -> list[list[float] | None]:
        return [None] * len(texts)


def _batched(texts: Sequence[str], size: int) -> Iterator[tuple[int, list[str]]]:
    for i in range(0, len(texts), size):
        yield i, list(texts[i : i + size])


class OpenAIEmbedder(Embedder):
    """OpenAI 호환 임베딩 엔드포인트.

    사내 게이트웨이도 같은 스키마를 쓰므로 base_url만 바꾸면 그대로 붙는다.
    """

    def __init__(
        self,
        api_key: str,
        *,
        base_url: str | None = None,
        model: str | None = None,
        dim: int | None = None,
        batch_size: int | None = None,
        client: httpx.Client | None = None,
    ) -> None:
        if not api_key:
            raise ValueError("OPENAI_API_KEY가 필요하다")
        self.model = model or settings.embedding_model
        self.dim = dim or settings.embedding_dim
        self._batch = batch_size or settings.embedding_batch_size
        self._url = (base_url or settings.openai_base_url or OPENAI_DEFAULT_BASE_URL).rstrip("/")
        self._key = api_key
        self._client = client
        self._warned_truncate = False
        self._remaining: int | None = None  # 분당 토큰 잔량. 첫 응답 전에는 모른다
        self._reset = 0.0
        # 문자당 토큰. 응답의 usage로 실측해 갱신한다. 시작값 1.0은 과대평가라
        # 첫 배치에서 429를 맞지 않고, 이후 실제 비율(한글 기준 0.7 안팎)로 수렴한다.
        self._per_char = 1.0

    def _note_limits(self, headers: Mapping[str, str]) -> None:
        raw = headers.get("x-ratelimit-remaining-tokens")
        if raw is None:
            return
        try:
            self._remaining = int(raw)
        except ValueError:
            return
        self._reset = _seconds(headers.get("x-ratelimit-reset-tokens"))

    def _pace(self, batch: Sequence[str]) -> None:
        """이번 배치가 남은 분당 토큰을 넘기면 버킷이 찰 때까지 쉰다."""
        if self._remaining is None:
            return
        cost = int(sum(len(t) for t in batch) * self._per_char * 1.1)  # 10%는 여유
        if cost <= self._remaining:
            return
        wait = min(self._reset + 1.0, RETRY_CAP)
        log.info("분당 토큰 잔량 %d < 예상 %d — %.1f초 대기", self._remaining, cost, wait)
        time.sleep(wait)
        self._remaining = None  # 리셋됐다고 보고 다음 응답에서 다시 읽는다

    def _post(self, batch: list[str]) -> list[list[float]]:
        payload = {"model": self.model, "input": batch, "dimensions": self.dim}
        headers = {"Authorization": f"Bearer {self._key}"}
        client = self._client or httpx.Client(timeout=HTTP_TIMEOUT)
        try:
            for attempt in range(1, RETRY_MAX + 1):
                res = client.post(f"{self._url}/embeddings", json=payload, headers=headers)
                self._note_limits(res.headers)
                if res.status_code != 429:
                    break
                wait = min(
                    max(
                        _seconds(res.headers.get("retry-after")),
                        _seconds(res.headers.get("x-ratelimit-reset-tokens")),
                        1.0,
                    ),
                    RETRY_CAP,
                )
                log.warning("429 — %.1f초 뒤 재시도 %d/%d", wait, attempt, RETRY_MAX)
                time.sleep(wait)
            res.raise_for_status()
            body = res.json()
        finally:
            if self._client is None:
                client.close()

        used = (body.get("usage") or {}).get("prompt_tokens")
        chars = sum(len(t) for t in batch)
        if used and chars:
            self._per_char = used / chars

        # 응답 순서를 신뢰하지 않는다. index로 되돌린다.
        rows = sorted(body["data"], key=lambda d: d["index"])
        vectors = [r["embedding"] for r in rows]
        if len(vectors) != len(batch):
            raise ValueError(f"응답 개수 불일치: 요청 {len(batch)} 응답 {len(vectors)}")
        return [self._fit(v) for v in vectors]

    def _fit(self, vec: list[float]) -> list[float]:
        """차원을 맞춘다.

        게이트웨이가 dimensions를 통과시키지 않으면 기본 1536이 돌아온다.
        text-embedding-3 계열은 Matryoshka 학습이라 뒤를 잘라내고 L2 정규화하면
        되는데, 이는 OpenAI가 문서화한 축소 방법이다. 서버가 줄여준 것과
        완전히 같지는 않지만 검색 품질 손실은 작다.

        반대로 짧게 오면 자를 수 없으므로 거부한다.
        """
        n = len(vec)
        if n == self.dim:
            return vec
        if n < self.dim:
            raise ValueError(f"차원 부족: 기대 {self.dim} 실제 {n}")

        if not self._warned_truncate:
            log.warning(
                "응답 차원이 %d다. dimensions가 전달되지 않은 것으로 보여 "
                "%d로 잘라 정규화한다.", n, self.dim
            )
            self._warned_truncate = True

        head = vec[: self.dim]
        norm = math.sqrt(sum(x * x for x in head))
        if norm == 0:
            raise ValueError("잘라낸 벡터의 노름이 0이다")
        return [x / norm for x in head]

    def embed(self, texts: Sequence[str]) -> list[list[float] | None]:
        """배치 단위로 호출한다. 한 배치가 실패해도 나머지는 살린다.

        실패한 자리는 None으로 남겨 재실행 때 그 조각만 다시 시도할 수 있게 한다.
        """
        out: list[list[float] | None] = [None] * len(texts)
        for start, batch in _batched(texts, self._batch):
            try:
                self._pace(batch)
                for offset, vec in enumerate(self._post(batch)):
                    out[start + offset] = vec
            except (httpx.HTTPError, KeyError, ValueError):
                log.exception("임베딩 배치 실패: %d~%d", start, start + len(batch) - 1)
        return out


def get_embedder() -> Embedder:
    """설정을 보고 구현을 고른다. 키가 없으면 Null이다."""
    if settings.openai_api_key:
        return OpenAIEmbedder(settings.openai_api_key)
    log.warning("OPENAI_API_KEY가 없어 임베딩을 건너뛴다. .env를 확인하라")
    return NullEmbedder()
