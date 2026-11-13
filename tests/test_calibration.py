"""Retrieval thresholds are calibrated per embedding model (app.config)."""
import pytest

from app import models
from app.config import Calibration, MissingCalibration, Settings

GEMMA = (0.75, 0.9, 0.72, 0.85, 0.45)


def _tuple(c: Calibration):
    return (c.similar_paper, c.near_duplicate, c.related_min, c.novelty_repeat, c.novelty_unrelated)


def test_embeddinggemma_calibration_is_the_old_numbers():
    c = Settings(embedding_model="embeddinggemma").calibration
    assert _tuple(c) == GEMMA
    assert c.topic_suggestion is None


def test_missing_calibration_names_the_model_and_the_fix():
    with pytest.raises(MissingCalibration, match="uncalibrated-model.*evals.embeddings"):
        Settings(embedding_model="uncalibrated-model").calibration


def test_env_override_merges_per_model(monkeypatch):
    monkeypatch.setenv(
        "RETRIEVAL_CALIBRATION",
        '{"m": {"similar_paper": 0.6, "near_duplicate": 0.95, "related_min": 0.5,'
        ' "novelty_repeat": 0.8, "novelty_unrelated": 0.2, "topic_suggestion": 0.7}}',
    )
    s = Settings(embedding_model="m")
    assert s.calibration.topic_suggestion == 0.7
    assert _tuple(s.retrieval_calibration["embeddinggemma"]) == GEMMA


def test_removed_scalar_threshold_settings_are_gone():
    s = Settings()
    for name in ("rag_similar_paper_similarity", "rag_near_duplicate_similarity", "rag_min_similarity"):
        assert not hasattr(s, name)


def test_embed_papers_with_uncalibrated_model_writes_nothing(db_session, monkeypatch):
    from app.config import get_settings
    from app.temporal import activities
    from app.temporal.types import EmbedPapersInput
    from tests.conftest import EmbedderTextMixin

    calls = []

    class Fake(EmbedderTextMixin):
        enabled = True
        model = "uncalibrated-model"

        def embed(self, texts):
            calls.append(texts)
            return [[1.0] + [0.0] * 767 for _ in texts]

    paper = models.Paper(arxiv_id="2611.0001", title="T", abstract="A")
    db_session.add(paper)
    db_session.commit()
    monkeypatch.setattr(get_settings(), "embedding_model", "uncalibrated-model")
    monkeypatch.setattr(activities, "Embedder", Fake)

    with pytest.raises(MissingCalibration):
        activities.embed_papers(EmbedPapersInput(paper_ids=[paper.id]))

    assert calls == []
    assert db_session.query(models.PaperEmbedding).count() == 0
