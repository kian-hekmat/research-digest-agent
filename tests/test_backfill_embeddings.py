"""The embeddings backfill's --embed-only / --signals-only modes
(app.backfill_embeddings)."""
import math
from datetime import datetime, timezone

import pytest

from app import models
from app.models import EMBEDDING_DIM
from tests.conftest import EmbedderTextMixin


def _vec(angle_deg: float) -> list[float]:
    v = [0.0] * EMBEDDING_DIM
    v[0], v[1] = math.cos(math.radians(angle_deg)), math.sin(math.radians(angle_deg))
    return v


def _topic(db, name, query):
    t = models.Topic(name=name, query=query)
    db.add(t)
    db.commit()
    return t


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

        def embed(self, texts):
            return [_vec(0) for _ in texts]

    settings = get_settings()
    monkeypatch.setattr(settings, "summary_backend", "openai")  # embeddings_enabled
    monkeypatch.setattr(settings, "embedding_model", "fake-embed")
    monkeypatch.setattr(backfill_embeddings, "Embedder", SecondModel)
    return backfill_embeddings


def test_embed_only_backfill_leaves_signals_untouched(db_session, backfill_world):
    before = _signals_snapshot(db_session)

    backfill_world.main(["--embed-only"])

    db_session.expire_all()
    assert db_session.query(models.PaperEmbedding).filter_by(model="fake-embed").count() == 2
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
