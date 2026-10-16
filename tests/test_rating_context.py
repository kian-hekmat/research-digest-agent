"""Phase 3: topic descriptions and reference-rated few-shot anchors for the
relevance rating."""
import json
import math
from datetime import datetime, timezone

import httpx
import pytest
import respx

from app import models
from app.config import Settings, get_settings
from app.import_relevance_labels import import_labels
from app.models import EMBEDDING_DIM
from app.services import summarize as summarize_module
from app.services.retrieval import embed_missing, rating_anchors
from app.services.summarize import RatingAnchor, RatingContext, Summarizer
from app.temporal import activities as temporal_activities

OLLAMA_CHAT = "http://ollama.test:11434/api/chat"
MODEL = "fake-embed"


# ---------- prompts ----------
def _summarizer():
    return Summarizer(Settings(summary_backend="ollama", ollama_base_url="http://ollama.test:11434"))


def _sent(route):
    return json.loads(route.calls.last.request.content)["messages"]


CONTEXT = RatingContext(
    description="Structure-preserving integrators; not pure PDE theory.",
    anchors=(RatingAnchor("Core Paper", "About integrators " * 100, 9), RatingAnchor("Side Paper", "Mentions it.", 2)),
)


@respx.mock
def test_rate_relevance_with_context_shows_description_and_anchors():
    route = respx.post(OLLAMA_CHAT).mock(return_value=httpx.Response(200, json={"message": {"content": "7"}}))

    assert _summarizer().rate_relevance("T", "A.", "Numerical Analysis", "cat:math.NA", rating_context=CONTEXT) == 7

    system, user = _sent(route)
    assert system["content"] == summarize_module._RELEVANCE_SYSTEM + summarize_module._ANCHORS_NOTE
    assert user["content"].startswith(
        "Reader's topic: Numerical Analysis (arXiv search: cat:math.NA)\n"
        "What the reader wants from this topic: Structure-preserving integrators; not pure PDE theory.\n\n"
        "Papers the reader has already rated for this topic:\n- Rated 9: Core Paper. About integrators"
    )
    assert "\n- Rated 2: Side Paper. Mentions it.\n\nTitle: T\n\nAbstract: A." in user["content"]
    assert len(user["content"]) < 900  # anchor abstracts are clipped


@respx.mock
def test_description_alone_adds_no_anchor_note():
    route = respx.post(OLLAMA_CHAT).mock(return_value=httpx.Response(200, json={"message": {"content": "5"}}))

    _summarizer().rate_relevance("T", "A.", "PINNs", "pinns", rating_context=RatingContext(description="Methods."))

    system, user = _sent(route)
    assert system["content"] == summarize_module._RELEVANCE_SYSTEM
    assert user["content"] == "Reader's topic: PINNs\nWhat the reader wants from this topic: Methods.\n\nTitle: T\n\nAbstract: A."


@pytest.mark.parametrize("context", [None, RatingContext()])
@respx.mock
def test_without_context_both_prompts_are_unchanged(context):
    route = respx.post(OLLAMA_CHAT).mock(
        return_value=httpx.Response(200, json={"message": {"content": "Relevance: 5\n\nS."}})
    )
    s = _summarizer()

    s.summarize_and_rate("T", "A.", "PINNs", "pinns", rating_context=context)
    system, user = _sent(route)
    assert system["content"] == summarize_module._RATED_SUMMARY_SYSTEM
    assert user["content"] == "Reader's topic: PINNs\n\nTitle: T\n\nAbstract: A."

    s.rate_relevance("T", "A.", "PINNs", "pinns", rating_context=context)
    system, user = _sent(route)
    assert system["content"] == summarize_module._RELEVANCE_SYSTEM


@respx.mock
def test_summarize_and_rate_gets_the_context_too():
    route = respx.post(OLLAMA_CHAT).mock(
        return_value=httpx.Response(200, json={"message": {"content": "Relevance: 8\n\nS."}})
    )

    out = _summarizer().summarize_and_rate("T", "A.", "PINNs", "pinns", rating_context=CONTEXT)

    assert out.relevance == 8
    system, user = _sent(route)
    assert system["content"].endswith(summarize_module._ANCHORS_NOTE)
    assert "Rated 9: Core Paper" in user["content"]


