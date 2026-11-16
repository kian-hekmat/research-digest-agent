"""Phase 2 retrieval: signals (novelty, near duplicates, the similar
earlier paper) and how emails show them."""
import math
from datetime import datetime, timezone

import pytest

from app import crud, models
from app.config import get_settings
from app.models import EMBEDDING_DIM
from app.services.email import DigestForEmail, PaperForEmail, build_topic_section, render_digest_email
from app.services.retrieval import compute_signals, embed_missing, neighbours, related_context
from app.services.summarize import RatedSummary
from app.temporal import activities as temporal_activities
from tests.conftest import EmbedderTextMixin

MODEL = "fake-embed"


# ---------- retrieval signals ----------
def _vec(angle_deg):
    v = [0.0] * EMBEDDING_DIM
    v[0], v[1] = math.cos(math.radians(angle_deg)), math.sin(math.radians(angle_deg))
    return v


class AngleEmbedder(EmbedderTextMixin):
    enabled = True
    model = MODEL

    def embed(self, texts):
        return [_vec(float(t.rsplit("angle:", 1)[1])) for t in texts]


@pytest.fixture
def topic(db_session):
    t = models.Topic(name="PINNs", query="pinns")
    db_session.add(t)
    db_session.commit()
    return t


def _paper(db, arxiv_id, angle, day, topic=None, summary=None):
    p = models.Paper(
        arxiv_id=arxiv_id,
        title=f"Paper {arxiv_id}",
        abstract=f"angle:{angle}",
        summary=summary,
        published_at=datetime(2026, 9, day, tzinfo=timezone.utc),
    )
    if topic is not None:
        p.topics.append(topic)
    db.add(p)
    db.commit()
    return p


def _signals(db, ids=None):
    compute_signals(db, ids, model=MODEL, near_duplicate_similarity=0.9, novelty_window_days=90)


def test_unlinked_papers_are_never_retrieved(db_session, topic):
    q = _paper(db_session, "q", 0, 20, topic)
    _paper(db_session, "orphan", 1, 1)  # unlinked: off-topic history
    _paper(db_session, "linked", 30, 2, topic)
    embed_missing(db_session, AngleEmbedder())

    found = related_context(db_session, q.id, model=MODEL, k=4, min_similarity=0.0)

    assert [n.arxiv_id for n in found] == ["linked"]


def test_within_days_window(db_session, topic):
    q = _paper(db_session, "q", 0, 28, topic)
    _paper(db_session, "recent", 30, 20, topic)
    _paper(db_session, "old", 1, 1, topic)
    embed_missing(db_session, AngleEmbedder())

    found = neighbours(db_session, q.id, model=MODEL, published_before=True, within_days=10)

    assert [n.arxiv_id for n in found] == ["recent"]


def test_prior_similarity_is_the_closest_earlier_paper(db_session, topic):
    first = _paper(db_session, "first", 0, 1, topic)
    second = _paper(db_session, "second", 60, 5, topic)
    later = _paper(db_session, "later", 55, 9, topic)  # later: never "prior" to second
    embed_missing(db_session, AngleEmbedder())

    _signals(db_session)

    db_session.expire_all()
    assert db_session.get(models.Paper, first.id).prior_similarity is None
    assert db_session.get(models.Paper, second.id).prior_similarity == pytest.approx(math.cos(math.radians(60)), abs=1e-4)
    assert db_session.get(models.Paper, later.id).prior_similarity == pytest.approx(math.cos(math.radians(5)), abs=1e-4)


def test_near_duplicate_points_from_the_later_paper_to_the_earlier(db_session, topic):
    original = _paper(db_session, "2609.00001", 0, 1, topic)
    dup = _paper(db_session, "2609.00002", 5, 3, topic)  # cos 5deg = 0.996
    other = _paper(db_session, "2609.00003", 40, 4, topic)
    embed_missing(db_session, AngleEmbedder())

    _signals(db_session)

    db_session.expire_all()
    assert db_session.get(models.Paper, dup.id).near_duplicate_of_id == original.id
    assert db_session.get(models.Paper, original.id).near_duplicate_of_id is None
    assert db_session.get(models.Paper, other.id).near_duplicate_of_id is None


