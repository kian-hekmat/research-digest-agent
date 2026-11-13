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


@pytest.mark.parametrize(
    "fields",
    [
        dict(novelty_repeat=0.5, novelty_unrelated=0.5),   # zero span: division by zero in ranking
        dict(novelty_repeat=0.4, novelty_unrelated=0.6),   # inverted: novelty silently flipped
        dict(similar_paper=0.8, near_duplicate=0.7),       # a near duplicate must be at least "similar"
        dict(similar_paper=1.5),                           # cosine is within [-1, 1]
    ],
)
def test_inconsistent_calibration_is_rejected(fields):
    from pydantic import ValidationError

    base = dict(similar_paper=0.75, near_duplicate=0.9, related_min=0.72, novelty_repeat=0.85, novelty_unrelated=0.45)
    with pytest.raises(ValidationError):
        Calibration(**{**base, **fields})


def test_worker_refuses_to_start_without_a_calibration_for_its_model():
    # A mistyped EMBEDDING_MODEL must fail at `docker-compose up`, not block
    # every email at the next send - even with embeddings off, ranking needs it.
    from app.worker import check_config

    with pytest.raises(SystemExit, match="calibration"):
        check_config(Settings(embedding_model="typo-model", summary_backend="none"))
    check_config(Settings(embedding_model="embeddinggemma"))  # fine


def test_text_embedding_3_small_is_calibrated_from_the_live_corpus():
    c = Settings(embedding_model="text-embedding-3-small").calibration
    assert (c.similar_paper, c.near_duplicate, c.related_min, c.novelty_repeat, c.novelty_unrelated) == (
        0.749, 0.92, 0.721, 0.838, 0.459,
    )
    # Plain embedding similarity can't tell "RLHF" from its expansion
    # (docs/rag-plan.md §5.9): suggestions stay off for this model.
    assert c.topic_suggestion is None