# ---------- anchor retrieval ----------
def _vec(angle):
    v = [0.0] * EMBEDDING_DIM
    v[0], v[1] = math.cos(math.radians(angle)), math.sin(math.radians(angle))
    return v


class AngleEmbedder:
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


def _linked(db, topic, arxiv_id, angle, reference=None, llm=None, day=1):
    p = models.Paper(arxiv_id=arxiv_id, title=f"Paper {arxiv_id}", abstract=f"angle:{angle}",
                     published_at=datetime(2026, 9, day, tzinfo=timezone.utc))
    db.add(p)
    db.flush()
    db.execute(models.topic_paper_association.insert().values(
        topic_id=topic.id, paper_id=p.id, relevance=llm, reference_relevance=reference,
        reference_labeler="test" if reference else None,
    ))
    db.commit()
    return p


def test_anchors_span_the_bands_nearest_first_and_skip_llm_only_ratings(db_session, topic):
    q = _linked(db_session, topic, "q", 0)
    for aid, angle, ref in [("hi-near", 5, 9), ("hi-mid", 20, 10), ("hi-far", 60, 8),
                            ("mid", 40, 5), ("lo", 70, 2)]:
        _linked(db_session, topic, aid, angle, reference=ref)
    _linked(db_session, topic, "llm-only", 1, llm=10)  # LLM ratings are never anchors
    embed_missing(db_session, AngleEmbedder())

    anchors = rating_anchors(db_session, topic.id, q.id, model=MODEL, per_band=2)

    assert [a.arxiv_id for a in anchors] == ["hi-mid", "hi-near", "mid", "lo"]
    assert {a.arxiv_id for a in anchors if a.rating >= 8} == {"hi-near", "hi-mid"}
    assert anchors[1].similarity == pytest.approx(math.cos(math.radians(5)), abs=1e-4)


def test_anchors_never_include_the_paper_or_excluded_ones(db_session, topic):
    q = _linked(db_session, topic, "q", 0, reference=9)
    other = _linked(db_session, topic, "other", 5, reference=9)
    keep = _linked(db_session, topic, "keep", 30, reference=9)
    embed_missing(db_session, AngleEmbedder())

    anchors = rating_anchors(db_session, topic.id, q.id, model=MODEL, exclude_paper_ids=(other.id,))

    assert [a.arxiv_id for a in anchors] == ["keep"]


def test_anchors_accept_uuid_objects_as_well_as_strings(db_session, topic):
    import uuid

    q = _linked(db_session, topic, "q", 0)
    other = _linked(db_session, topic, "other", 5, reference=9)
    keep = _linked(db_session, topic, "keep", 30, reference=9)
    embed_missing(db_session, AngleEmbedder())

    anchors = rating_anchors(
        db_session, topic.id, uuid.UUID(q.id), model=MODEL, exclude_paper_ids=(uuid.UUID(other.id),)
    )

    assert [a.arxiv_id for a in anchors] == ["keep"]


def test_anchors_without_an_embedding_fall_back_to_newest(db_session, topic):
    q = _linked(db_session, topic, "q", 0)
    _linked(db_session, topic, "old", 5, reference=9, day=1)
    _linked(db_session, topic, "new", 50, reference=9, day=20)

    anchors = rating_anchors(db_session, topic.id, q.id, model=MODEL, per_band=1)

    assert [(a.arxiv_id, a.similarity) for a in anchors] == [("new", None)]


def test_anchors_only_from_this_topic(db_session, topic):
    other_topic = models.Topic(name="RLHF", query="rlhf")
    db_session.add(other_topic)
    db_session.commit()
    q = _linked(db_session, topic, "q", 0)
    _linked(db_session, other_topic, "elsewhere", 1, reference=9)

    assert rating_anchors(db_session, topic.id, q.id, model=MODEL) == []


