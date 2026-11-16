"""Topic descriptions for the relevance rating (and the reference-label
import the ranking eval uses)."""
import json

import pytest
import respx

from app import models
from app.import_relevance_labels import import_labels
from app.services import summarize as summarize_module
from app.services.summarize import RatingContext, Summarizer
from app.temporal import activities as temporal_activities
from tests.conftest import OPENAI_TEST_CHAT, openai_reply, openai_test_settings


# ---------- prompts ----------
def _summarizer():
    return Summarizer(openai_test_settings())


def _sent(route):
    return json.loads(route.calls.last.request.content)["messages"]


CONTEXT = RatingContext(description="Structure-preserving integrators; not pure PDE theory.")


@respx.mock
def test_rate_relevance_with_a_description_shows_it_after_the_topic():
    route = respx.post(OPENAI_TEST_CHAT).mock(return_value=openai_reply("7"))

    assert _summarizer().rate_relevance("T", "A.", "Numerical Analysis", "cat:math.NA", rating_context=CONTEXT) == 7

    system, user = _sent(route)
    assert system["content"] == summarize_module._RELEVANCE_SYSTEM
    assert user["content"] == (
        "Reader's topic: Numerical Analysis (arXiv search: cat:math.NA)\n"
        "What the reader wants from this topic: Structure-preserving integrators; not pure PDE theory."
        "\n\nTitle: T\n\nAbstract: A."
    )


@pytest.mark.parametrize("context", [None, RatingContext()])
@respx.mock
def test_without_context_both_prompts_are_unchanged(context):
    route = respx.post(OPENAI_TEST_CHAT).mock(return_value=openai_reply("Relevance: 5\n\nS."))
    s = _summarizer()

    s.summarize_and_rate("T", "A.", "PINNs", "pinns", rating_context=context)
    system, user = _sent(route)
    assert system["content"] == summarize_module._RATED_SUMMARY_SYSTEM
    assert user["content"] == "Reader's topic: PINNs\n\nTitle: T\n\nAbstract: A."

    s.rate_relevance("T", "A.", "PINNs", "pinns", rating_context=context)
    system, user = _sent(route)
    assert system["content"] == summarize_module._RELEVANCE_SYSTEM


@respx.mock
def test_summarize_and_rate_gets_the_description_too():
    route = respx.post(OPENAI_TEST_CHAT).mock(return_value=openai_reply("Relevance: 8\n\nS."))

    out = _summarizer().summarize_and_rate("T", "A.", "PINNs", "pinns", rating_context=CONTEXT)

    assert out.relevance == 8
    system, user = _sent(route)
    assert system["content"] == summarize_module._RATED_SUMMARY_SYSTEM
    assert "What the reader wants from this topic: Structure-preserving" in user["content"]


@pytest.fixture
def topic(db_session):
    t = models.Topic(name="PINNs", query="pinns")
    db_session.add(t)
    db_session.commit()
    return t


def _linked(db, topic, arxiv_id):
    p = models.Paper(arxiv_id=arxiv_id, title=f"Paper {arxiv_id}", abstract="a")
    db.add(p)
    db.flush()
    db.execute(models.topic_paper_association.insert().values(topic_id=topic.id, paper_id=p.id))
    db.commit()
    return p


# ---------- label import ----------
def test_import_labels_stores_linked_valid_rows_and_counts_the_rest(db_session, topic):
    p = _linked(db_session, topic, "2609.00001")
    _linked(db_session, topic, "2609.00002")
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
    def __init__(self, *args, **kwargs):  # activities pass ledger= and topic_id=
        pass

    contexts: list = []

    def rate_relevance(self, title, abstract, topic_name, topic_query, rating_context=None):
        type(self).contexts.append(rating_context)
        return 6


def test_rate_activity_passes_the_topic_description(db_session, topic, monkeypatch):
    monkeypatch.setattr(temporal_activities, "Summarizer", RecordingRater)
    RecordingRater.contexts = []
    topic.description = "Training methods for PINNs."
    db_session.commit()
    q = _linked(db_session, topic, "q")

    temporal_activities.rate_relevance(
        temporal_activities.SummarizePaperInput(
            paper_id=q.id, title="t", abstract="a", topic_id=topic.id, topic_name="PINNs", topic_query="pinns"
        )
    )

    [context] = RecordingRater.contexts
    assert context.description == "Training methods for PINNs."


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
