"""Phase 4: history-aware digest overviews (shown the topic's earlier
overviews only so they aren't repeated)."""
import json
from datetime import datetime, timedelta, timezone

import respx

from app import crud, models
from app.config import get_settings
from app.services import summarize as summarize_module
from app.services.summarize import Summarizer
from app.temporal import activities as temporal_activities
from app.temporal.types import WriteOverviewInput
from tests.conftest import OPENAI_TEST_CHAT, openai_reply, openai_test_settings

T0 = datetime(2026, 10, 1, 10, tzinfo=timezone.utc)


def _summarizer():
    return Summarizer(openai_test_settings())


# ---------- prompt ----------
@respx.mock
def test_overview_without_history_is_the_pre_retrieval_prompt():
    route = respx.post(OPENAI_TEST_CHAT).mock(return_value=openai_reply("An overview."))

    _summarizer().write_overview("PINNs", ["S1.", "S2."], previous=[])

    system, user = json.loads(route.calls.last.request.content)["messages"]
    assert system["content"] == summarize_module._OVERVIEW_SYSTEM
    assert user["content"] == "Topic: PINNs\n\nPaper summaries:\n1. S1.\n\n2. S2."


@respx.mock
def test_avoid_repeats_shows_previous_overviews_as_points_not_to_repeat():
    route = respx.post(OPENAI_TEST_CHAT).mock(return_value=openai_reply("An overview."))

    _summarizer().write_overview("PINNs", ["S1."], previous=["Old overview " * 200, "Newer overview."])

    system, user = json.loads(route.calls.last.request.content)["messages"]
    assert system["content"] == summarize_module._OVERVIEW_AVOID_REPEATS_SYSTEM
    assert "Do not mention the previous digests" in system["content"]
    text = user["content"]
    assert text.startswith("Topic: PINNs\n\nWhat previous digests already said (do not repeat):\n- Old overview")
    assert text.endswith("\n- Newer overview.\n\nPaper summaries:\n1. S1.")
    assert len(text) < 1100  # previous overviews are clipped


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

    assert [text for _, text in got] == ["o2", "o3", "o4"]


# ---------- activity ----------
class RecordingWriter:
    def __init__(self, *args, **kwargs):  # activities pass ledger= and topic_id=
        pass

    histories: list = []

    def write_overview(self, topic_name, summaries, previous=()):
        type(self).histories.append(list(previous))
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

    assert RecordingWriter.histories == [["Earlier overview."]]


def test_write_overview_activity_history_can_be_switched_off(db_session, monkeypatch):
    monkeypatch.setattr(get_settings(), "rag_overview_history", False)
    monkeypatch.setattr(temporal_activities, "Summarizer", RecordingWriter)
    RecordingWriter.histories = []
    topic = _topic(db_session)
    _digest(db_session, topic, "Earlier overview.", 0)

    temporal_activities.write_overview(WriteOverviewInput(topic_name="PINNs", summaries=["S."], topic_id=topic.id))

    assert RecordingWriter.histories == [[]]


def test_old_style_input_without_topic_gets_no_history(monkeypatch):
    monkeypatch.setattr(temporal_activities, "Summarizer", RecordingWriter)
    RecordingWriter.histories = []

    temporal_activities.write_overview(WriteOverviewInput(topic_name="PINNs", summaries=["S."]))

    assert RecordingWriter.histories == [[]]
