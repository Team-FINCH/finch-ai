"""한국어 전문 검색을 위한 바이그램(2음절 셔글) 토큰화.

Postgres 내장 텍스트 검색은 형태소를 모른다 — 'simple' 설정은 공백만 본다.
한국어는 조사·어미를 붙여 쓰므로 문장을 그대로 넣으면 문장 하나가 통째로
한 토큰이 되어 아무것도 매치되지 않는다. 글자 단위 2-셔글로 잘라 공백으로
이어 붙이면 부분 일치 기반의 어휘 랭킹(ts_rank_cd)을 확보한다.

pg_bigm 같은 외부 확장 없이 내장 tsvector · GIN 만으로 돌리기 위한 선택이다.
저장 시엔 `to_tsv_text`, 질의 시엔 `lexical_tsquery` 를 쓴다.
"""

from __future__ import annotations

import re

from sqlalchemy import func, literal_column

# 토큰에 들어갈 문자만 남긴다. 문장부호·특수문자는 셔글 경계가 아니라 소거 대상.
_KEEP = re.compile(r"[^0-9A-Za-z가-힣ㄱ-ㅣ\s]")

# 질의 하나가 만들 수 있는 셔글 상한. 긴 붙여넣기 질의가 to_tsquery 를
# 비대하게 만드는 것을 막는다. 어휘 랭킹에는 앞쪽이면 충분하다.
_QUERY_TERMS_MAX = 64


def bigrams(text: str) -> list[str]:
    """글자 단위 2-셔글 목록. 한 글자 어절은 그대로 하나의 토큰으로 남긴다."""
    cleaned = _KEEP.sub(" ", text)
    out: list[str] = []
    for word in cleaned.split():
        if len(word) == 1:
            out.append(word)
            continue
        out.extend(word[i : i + 2] for i in range(len(word) - 1))
    return out


def to_tsv_text(text: str) -> str:
    """청크 저장용 — 셔글 중복을 제거해 공백으로 이어붙인다.

    순서를 버리는 건 저장 컬럼이 어차피 집합 기반(GIN)이기 때문이다.
    """
    seen: dict[str, None] = {}
    for gram in bigrams(text):
        seen.setdefault(gram)
    return " ".join(seen)


def lexical_tsquery(text: str) -> str:
    """질의 변환 — OR 로 이은 to_tsquery 리터럴. 빈 입력이면 빈 문자열.

    AND(websearch 기본)로 잠그면 셔글 하나만 어긋나도 전부 놓친다. 어휘 팔은
    넓게 잡고 랭킹(ts_rank_cd)이 좁히는 구조가 맞다. 순서를 보존하는 이유는
    인접 셔글이 같이 맞았을 때 rank 가 올라 구절 신호가 살아있기 때문이다.
    """
    grams = bigrams(text)[:_QUERY_TERMS_MAX]
    if not grams:
        return ""
    return " | ".join("'" + g.replace("'", "") + "'" for g in grams)


def weighted_tsvector(title: str, text: str):
    """제목(A)과 본문(D)을 가중치가 다른 하나의 tsvector로 만든다."""
    title_vector = func.setweight(
        func.to_tsvector("simple", to_tsv_text(title)), literal_column("'A'")
    )
    body_vector = func.setweight(
        func.to_tsvector("simple", to_tsv_text(text)), literal_column("'D'")
    )
    return title_vector.op("||")(body_vector)