def test_similar_link_is_the_closest_earlier_paper_above_the_floor(db_session, topic):
    first = _paper(db_session, "first", 0, 1, topic)
    close = _paper(db_session, "close", 30, 5, topic)  # cos 30 = 0.87 to first
    far = _paper(db_session, "far", 80, 9, topic)  # cos 50 = 0.64 to close: below 0.75
    embed_missing(db_session, AngleEmbedder())

    compute_signals(db_session, None, model=MODEL, near_duplicate_similarity=0.95,
                    novelty_window_days=90, similar_paper_similarity=0.75)

    db_session.expire_all()
    assert [p.id for p in db_session.get(models.Paper, close.id).related_papers] == [first.id]
    assert db_session.get(models.Paper, far.id).related_papers == []
    assert db_session.get(models.Paper, first.id).related_papers == []


def test_set_paper_related_replaces_previous_citations(db_session, topic):
    a, b, c = (_paper(db_session, x, 0, 1, topic) for x in ("a", "b", "c"))
    crud.set_paper_related(db_session, a.id, [(b.id, 0.7)])
    crud.set_paper_related(db_session, a.id, [(c.id, 0.8)])
    db_session.commit()
    db_session.expire_all()

    assert [p.arxiv_id for p in db_session.get(models.Paper, a.id).related_papers] == ["c"]


# ---------- the summarize activity ----------
class RecordingSummarizer:
    def __init__(self, *args, **kwargs):  # activities pass ledger= and topic_id=
        pass

    reply: RatedSummary | None = None

    def summarize_and_rate(self, title, abstract, topic_name, topic_query, rating_context=None):
        return type(self).reply


def test_summarize_activity_stores_the_summary_and_the_similar_link_stays(db_session, topic, monkeypatch):
    """embed_papers records the closest earlier paper; summarizing in prep
    stores the summary and leaves that link alone."""
    settings = get_settings()
    monkeypatch.setattr(settings, "embedding_model", MODEL)
    monkeypatch.setattr(temporal_activities, "Summarizer", RecordingSummarizer)
    earlier = _paper(db_session, "2609.00001", 0, 1, topic)
    new = _paper(db_session, "2609.00009", 10, 9, topic)
    embed_missing(db_session, AngleEmbedder())
    _signals(db_session)
    RecordingSummarizer.reply = RatedSummary(summary="S.", relevance=5)

    temporal_activities.summarize_for_edition(
        temporal_activities.SummarizePaperInput(paper_id=new.id, title="t", abstract="a", topic_id=topic.id)
    )

    db_session.expire_all()
    stored = db_session.get(models.Paper, new.id)
    assert stored.summary == "S."
    assert [p.id for p in stored.related_papers] == [earlier.id]


# ---------- email ----------
def _ep(arxiv_id, day=1, **kw):
    return PaperForEmail(
        title=f"Title {arxiv_id}", summary="S.", arxiv_id=arxiv_id,
        published_at=datetime(2026, 9, day, 14, tzinfo=timezone.utc), **kw,
    )


def _digest(papers):
    return DigestForEmail(generated_at=datetime(2026, 9, 30, tzinfo=timezone.utc), overview=None, papers=papers)


def test_near_duplicates_fold_under_their_original_and_skip_the_cap():
    papers = [
        _ep("2609.00001", relevance=9),
        _ep("2609.00002", relevance=9, near_duplicate_of="2609.00001"),
        _ep("2609.00003", relevance=9, near_duplicate_of="2609.00002"),  # chain
        _ep("2609.00004", relevance=5),
    ]

    section = build_topic_section("PINNs", [_digest(papers)], max_papers=2)

    assert [p.arxiv_id for p in section.papers] == ["2609.00001", "2609.00004"]
    assert [d.arxiv_id for d in section.papers[0].see_also] == ["2609.00002", "2609.00003"]
    assert section.omitted == 0
    text = render_digest_email([section]).text_body
    assert "Also (near-duplicate): Title 2609.00002 https://arxiv.org/abs/2609.00002" in text


def test_near_duplicate_of_a_paper_not_in_the_email_is_shown_normally():
    section = build_topic_section("PINNs", [_digest([_ep("2609.00002", near_duplicate_of="2609.00001")])])
    assert [p.arxiv_id for p in section.papers] == ["2609.00002"]


def test_similar_earlier_paper_is_shown():
    section = build_topic_section(
        "PINNs", [_digest([_ep("2609.00002", similar=[("2609.00001", "Old <Paper>")])])]
    )

    content = render_digest_email([section])

    assert "  Similar earlier paper: Old <Paper> https://arxiv.org/abs/2609.00001" in content.text_body
    assert (
        '<em>Similar earlier paper:</em> <a href="https://arxiv.org/abs/2609.00001">Old &lt;Paper&gt;</a>'
    ) in content.html_body
