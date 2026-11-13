"""Pure metric helpers for the summarization evals - no I/O, unit-tested in
tests/test_eval_metrics.py."""
from __future__ import annotations

import math
import re
import statistics
from collections import Counter
from typing import Sequence

# Numbers as they appear in abstracts and summaries: 3, 0.85, 1,000, 45%.
_NUMBER = re.compile(r"(?<![\w.])\d{1,3}(?:,\d{3})+(?:\.\d+)?|(?<![\w.])\d+(?:\.\d+)?")


def _ranks(values: Sequence[float]) -> list[float]:
    """1-based ranks, ties get the average of the ranks they span."""
    order = sorted(range(len(values)), key=lambda i: values[i])
    ranks = [0.0] * len(values)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and values[order[j + 1]] == values[order[i]]:
            j += 1
        avg = (i + j) / 2 + 1
        for k in range(i, j + 1):
            ranks[order[k]] = avg
        i = j + 1
    return ranks


def spearman(a: Sequence[float], b: Sequence[float]) -> float | None:
    """Spearman's rho with average ranks for ties. None when undefined (fewer
    than 3 pairs, or either side constant)."""
    if len(a) != len(b):
        raise ValueError("sequences differ in length")
    if len(a) < 3:
        return None
    ra, rb = _ranks(a), _ranks(b)
    ma, mb = statistics.fmean(ra), statistics.fmean(rb)
    cov = sum((x - ma) * (y - mb) for x, y in zip(ra, rb))
    var_a = sum((x - ma) ** 2 for x in ra)
    var_b = sum((y - mb) ** 2 for y in rb)
    if var_a == 0 or var_b == 0:
        return None
    return cov / math.sqrt(var_a * var_b)


def spread(ratings: Sequence[int | None]) -> dict:
    """How much a set of 1-10 ratings discriminates. `share_7_to_9` is the
    failure mode seen in production: nearly everything lands there."""
    rated = [r for r in ratings if r is not None]
    if not rated:
        return {"n": 0, "unrated": len(ratings)}
    return {
        "n": len(rated),
        "unrated": len(ratings) - len(rated),
        "mean": round(statistics.fmean(rated), 2),
        "std": round(statistics.pstdev(rated), 2),
        "share_7_to_9": round(sum(7 <= r <= 9 for r in rated) / len(rated), 3),
        "distinct_values": len(set(rated)),
        "histogram": {str(k): v for k, v in sorted(Counter(rated).items())},
    }


def agreement(a: Sequence[int | None], b: Sequence[int | None]) -> dict:
    """Agreement between two ratings of the same items (two LLM runs, or LLM
    vs. human). Pairs with a missing side are dropped and counted."""
    pairs = [(x, y) for x, y in zip(a, b) if x is not None and y is not None]
    if not pairs:
        return {"n": 0, "dropped": len(a)}
    diffs = [abs(x - y) for x, y in pairs]
    rho = spearman([x for x, _ in pairs], [y for _, y in pairs])
    return {
        "n": len(pairs),
        "dropped": len(a) - len(pairs),
        "exact": round(sum(d == 0 for d in diffs) / len(pairs), 3),
        "within_1": round(sum(d <= 1 for d in diffs) / len(pairs), 3),
        "mae": round(statistics.fmean(diffs), 2),
        "spearman": None if rho is None else round(rho, 3),
    }


def numbers_in(text: str) -> set[str]:
    # LaTeX thousands separators ("15{,}000", from arXiv HTML) read as "15,000".
    text = text.replace("{,}", ",")
    return {m.replace(",", "") for m in _NUMBER.findall(text)}


def unsupported_numbers(summary: str, source: str) -> set[str]:
    """Numbers stated in the summary that never appear in its source text -
    a cheap, conservative hallucination check (it can't catch a wrong word,
    only a number with no origin)."""
    return numbers_in(summary) - numbers_in(source)


def percentile(values: Sequence[float], q: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    idx = min(len(ordered) - 1, max(0, math.ceil(q * len(ordered)) - 1))
    return ordered[idx]


def quantile_map(value: float, from_sample: Sequence[float], to_sample: Sequence[float]) -> float:
    """The value at the same quantile of `to_sample` as `value` sits at in
    `from_sample` (both empirical, linearly interpolated). Values outside
    `from_sample`'s range clamp to `to_sample`'s min or max. Used to carry a
    similarity threshold from one embedding model's scale to another's."""
    if not from_sample or not to_sample:
        raise ValueError("quantile_map needs non-empty samples")
    src, dst = sorted(from_sample), sorted(to_sample)
    if value <= src[0]:
        return dst[0]
    if value >= src[-1]:
        return dst[-1]
    # Fractional position of `value` among the sorted source points.
    hi = next(i for i, v in enumerate(src) if v >= value)
    lo = hi - 1
    frac = 0.0 if src[hi] == src[lo] else (value - src[lo]) / (src[hi] - src[lo])
    q = (lo + frac) / (len(src) - 1)
    # Same fractional position among the destination points.
    pos = q * (len(dst) - 1)
    i = min(int(pos), len(dst) - 2) if len(dst) > 1 else 0
    if len(dst) == 1:
        return dst[0]
    return dst[i] + (pos - i) * (dst[i + 1] - dst[i])


def separating_threshold(same: Sequence[float], different: Sequence[float]) -> tuple[float, bool]:
    """A similarity floor that tells `same` pairs from `different` ones.
    Separable: the midpoint between the lowest same and the highest
    different (overlap False). Otherwise the observed value with the best F1
    for "same" (ties to the lower one: a wrong suggestion only costs a
    click), with overlap True."""
    if not same or not different:
        raise ValueError("separating_threshold needs both kinds of pairs")
    if min(same) > max(different):
        return (min(same) + max(different)) / 2, False
    best, best_f1 = None, -1.0
    for t in sorted(set(same) | set(different)):
        tp = sum(s >= t for s in same)
        fp = sum(d >= t for d in different)
        fn = len(same) - tp
        f1 = 0.0 if tp == 0 else 2 * tp / (2 * tp + fp + fn)
        if f1 > best_f1:
            best, best_f1 = t, f1
    return best, True


def latency(seconds: Sequence[float]) -> dict:
    if not seconds:
        return {"n": 0}
    return {
        "n": len(seconds),
        "mean_s": round(statistics.fmean(seconds), 2),
        "p50_s": round(percentile(seconds, 0.5), 2),
        "p90_s": round(percentile(seconds, 0.9), 2),
    }


def ndcg_at_k(gains_in_ranked_order: Sequence[float], k: int) -> float | None:
    """Normalized discounted cumulative gain of the top `k`: 1.0 when the
    ranking puts the highest gains first. None when every gain is 0."""
    def dcg(gains: Sequence[float]) -> float:
        return sum(g / math.log2(i + 2) for i, g in enumerate(gains[:k]))

    ideal = dcg(sorted(gains_in_ranked_order, reverse=True))
    return None if ideal == 0 else dcg(gains_in_ranked_order) / ideal
