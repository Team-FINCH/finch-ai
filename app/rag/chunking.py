"""본문 청킹.

임베딩 단위로 자른다. 문단 경계를 우선 지키고, 한 문단이 너무 길면 그때만 자른다.
문장 중간에서 끊으면 검색 결과 스니펫이 근거로 읽히지 않는다 (api-spec §2.4).
"""

from __future__ import annotations

import re

CHUNK_SIZE = 1000
OVERLAP = 150

_BLANKS = re.compile(r"[ \t 　]+")
_NEWLINES = re.compile(r"\n{2,}")


def normalize(text: str) -> str:
    """공시 원문의 들쭉날쭉한 공백을 정리한다. 문단 구분은 남긴다."""
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = _BLANKS.sub(" ", text)
    lines = [line.strip() for line in text.split("\n")]
    return _NEWLINES.sub("\n\n", "\n".join(lines)).strip()


def _window(paragraph: str, size: int, overlap: int) -> list[str]:
    """문단 하나가 size를 넘을 때만 쓴다. overlap만큼 겹쳐 문맥이 끊기지 않게 한다."""
    step = max(1, size - overlap)
    return [paragraph[i : i + size] for i in range(0, len(paragraph), step)]


def chunk(text: str, size: int = CHUNK_SIZE, overlap: int = OVERLAP) -> list[str]:
    """정규화한 본문을 size 이하 조각으로 나눈다. 빈 입력이면 빈 목록."""
    if overlap >= size:
        raise ValueError(f"overlap({overlap})은 size({size})보다 작아야 한다")

    body = normalize(text)
    if not body:
        return []

    chunks: list[str] = []
    buffer = ""
    for paragraph in body.split("\n\n"):
        if len(paragraph) > size:
            if buffer:
                chunks.append(buffer)
                buffer = ""
            chunks.extend(_window(paragraph, size, overlap))
            continue

        candidate = f"{buffer}\n\n{paragraph}" if buffer else paragraph
        if len(candidate) > size:
            chunks.append(buffer)
            buffer = paragraph
        else:
            buffer = candidate

    if buffer:
        chunks.append(buffer)
    return chunks
