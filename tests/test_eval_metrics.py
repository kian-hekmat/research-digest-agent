"""The pure parts of the eval harness (evals/). The harness itself calls the
real model and is never run in CI."""
import pytest

from evals import metrics
from evals.rag import parse_label, stratified_sample


def test_spearman_perfect_and_inverse():
    assert metrics.spearman([1, 2, 3, 4], [10, 20, 30, 40]) == pytest.approx(1.0)
    assert metrics.spearman([1, 2, 3, 4], [4, 3, 2, 1]) == pytest.approx(-1.0)


def test_spearman_ties_use_average_ranks():
    # Ranks of [1, 2, 2, 3] are [1, 2.5, 2.5, 4]; against [1, 2, 3, 4] that's
    # rho = 0.9487 (computed by hand / matches scipy.stats.spearmanr).
    assert metrics.spearman([1, 2, 2, 3], [1, 2, 3, 4]) == pytest.approx(0.9487, abs=1e-4)


def test_spearman_undefined_cases():
    assert metrics.spearman([1, 2], [1, 2]) is None
    assert metrics.spearman([5, 5, 5], [1, 2, 3]) is None
    with pytest.raises(ValueError):
        metrics.spearman([1, 2, 3], [1, 2])


def test_spread_reports_the_7_to_9_pileup():
    s = metrics.spread([8, 8, 9, 7, 2, None])
    assert s["n"] == 5 and s["unrated"] == 1
    assert s["share_7_to_9"] == 0.8
    assert s["histogram"] == {"2": 1, "7": 1, "8": 2, "9": 1}


def test_spread_with_nothing_rated():
    assert metrics.spread([None, None]) == {"n": 0, "unrated": 2}


def test_agreement_drops_incomplete_pairs():
    a = metrics.agreement([8, 7, None, 3], [8, 5, 9, 4])
    assert a["n"] == 3 and a["dropped"] == 1
    assert a["exact"] == pytest.approx(1 / 3, abs=1e-3)
    assert a["within_1"] == pytest.approx(2 / 3, abs=1e-3)
    assert a["mae"] == 1.0


def test_unsupported_numbers_flags_only_numbers_without_a_source():
    abstract = "We reach 92.4% accuracy on 1,000 samples, a 3x speedup over FEM."
    summary = "It reaches 92.4% accuracy on 1000 samples and runs 5 times faster."
    assert metrics.unsupported_numbers(summary, abstract) == {"5"}


def test_numbers_in_ignores_digits_inside_identifiers():
    assert metrics.numbers_in("gemma3 and L2 error in 2 hours") == {"2"}


def test_latency_percentiles():
    lat = metrics.latency([1.0, 2.0, 3.0, 4.0, 10.0])
    assert lat["p50_s"] == 3.0 and lat["p90_s"] == 10.0 and lat["mean_s"] == 4.0
    assert metrics.latency([]) == {"n": 0}


def test_stratified_sample_covers_rare_buckets_and_is_deterministic():
    rows = [(f"hi{i}", f"hi{i}", 8) for i in range(30)]
    rows += [("lo1", "lo1", 2), ("mid1", "mid1", 6), ("none1", "none1", None)]
    picked = stratified_sample(rows, 6, seed=0)
    assert len(picked) == 6
    assert {"lo1", "mid1", "none1"} <= set(picked)
    assert picked == stratified_sample(list(reversed(rows)), 6, seed=0)


def test_stratified_sample_returns_everything_when_short():
    rows = [("a", "a", 8), ("b", "b", None)]
    assert sorted(stratified_sample(rows, 20, seed=0)) == ["a", "b"]


@pytest.mark.parametrize("raw, expected", [("7", 7), (" 10 ", 10), ("0", None), ("11", None), ("", None), ("x", None)])
def test_parse_label(raw, expected):
    assert parse_label(raw) == expected
