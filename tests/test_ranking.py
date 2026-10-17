import math

import pytest

from app.services.ranking import (
    GATED,
    H_INDEX_WEIGHT,
    NOVELTY_WEIGHT,
    RELEVANCE_WEIGHT,
    VENUE_BONUS,
    h_index_component,
    novelty_component,
    relevance_gate_component,
    relevance_component,
    score_papers,
    venue_score,
)
from app.services.email import PaperForEmail


def _p(relevance=None, h=None, venue=0.0, prior=None):
    return PaperForEmail(
        title="t", summary=None, arxiv_id="x",
        relevance=relevance, max_author_h_index=h, venue_score=venue, prior_similarity=prior,
    )


@pytest.mark.parametrize(
    "comment, journal_ref, expected",
    [
        ("Accepted at NeurIPS 2026. 37 pages", None, 1.0),
        ("18 pages, accepted by IEEE S&P 2027", None, 1.0),
        ("16 pages, 9 figures, to be published in IEEE TVCG", None, 1.0),
        ("Camera-ready version for ICML 2026", None, 1.0),
        ("Accepted to the NeurIPS 2026 Meta-Agents Workshop", None, 0.5),
        ("Submitted to ICLR 2027", None, 0.0),
        ("12 pages, 4 figures", None, 0.0),
        (None, "Phys. Rev. D 110, 044012 (2026)", 1.0),
        (None, None, 0.0),
    ],
)
def test_venue_score(comment, journal_ref, expected):
    assert venue_score(comment, journal_ref) == expected


def test_relevance_component_spans_zero_to_one_and_clamps():
    assert relevance_component(1) == 0.0
    assert relevance_component(10) == 1.0
    assert relevance_component(15) == 1.0
    assert relevance_component(None) is None


def test_h_index_component_is_log_scaled_and_saturates():
    assert h_index_component(0) == 0.0
    assert h_index_component(100) == 1.0
    assert h_index_component(500) == 1.0
    # Log scale: going 2 -> 20 is worth more than 60 -> 80.
    assert h_index_component(20) - h_index_component(2) > h_index_component(80) - h_index_component(60)
    assert h_index_component(None) is None


def test_weights_combine_as_specified():
    [score] = score_papers([_p(relevance=10, h=100, venue=1.0, prior=0.2)])
    assert math.isclose(score, RELEVANCE_WEIGHT + H_INDEX_WEIGHT + NOVELTY_WEIGHT + VENUE_BONUS)
    assert (RELEVANCE_WEIGHT, H_INDEX_WEIGHT, NOVELTY_WEIGHT, VENUE_BONUS) == (0.6, 0.3, 0.1, 0.08)


def test_novelty_component_maps_similarity_linearly_and_clamps():
    assert novelty_component(0.85) == 0.0
    assert novelty_component(0.95) == 0.0
    assert novelty_component(0.45) == 1.0
    assert novelty_component(0.10) == 1.0
    assert math.isclose(novelty_component(0.65), 0.5)
    assert novelty_component(None) is None


def test_novelty_breaks_a_tie_but_never_beats_relevance():
    novel, repeat = score_papers([_p(relevance=8, h=10, prior=0.5), _p(relevance=8, h=10, prior=0.84)])
    assert novel > repeat
    central_repeat, tangential_novel = score_papers([_p(relevance=9, h=10, prior=0.85), _p(relevance=6, h=10, prior=0.3)])
    assert central_repeat > tangential_novel


def test_missing_signal_is_imputed_from_the_batch_median():
    """A paper too new for Semantic Scholar lands mid-pack on that term,
    neither at the bottom (h=0) nor the top."""
    known = [_p(relevance=5, h=h) for h in (2, 10, 50)]
    unknown = _p(relevance=5, h=None)

    scores = score_papers(known + [unknown])

    assert math.isclose(scores[3], scores[1])  # same as the median (h=10)


def test_no_signals_at_all_scores_every_paper_the_same():
    scores = score_papers([_p(), _p(), _p()])
    assert len(set(scores)) == 1


def test_relevance_outweighs_author_standing():
    """60/30: a central paper by unknown authors beats a tangential one by
    famous authors."""
    central_unknown, tangential_famous = score_papers([_p(relevance=10, h=1), _p(relevance=3, h=80)])
    assert central_unknown > tangential_famous


def test_relevance_gate_component():
    assert [relevance_gate_component(r) for r in (1, 5, 6, 10)] == [0.0, 0.0, 1.0, 1.0]
    assert relevance_gate_component(None) is None


def test_gated_on_topic_always_beats_off_topic_and_ignores_the_grade():
    on_weak, off_strong = score_papers([_p(relevance=6, h=1, prior=0.85), _p(relevance=5, h=100, prior=0.2, venue=1.0)], GATED)
    assert on_weak > off_strong
    seven, ten = score_papers([_p(relevance=7, h=50), _p(relevance=10, h=50)], GATED)
    assert seven == ten


def test_unknown_relevance_mode_is_rejected():
    with pytest.raises(ValueError):
        score_papers([_p()], "fuzzy")
