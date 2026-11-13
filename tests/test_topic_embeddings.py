"""Topic-query embeddings and the similar-topic lookup (app.services.retrieval),
and the backfill's --embed-only / --signals-only modes."""
import math
from datetime import datetime, timezone

import pytest

from app import models
from app.models import EMBEDDING_DIM
from app.services.retrieval import embed_missing_topics, similar_topics
from tests.conftest import EmbedderTextMixin

MODEL = "fake-embed"

# Each query text sits at a fixed angle: cosine similarity of two topics is
# cos(angle difference).
ANGLES = {"rlhf": 0, "reinforcement learning from human feedback": 10, "dpo": 40, "pinns": 90}


def _vec(angle_deg: float) -> list[float]:
    v = [0.0] * EMBEDDING_DIM
    v[0], v[1] = math.cos(math.radians(angle_deg)), math.sin(math.radians(angle_deg))
    return v


class TopicEmbedder(EmbedderTextMixin):
    """OpenAI-style: query text is the bare query."""

    enabled = True
    model = MODEL

    def __init__(self, enabled: bool = True) -> None:
        self.enabled = enabled
        self.calls: list[list[str]] = []

    def query_text(self, query):
        return query

    def embed(self, texts):
        if not self.enabled:
            return None
        self.calls.append(texts)
        return [_vec(ANGLES[t]) for t in texts]


def _topic(db, name, query):
    t = models.Topic(name=name, query=query)
    db.add(t)
    db.commit()
    return t


def test_embed_missing_topics_embeds_each_topic_once_per_model(db_session):
    _topic(db_session, "RLHF", "rlhf")
    _topic(db_session, "PINNs", "pinns")
    e = TopicEmbedder()

    assert embed_missing_topics(db_session, e) == 2
    assert embed_missing_topics(db_session, e) == 0
    assert len(e.calls) == 1
    assert db_session.query(models.TopicEmbedding).count() == 2


def test_topic_embedding_text_is_the_normalized_query(db_session):
    t = _topic(db_session, "RLHF", ' "RLHF" ')
    embed_missing_topics(db_session, TopicEmbedder())
    row = db_session.query(models.TopicEmbedding).one()
    assert (row.topic_id, row.model, row.content) == (t.id, MODEL, "rlhf")


def test_embed_missing_topics_can_be_limited_to_some_topics(db_session):
    a = _topic(db_session, "RLHF", "rlhf")
    _topic(db_session, "PINNs", "pinns")
    assert embed_missing_topics(db_session, TopicEmbedder(), [a.id]) == 1
    assert db_session.query(models.TopicEmbedding).one().topic_id == a.id


def test_similar_topics_ranks_and_filters_by_floor(db_session):
    for name, query in [("RLHF", "rlhf"), ("DPO", "dpo"), ("PINNs", "pinns")]:
        _topic(db_session, name, query)
    e = TopicEmbedder()
    embed_missing_topics(db_session, e)

    # "reinforcement learning from human feedback" is at 10deg: rlhf cos10 ~ 0.98,
    # dpo cos30 ~ 0.87, pinns cos80 ~ 0.17
    matches = similar_topics(
        db_session, e, "Reinforcement Learning  from Human Feedback", min_similarity=0.8
    )

    assert [m.name for m in matches] == ["RLHF", "DPO"]
    assert matches[0].similarity == pytest.approx(math.cos(math.radians(10)), abs=1e-6)
    assert matches[0].normalized_query == "rlhf"
    assert e.calls[-1] == ["reinforcement learning from human feedback"]  # normalized before embedding


def test_similar_topics_respects_k(db_session):
    for name, query in [("RLHF", "rlhf"), ("DPO", "dpo")]:
        _topic(db_session, name, query)
    e = TopicEmbedder()
    embed_missing_topics(db_session, e)
    assert len(similar_topics(db_session, e, "rlhf", min_similarity=0.0, k=1)) == 1


def test_similar_topics_disabled_embedder_returns_empty(db_session):
    _topic(db_session, "RLHF", "rlhf")
    embed_missing_topics(db_session, TopicEmbedder())
    assert similar_topics(db_session, TopicEmbedder(enabled=False), "rlhf", min_similarity=0.0) == []


def test_deleting_a_topic_deletes_its_embedding(db_session):
    t = _topic(db_session, "RLHF", "rlhf")
    embed_missing_topics(db_session, TopicEmbedder())
    db_session.delete(t)
    db_session.commit()
    assert db_session.query(models.TopicEmbedding).count() == 0


# ---------- backfill modes ----------
def _signals_snapshot(db):
    papers = [(p.arxiv_id, p.prior_similarity, p.near_duplicate_of_id) for p in db.query(models.Paper).order_by(models.Paper.arxiv_id)]
    related = sorted(
        (r.paper_id, r.related_paper_id, r.similarity)
        for r in db.execute(models.paper_related_association.select())
    )
    return papers, related


@pytest.fixture
def backfill_world(db_session, monkeypatch):
    """Two papers embedded under the old model, with stored signals; the
    backfill runs with a fake embedder for a second model."""
    from app import backfill_embeddings
    from app.config import get_settings

    topic = _topic(db_session, "RLHF", "rlhf")
    a = models.Paper(arxiv_id="2601.00001", title="A", abstract="angle a", prior_similarity=0.33,
                     published_at=datetime(2026, 1, 1, tzinfo=timezone.utc))
    b = models.Paper(arxiv_id="2601.00002", title="B", abstract="angle b", prior_similarity=0.66,
                     published_at=datetime(2026, 1, 5, tzinfo=timezone.utc))
    db_session.add_all([a, b])
    db_session.commit()
    topic.papers.extend([a, b])
    db_session.commit()

    class SecondModel(EmbedderTextMixin):
        enabled = True
        model = "fake-embed"

        def query_text(self, query):
            return query

        def embed(self, texts):
            return [_vec(0) for _ in texts]

    settings = get_settings()
    monkeypatch.setattr(settings, "summary_backend", "ollama")  # embeddings_enabled
    monkeypatch.setattr(settings, "embedding_model", "fake-embed")
    monkeypatch.setattr(backfill_embeddings, "Embedder", SecondModel)
    return backfill_embeddings


def test_embed_only_backfill_leaves_signals_untouched(db_session, backfill_world):
    before = _signals_snapshot(db_session)

    backfill_world.main(["--embed-only"])

    db_session.expire_all()
    assert db_session.query(models.PaperEmbedding).filter_by(model="fake-embed").count() == 2
    assert db_session.query(models.TopicEmbedding).count() == 1
    assert _signals_snapshot(db_session) == before


def test_signals_only_backfill_recomputes_and_embeds_nothing(db_session, backfill_world):
    backfill_world.main(["--embed-only"])
    db_session.expire_all()
    embedded = db_session.query(models.PaperEmbedding).count()

    backfill_world.main(["--signals-only"])

    db_session.expire_all()
    assert db_session.query(models.PaperEmbedding).count() == embedded
    # Identical vectors: the later paper's prior similarity becomes ~1.0
    later = db_session.query(models.Paper).filter_by(arxiv_id="2601.00002").one()
    assert later.prior_similarity == pytest.approx(1.0, abs=1e-6)


def test_backfill_flags_are_exclusive(backfill_world):
    with pytest.raises(SystemExit):
        backfill_world.main(["--embed-only", "--signals-only"])
