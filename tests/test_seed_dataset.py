"""시드 데이터셋 문서가 기계로 읽을 수 있는 상태인지 검사한다.

시세(price_daily) 적재 대상 30종의 유일한 정의는 docs/seed-dataset.md 의 표다.
종목 마스터(instruments)는 전종목이 적재되므로 이 30종과 범위가 다르다.

재현 명령이 표를 sed로 뽑아 ingest.prices --tickers 로 넘기므로, 표 형식이 깨지면
적재가 조용히 엉뚱한 종목 집합으로 돌아간다. 목록을 여기 복사해 두지 않고 문서를
읽어서 불변식만 확인한다.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

AI_ROOT = Path(__file__).resolve().parents[1]
SEED_DOC = AI_ROOT / "docs" / "seed-dataset.md"
SECTORS_TABLE = AI_ROOT / "ingest" / "ksic_sectors.json"

# 재현 절차 2단계의 sed( 's/^| \([0-9]\{6\}\) .*/\1/p' )와 정확히 같은 범위를 잡아야 한다.
# 이 패턴과 ROW 가 어긋나면 문서에 종목코드로 시작하는 다른 표가 생겨도 눈치채지 못한다.
SED_EQUIVALENT = re.compile(r"^\| (\d{6}) ", re.MULTILINE)
ROW = re.compile(r"^\| (\d{6}) \| (.+?) \| (\w+) \| (.+?) \|$", re.MULTILINE)

EXPECTED_COUNT = 30
EXPECTED_SPLIT = {"KOSPI": 20, "KOSDAQ": 10}


@pytest.fixture(scope="module")
def doc() -> str:
    return SEED_DOC.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def rows(doc: str) -> list[tuple[str, str, str, str]]:
    return ROW.findall(doc)


def test_재현명령의_sed가_정확히_30종을_뽑는다(doc: str) -> None:
    """문서 어디에도 종목코드로 시작하는 다른 표가 없어야 한다."""
    assert len(SED_EQUIVALENT.findall(doc)) == EXPECTED_COUNT


def test_sed_추출과_표_파싱이_같은_집합이다(doc: str, rows) -> None:
    assert SED_EQUIVALENT.findall(doc) == [r[0] for r in rows]


def test_종목_수가_30이다(rows) -> None:
    assert len(rows) == EXPECTED_COUNT


def test_종목코드가_6자리_문자열이고_중복이_없다(rows) -> None:
    tickers = [r[0] for r in rows]
    assert len(set(tickers)) == len(tickers)
    # 선행 0이 살아 있어야 한다. 정수로 다루면 005930이 5930이 되어
    # 모델의 char_length(ticker) = 6 제약에서 터진다.
    assert all(isinstance(t, str) and len(t) == 6 and t.isdigit() for t in tickers)
    assert any(t.startswith("0") for t in tickers), "선행 0 종목이 있어야 파싱 버그를 잡는다"


def test_시장이_코스피_20_코스닥_10이다(rows) -> None:
    counts: dict[str, int] = {}
    for _, _, market, _ in rows:
        counts[market] = counts.get(market, 0) + 1
    assert counts == EXPECTED_SPLIT


def test_섹터가_ksic_분류에_있는_이름이다(rows) -> None:
    known = set(json.loads(SECTORS_TABLE.read_text(encoding="utf-8"))["sectors"])
    used = {r[3] for r in rows}
    assert used <= known, f"분류에 없는 섹터: {sorted(used - known)}"
    # 상관·기여도 분석이 의미를 가지려면 한 섹터에 몰리면 안 된다.
    assert len(used) >= 12, f"섹터가 {len(used)}종뿐이다"


def test_종목명이_비어_있지_않고_중복이_없다(rows) -> None:
    names = [r[1].strip() for r in rows]
    assert all(names)
    assert len(set(names)) == len(names)


def test_마스터_전종목과_시세_30종의_구분이_문서에_남아_있다(doc: str) -> None:
    """이 구분이 사라지면 문서를 따라한 사람이 다른 결과를 본다."""
    assert "instruments" in doc and "price_daily" in doc
    assert "전종목" in doc, "종목 마스터가 전종목이라는 서술이 필요하다"
    assert "시세 적재 대상 30종" in doc, "30종이 시세 대상임을 밝히는 절이 필요하다"
