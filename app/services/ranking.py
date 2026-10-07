"""Which papers make the cut when a subscription caps papers per email.

A paper's score combines three signals, each scaled to [0, 1]:

- relevance (60%) - how central the paper is to the topic, rated 1-10 by the
  LLM in the same call that writes its summary. A phrase-match search returns
  papers that are squarely about the topic and papers that mention it once,
  and nothing else in the pipeline tells them apart.
- author standing (40%) - the highest h-index among the paper's authors, from
  Semantic Scholar, log-scaled. The max rather than the mean: most strong
  papers are led by students with an established senior author, and a mean
  punishes exactly that; it also limits the damage of one author being
  mis-matched to a namesake's profile. Log-scaled so the difference between
  h=2 and h=20 counts for more than between h=60 and h=80.
- venue (up to +8%) - a bonus when the arXiv comment or journal-ref says the
  paper was accepted somewhere; half that for a workshop. A small bonus, not
  a weight: most papers in a fresh digest haven't been accepted anywhere yet,
  and that says little about them.

A missing signal (no LLM configured, a paper too new for Semantic Scholar,
a failed lookup) is imputed as the median of the papers that do have it, so
it neither sinks nor lifts the paper. Ties - including every paper when no
signal is available at all - fall back to newest first, the old behavior.

Pure functions, no I/O: independently testable, and safe to call from
`build_topic_section`.
"""
from __future__ import annotations

import math
import re
import statistics
from typing import Protocol, Sequence

RELEVANCE_WEIGHT = 0.6
H_INDEX_WEIGHT = 0.4
VENUE_BONUS = 0.08
WORKSHOP_VENUE_FRACTION = 0.5

# h-index at which the author term saturates at 1.0. log1p-scaled, so
# h=5 -> 0.39, h=10 -> 0.52, h=30 -> 0.74, h=60 -> 0.89.
H_INDEX_SATURATION = 100

RELEVANCE_MIN, RELEVANCE_MAX = 1, 10

_ACCEPTED = re.compile(
    r"\b(accepted|to appear|to be published|published in|camera[- ]ready)\b",
    re.IGNORECASE,
)
_WORKSHOP = re.compile(r"\bworkshop\b", re.IGNORECASE)


class Rankable(Protocol):
    relevance: int | None
    max_author_h_index: int | None
    venue_score: float


def venue_score(comment: str | None, journal_ref: str | None) -> float:
    """1.0 for a journal-ref or an accepted-at comment, 0.5 if the acceptance
    is a workshop, else 0.0. "Submitted to X" deliberately doesn't count."""
    if journal_ref and journal_ref.strip():
        return 1.0
    if not comment or not _ACCEPTED.search(comment):
        return 0.0
    return WORKSHOP_VENUE_FRACTION if _WORKSHOP.search(comment) else 1.0


def relevance_component(relevance: int | None) -> float | None:
    if relevance is None:
        return None
    clamped = min(max(relevance, RELEVANCE_MIN), RELEVANCE_MAX)
    return (clamped - RELEVANCE_MIN) / (RELEVANCE_MAX - RELEVANCE_MIN)


def h_index_component(h_index: int | None) -> float | None:
    if h_index is None:
        return None
    return min(math.log1p(max(h_index, 0)) / math.log1p(H_INDEX_SATURATION), 1.0)


def _impute(values: list[float | None]) -> list[float]:
    known = [v for v in values if v is not None]
    fill = statistics.median(known) if known else 0.5
    return [fill if v is None else v for v in values]


def score_papers(papers: Sequence[Rankable]) -> list[float]:
    """One score per paper, same order. Scores are only comparable within one
    call - missing signals are imputed from the batch's own median."""
    relevance = _impute([relevance_component(p.relevance) for p in papers])
    h_index = _impute([h_index_component(p.max_author_h_index) for p in papers])
    return [
        RELEVANCE_WEIGHT * r + H_INDEX_WEIGHT * h + VENUE_BONUS * p.venue_score
        for p, r, h in zip(papers, relevance, h_index)
    ]
