"""임베딩 인터페이스.

제공자 키가 아직 없다. 인터페이스와 Null 구현만 두고 실제 호출은 붙이지 않는다.
적재기가 임베딩 유무와 무관하게 먼저 돌아가야 공시 원문 확보가 막히지 않는다.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Sequence

from app.core.config import settings


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

    제공자가 정해지기 전까지의 기본값이다. 0 벡터를 채우면 유사도 검색이
    조용히 엉뚱한 결과를 내므로, 비어 있음을 NULL로 남긴다.
    """

    def embed(self, texts: Sequence[str]) -> list[list[float] | None]:
        return [None] * len(texts)
