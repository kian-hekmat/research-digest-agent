"""Phase 4: history-aware digest overviews."""
import json
from datetime import datetime, timedelta, timezone

import httpx
import respx

from app import crud, models
from app.config import Settings, get_settings
from app.services import summarize as summarize_module
from app.services.summarize import OverviewHistory, Summarizer
from app.temporal import activities as temporal_activities
from app.temporal.types import WriteOverviewInput

OLLAMA_CHAT = "http://ollama.test:11434/api/chat"
T0 = datetime(2026, 10, 1, 10, tzinfo=timezone.utc)


def _summarizer():
    return Summarizer(Settings(summary_backend="ollama", ollama_base_url="http://ollama.test:11434"))


def _reply():
    return httpx.Response(200, json={"message": {"content": "An overview."}})


# ---------- prompt ----------
@respx.mock
def test_overview_without_history_is_the_pre_retrieval_prompt():
    route = respx.post(OLLAMA_CHAT).mock(return_value=_reply())

    _summarizer().write_overview("PINNs", ["S1.", "S2."], history=OverviewHistory())

    system, user = json.loads(route.calls.last.request.content)["messages"]
    assert system["content"] == summarize_module._OVERVIEW_SYSTEM
    assert user["content"] == "Topic: PINNs\n\nPaper summaries:\n1. S1.\n\n2. S2."


@respx.mock
def test_avoid_repeats_shows_previous_overviews_as_points_not_to_repeat():
    route = respx.post(OLLAMA_CHAT).mock(return_value=_reply())
    history = OverviewHistory(previous=(("Oct 1", "Old overview."),), style="avoid_repeats",
                              earlier_papers=(("New Paper", "Old Paper"),))

    _summarizer().write_overview("PINNs", ["S1."], history=history)

    system, user = json.loads(route.calls.last.request.content)["messages"]
    assert system["content"] == summarize_module._OVERVIEW_AVOID_REPEATS_SYSTEM
    assert "Do not mention the previous digests" in system["content"]
    assert user["content"] == (
        "Topic: PINNs\n\nWhat previous digests already said (do not repeat):\n- Old overview.\n\n"
        "Paper summaries:\n1. S1."
    )  # no dates, and no similar-paper pairs to cite


@respx.mock
def test_compare_style_shows_previous_overviews_and_earlier_papers():
    route = respx.post(OLLAMA_CHAT).mock(return_value=_reply())
    history = OverviewHistory(
        previous=(("Oct 1", "Old overview " * 200), ("Oct 3", "Newer overview.")),
        earlier_papers=(("New Paper", "Old Paper"),),
        style="compare",
    )

    _summarizer().write_overview("PINNs", ["S1."], history=history)

    system, user = json.loads(route.calls.last.request.content)["messages"]
    assert system["content"] == summarize_module._OVERVIEW_WITH_HISTORY_SYSTEM
    text = user["content"]
    assert text.startswith("Topic: PINNs\n\nHow this topic's previous digests opened, oldest first:\n[Oct 1] Old overview")
    assert "\n[Oct 3] Newer overview.\n\n" in text
    assert 'New papers that closely resemble earlier ones the reader has seen:\n- "New Paper" resembles "Old Paper"' in text
    assert text.endswith("Paper summaries (new in this digest):\n1. S1.")
    assert len(text) < 1300  # previous overviews are clipped


# ---------- history queries ----------
def _topic(db, name="PINNs"):
    t = models.Topic(name=name, query=name.lower())
    db.add(t)
    db.commit()
    return t


def _digest(db, topic, overview, days, status=models.DigestStatus.completed):
    d = models.Digest(topic_id=topic.id, status=status, overview=overview,
                      completed_at=T0 + timedelta(days=days) if status == models.DigestStatus.completed else None)
    db.add(d)
    db.commit()
    return d


def test_recent_overviews_latest_n_oldest_first_excluding_current(db_session):
    topic, other = _topic(db_session), _topic(db_session, "RLHF")
    for day in range(5):
        _digest(db_session, topic, f"o{day}", day)
    _digest(db_session, topic, None, 6)  # no overview
    _digest(db_session, topic, "failed", 7, status=models.DigestStatus.failed)
    _digest(db_session, other, "other topic", 8)
    current = _digest(db_session, topic, "current", 9)

    got = crud.recent_overviews(db_session, topic.id, exclude_digest_id=current.id, limit=3)

    assert [d.overview for d in got] == ["o2", "o3", "o4"]


def test_similar_earlier_papers_from_paper_related(db_session):
    old = models.Paper(arxiv_id="2609.00001", title="Old Paper")
    new = models.Paper(arxiv_id="2609.00002", title="New Paper")
    lone = models.Paper(arxiv_id="2609.00003", title="Lone Paper")
    db_session.add_all([old, new, lone])
    db_session.commit()
    crud.set_paper_related(db_session, new.id, [(old.id, 0.8)])
    db_session.commit()

    assert crud.similar_earlier_papers(db_session, [new.id, lone.id]) == [("New Paper", "Old Paper")]
    assert crud.similar_earlier_papers(db_session, []) == []


# ---------- activity ----------
class RecordingWriter:
    histories: list = []

    def write_overview(self, topic_name, summaries, history=None):
        type(self).histories.append(history)
        return "o"


def test_write_overview_activity_passes_history(db_session, monkeypatch):
    monkeypatch.setattr(temporal_activities, "Summarizer", RecordingWriter)
    RecordingWriter.histories = []
    topic = _topic(db_session)
    _digest(db_session, topic, "Earlier overview.", 0)
    current = _digest(db_session, topic, None, 1, status=models.DigestStatus.pending)

    temporal_activities.write_overview(
        WriteOverviewInput(topic_name="PINNs", summaries=["S."], topic_id=topic.id, digest_id=current.id)
    )

    [history] = RecordingWriter.histories
    assert history.previous == (("Oct 1", "Earlier overview."),)
    assert history.style == "avoid_repeats" and history.earlier_papers == ()


def test_write_overview_activity_history_can_be_switched_off(db_session, monkeypatch):
    monkeypatch.setattr(get_settings(), "rag_overview_history", False)
    monkeypatch.setattr(temporal_activities, "Summarizer", RecordingWriter)
    RecordingWriter.histories = []
    topic = _topic(db_session)
    _digest(db_session, topic, "Earlier overview.", 0)

    temporal_activities.write_overview(WriteOverviewInput(topic_name="PINNs", summaries=["S."], topic_id=topic.id))

    assert RecordingWriter.histories == [None]


def test_old_style_input_without_topic_gets_no_history(monkeypatch):
    monkeypatch.setattr(temporal_activities, "Summarizer", RecordingWriter)
    RecordingWriter.histories = []

    temporal_activities.write_overview(WriteOverviewInput(topic_name="PINNs", summaries=["S."]))

    assert RecordingWriter.histories == [None]
