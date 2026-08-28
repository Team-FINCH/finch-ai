"""피드백 집계와 프롬프트 버전 비교."""

from __future__ import annotations

from eval.feedback import FeedbackObservation, aggregate, compare, comparisons, render


def _observation(
    version: str,
    rating: str | None,
    *reasons: str,
    endpoint: str = "stocks.analysis",
) -> FeedbackObservation:
    return FeedbackObservation(
        endpoint=endpoint,
        prompt_version=version,
        rating=rating,
        reasons=tuple(reasons),
        input_tokens=100,
        output_tokens=20,
        latency_ms=500,
    )


def test_응답과_피드백을_프롬프트_버전별로_집계한다() -> None:
    stats = aggregate(
        [
            _observation("prompt_old", "up"),
            _observation("prompt_old", "down", "wrong_number", "outdated"),
            _observation("prompt_old", None),
        ]
    )[0]

    assert stats.responses == 3
    assert stats.feedback == 2
    assert stats.feedback_rate == 2 / 3
    assert stats.approval_rate == 0.5
    assert stats.reasons == {"wrong_number": 1, "outdated": 1}
    assert stats.average_latency_ms == 500


def test_새_프롬프트의_승인율과_오류율_변화를_비교한다() -> None:
    by_version = {
        item.prompt_version: item
        for item in aggregate(
        [
            _observation("prompt_old", "up"),
            _observation("prompt_old", "down", "wrong_number"),
            _observation("prompt_new", "up"),
            _observation("prompt_new", "up"),
        ]
        )
    }

    result = compare(by_version["prompt_old"], by_version["prompt_new"])

    assert result.baseline == "prompt_old"
    assert result.candidate == "prompt_new"
    assert result.approval_delta == 0.5
    assert result.wrong_number_rate_delta == -0.5


def test_같은_엔드포인트에_두_버전이_있을_때만_비교한다() -> None:
    stats = aggregate(
        [
            _observation("prompt_old", "up"),
            _observation("prompt_new", "up"),
            _observation("prompt_new", "up", endpoint="chat"),
        ]
    )

    items = comparisons(stats, "prompt_old", "prompt_new")

    assert [item.endpoint for item in items] == ["stocks.analysis"]
    assert "stocks.analysis" in render(stats)
