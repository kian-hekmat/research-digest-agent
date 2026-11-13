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


def test_ndcg_at_k():
    assert metrics.ndcg_at_k([3, 2, 1, 0], 4) == pytest.approx(1.0)
    worst = metrics.ndcg_at_k([0, 1, 2, 3], 4)
    assert 0 < worst < 1
    # Only the top k count: a perfect top 2 scores 1 whatever follows.
    assert metrics.ndcg_at_k([3, 2, 0, 1], 2) == pytest.approx(1.0)
    assert metrics.ndcg_at_k([0, 0], 2) is None


def test_numbers_in_reads_latex_thousands_separators():
    assert metrics.numbers_in("uses $15{,}000$ points") == {"15000"}


def test_quantile_map_carries_a_percentile_across_samples():
    old = [i / 100 for i in range(101)]          # 0.00 .. 1.00
    new = [0.2 + 0.6 * i / 100 for i in range(101)]  # 0.20 .. 0.80
    # 0.9 is the 90th percentile of `old`; the 90th percentile of `new` is 0.74
    assert metrics.quantile_map(0.9, old, new) == pytest.approx(0.74)
    assert metrics.quantile_map(0.5, old, new) == pytest.approx(0.5)


def test_quantile_map_clamps_outside_the_old_range():
    old, new = [0.3, 0.4, 0.5], [0.1, 0.2, 0.3]
    assert metrics.quantile_map(0.9, old, new) == 0.3
    assert metrics.quantile_map(0.0, old, new) == 0.1


def test_quantile_map_needs_samples():
    with pytest.raises(ValueError):
        metrics.quantile_map(0.5, [], [0.1])


def test_separating_threshold_midpoint_when_separable():
    t, overlap = metrics.separating_threshold(same=[0.8, 0.9], different=[0.3, 0.5])
    assert t == pytest.approx(0.65) and overlap is False


def test_separating_threshold_best_f1_when_overlapping():
    t, overlap = metrics.separating_threshold(same=[0.6, 0.8, 0.9], different=[0.3, 0.7])
    assert overlap is True
    # At 0.6: all 3 same pairs, 1 false positive (0.7) -> F1 0.857, beating
    # 0.8 (F1 0.8). A wrong suggestion only costs a click, so recall wins.
    assert t == pytest.approx(0.6)
