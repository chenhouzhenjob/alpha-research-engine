from alpha_metrics.models.composite_score import CompositeScoreResult
from alpha_metrics.scoring import compute_percentile_ranks


def _result(score: float) -> CompositeScoreResult:
    return CompositeScoreResult(score=score, confidence=1.0, tier="B")


def test_compute_percentile_ranks_unavailable_below_two_pools():
    ranks = compute_percentile_ranks({"0xonly": _result(0.5)})
    assert not ranks["0xonly"].is_available

    ranks_empty = compute_percentile_ranks({})
    assert ranks_empty == {}


def test_compute_percentile_ranks_orders_worst_to_best():
    scores = {"0xworst": _result(0.1), "0xmid": _result(0.5), "0xbest": _result(0.9)}
    ranks = compute_percentile_ranks(scores)
    assert ranks["0xworst"].value == 0.0
    assert ranks["0xmid"].value == 0.5
    assert ranks["0xbest"].value == 1.0


def test_compute_percentile_ranks_two_pools_are_zero_and_one():
    scores = {"0xa": _result(0.2), "0xb": _result(0.8)}
    ranks = compute_percentile_ranks(scores)
    assert ranks["0xa"].value == 0.0
    assert ranks["0xb"].value == 1.0