# ---------- label import ----------
def test_import_labels_stores_linked_valid_rows_and_counts_the_rest(db_session, topic):
    p = _linked(db_session, topic, "2609.00001", 0)
    _linked(db_session, topic, "2609.00002", 0)
    rows = [
        {"topic": "PINNs", "arxiv_id": "2609.00001", "label": "8", "labeler": "human"},
        {"topic": "PINNs", "arxiv_id": "2609.00002", "label": "", "labeler": ""},  # unlabeled
        {"topic": "PINNs", "arxiv_id": "2609.99999", "label": "5", "labeler": "human"},  # unknown paper
        {"topic": "Nope", "arxiv_id": "2609.00001", "label": "5", "labeler": "human"},  # unknown topic
    ]

    assert import_labels(db_session, rows) == (1, 3)

    t = models.topic_paper_association
    row = db_session.execute(
        t.select().where(t.c.topic_id == topic.id, t.c.paper_id == p.id)
    ).one()
    assert (row.reference_relevance, row.reference_labeler) == (8, "human")


# ---------- the activities ----------
class RecordingRater:
    contexts: list = []

    def rate_relevance(self, title, abstract, topic_name, topic_query, rating_context=None):
        type(self).contexts.append(rating_context)
        return 6


def test_rate_activity_passes_description_and_reference_anchors(db_session, topic, monkeypatch):
    monkeypatch.setattr(get_settings(), "embedding_model", MODEL)
    monkeypatch.setattr(get_settings(), "rag_rating_anchors", True)
    monkeypatch.setattr(temporal_activities, "Summarizer", RecordingRater)
    RecordingRater.contexts = []
    topic.description = "Training methods for PINNs."
    db_session.commit()
    q = _linked(db_session, topic, "q", 0)
    _linked(db_session, topic, "anchor", 5, reference=9)
    embed_missing(db_session, AngleEmbedder())

    temporal_activities.rate_relevance(
        temporal_activities.SummarizePaperInput(
            paper_id=q.id, title="t", abstract="a", topic_id=topic.id, topic_name="PINNs", topic_query="pinns"
        )
    )

    [context] = RecordingRater.contexts
    assert context.description == "Training methods for PINNs."
    assert [(a.title, a.rating) for a in context.anchors] == [("Paper anchor", 9)]


def test_anchors_can_be_switched_off(db_session, topic, monkeypatch):
    monkeypatch.setattr(get_settings(), "rag_rating_anchors", False)
    monkeypatch.setattr(temporal_activities, "Summarizer", RecordingRater)
    RecordingRater.contexts = []
    q = _linked(db_session, topic, "q", 0)
    _linked(db_session, topic, "anchor", 5, reference=9)

    temporal_activities.rate_relevance(
        temporal_activities.SummarizePaperInput(paper_id=q.id, title="t", abstract="a", topic_id=topic.id)
    )

    assert RecordingRater.contexts[0].anchors == ()


# ---------- API ----------
def test_create_topic_with_description(client):
    resp = client.post("/topics", json={"name": "NA", "query": "cat:math.NA", "description": "Integrators."})
    assert resp.status_code == 201
    assert resp.json()["description"] == "Integrators."


def test_patch_topic_description_sets_and_clears(client):
    topic_id = client.post("/topics", json={"name": "NA", "query": "cat:math.NA"}).json()["id"]

    set_resp = client.patch(f"/topics/{topic_id}", json={"description": "Integrators."})
    assert set_resp.status_code == 200 and set_resp.json()["description"] == "Integrators."

    untouched = client.patch(f"/topics/{topic_id}", json={})
    assert untouched.json()["description"] == "Integrators."

    cleared = client.patch(f"/topics/{topic_id}", json={"description": None})
    assert cleared.json()["description"] is None


def test_patch_missing_topic_is_404(client):
    assert client.patch("/topics/does-not-exist", json={"description": "x"}).status_code == 404
