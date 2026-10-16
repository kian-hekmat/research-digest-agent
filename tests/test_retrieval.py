"""pgvector storage and nearest-neighbour queries, against the real test DB."""
import math
from datetime import datetime, timezone

import pytest

from app import models
from app.models import EMBEDDING_DIM
from app.services.retrieval import embed_missing, neighbours, papers_missing_embedding

MODEL = "fake-embed"


def _vec(angle_deg: float) -> list[float]:
    """A unit vector in the plane of the first two axes: the cosine
    similarity of two of these is cos(angle difference)."""
    v = [0.0] * EMBEDDING_DIM
    v[0], v[1] = math.cos(math.radians(angle_deg)), math.sin(math.radians(angle_deg))
    return v


class FakeEmbedder:
    """Embeds a paper at the angle given in its abstract ("angle:30")."""

    enabled = True
    model = MODEL

    def __init__(self):
        self.calls: list[list[str]] = []

    def embed(self, texts):
        self.calls.append(texts)
        return [_vec(float(t.rsplit("angle:", 1)[1])) for t in texts]


def _paper(db, arxiv_id, angle, day=1, topic=None, abstract=None):
    paper = models.Paper(
        arxiv_id=arxiv_id,
        title=f"Paper {arxiv_id}",
        abstract=abstract if abstract is not None else f"angle:{angle}",
        published_at=datetime(2026, 9, day, tzinfo=timezone.utc) if day else None,
    )
    if topic is not None:
        paper.topics.append(topic)
    db.add(paper)
    db.commit()
    return paper


def test_vector_round_trips_through_the_column(db_session):
    paper = _paper(db_session, "2609.00001", 0)
    db_session.add(
        models.PaperEmbedding(paper_id=paper.id, model=MODEL, content="x", embedding=_vec(45))
    )
    db_session.commit()
    db_session.expire_all()

    stored = db_session.query(models.PaperEmbedding).one()
    assert stored.embedding == pytest.approx(_vec(45))
    assert (stored.kind, stored.chunk_ix) == ("abstract", 0)


def test_embed_missing_is_idempotent_and_skips_papers_without_abstracts(db_session):
    _paper(db_session, "2609.00001", 0)
    _paper(db_session, "2609.00002", 10)
    _paper(db_session, "2609.00003", 0, abstract="")
    embedder = FakeEmbedder()

    assert embed_missing(db_session, embedder, batch_size=1) == 2
    assert len(embedder.calls) == 2  # one commit per batch
    assert embed_missing(db_session, embedder) == 0
    assert papers_missing_embedding(db_session, MODEL) == []
    content = {e.content for e in db_session.query(models.PaperEmbedding)}
    assert content == {"title: Paper 2609.00001 | text: angle:0",
                       "title: Paper 2609.00002 | text: angle:10"}


def test_embed_missing_limited_to_given_papers(db_session):
    a = _paper(db_session, "2609.00001", 0)
    _paper(db_session, "2609.00002", 10)

    assert embed_missing(db_session, FakeEmbedder(), [a.id]) == 1
    assert embed_missing(db_session, FakeEmbedder(), []) == 0
    assert [p.arxiv_id for p in papers_missing_embedding(db_session, MODEL)] == ["2609.00002"]


def test_embed_missing_tracks_each_model_separately(db_session):
    _paper(db_session, "2609.00001", 0)
    embed_missing(db_session, FakeEmbedder())

    other = FakeEmbedder()
    other.model = "other-model"
    assert embed_missing(db_session, other) == 1


def test_embed_missing_disabled_writes_nothing(db_session):
    _paper(db_session, "2609.00001", 0)
    off = FakeEmbedder()
    off.enabled = False

    assert embed_missing(db_session, off) == 0
    assert db_session.query(models.PaperEmbedding).count() == 0


def _embedded(db_session, specs, topic=None):
    papers = {aid: _paper(db_session, aid, angle, day, topic) for aid, angle, day in specs}
    embed_missing(db_session, FakeEmbedder())
    return papers


def test_neighbours_most_similar_first_never_self(db_session):
    papers = _embedded(db_session, [("q", 0, 10), ("near", 10, 1), ("mid", 40, 2), ("far", 90, 3)])

    found = neighbours(db_session, papers["q"].id, model=MODEL, k=2)

    assert [n.arxiv_id for n in found] == ["near", "mid"]
    assert found[0].similarity == pytest.approx(math.cos(math.radians(10)), abs=1e-4)


def test_neighbours_min_similarity_floor(db_session):
    papers = _embedded(db_session, [("q", 0, 10), ("near", 10, 1), ("far", 80, 2)])

    found = neighbours(db_session, papers["q"].id, model=MODEL, min_similarity=0.9)

    assert [n.arxiv_id for n in found] == ["near"]


def test_neighbours_published_before(db_session):
    papers = _embedded(db_session, [("q", 0, 10), ("later", 5, 20), ("earlier", 30, 1), ("undated", 1, None)])

    found = neighbours(db_session, papers["q"].id, model=MODEL, published_before=True)

    assert [n.arxiv_id for n in found] == ["earlier"]


def test_neighbours_restricted_to_topic(db_session):
    topic = models.Topic(name="PINNs", query="pinns")
    db_session.add(topic)
    db_session.commit()
    papers = _embedded(db_session, [("q", 0, 10), ("in", 40, 1)], topic=topic)
    _embedded(db_session, [("out", 5, 2)])

    found = neighbours(db_session, papers["q"].id, model=MODEL, topic_id=topic.id)

    assert [n.arxiv_id for n in found] == ["in"]


def test_neighbours_of_an_unembedded_paper_is_empty(db_session):
    _embedded(db_session, [("a", 0, 1)])
    lonely = _paper(db_session, "b", 0)

    assert neighbours(db_session, lonely.id, model=MODEL) == []
    assert neighbours(db_session, lonely.id, model="no-such-model") == []
