"""Send prep (`PrepEditionsWorkflow` / `PrepTopicEditionWorkflow`): one
edition per topic and slot, summaries for the top K papers only, an overview
over them, and an alert when every summary fails.

Also restores the summary-side coverage the ingest tests gave up in Task 3
(partial summary failure, the concurrency cap, history-aware overviews, the
spend ledger on LLM activities, reusing a summary another topic wrote).

Runs on the same time-skipping Temporal harness as test_temporal_workflows.py.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

from app import crud, models
from app import alerts as alerts_module
from app.cadence import SEND_TIME_ZONE
from app.config import get_settings
from app.services.email import DigestForEmail, PaperForEmail, build_topic_section
from app.spend_ledger import DbLedger
from app.temporal import activities as temporal_activities
from app.temporal.types import PrepTopicInput
from app.temporal.workflows import PrepEditionsWorkflow, PrepTopicEditionWorkflow
from tests.conftest import make_subscriber, subscribe
from tests.test_temporal_workflows import (  # noqa: F401 - fixtures used by name
    TASK_QUEUE,
    FakeEmailSender,
    FakeEmbedder,
    FakeSummarizer,
    _make_topic,
    _relevance,
    _reset_fakes,
    temporal_client,
)

OWNER = "owner@example.com"
SLOT = datetime(2026, 11, 16, 8, tzinfo=SEND_TIME_ZONE)


def _subscribed(db_session, topic, *max_papers, email="reader@example.com"):
    """Active subscriptions to `topic`, one per max_papers value, all last
    sent two days ago (so the window starts there)."""
    sent = datetime.now(timezone.utc) - timedelta(days=2)
    for i, cap in enumerate(max_papers or (None,)):
        reader = make_subscriber(db_session, email=f"{i}-{email}")
        subscribe(db_session, topic, reader, max_papers=cap, last_sent_at=sent)


def _window(
    db_session, topic, n, *, relevance=lambda i: i, summary=lambda i: None, prefix="2611", completed_at=None
):
    """n papers P1..Pn ingested for `topic` (an hour ago by default), rated relevance(i)."""
    digest = models.Digest(
        topic_id=topic.id, status=models.DigestStatus.completed,
        completed_at=completed_at or datetime.now(timezone.utc) - timedelta(hours=1),
    )
    db_session.add(digest)
    papers = []
    for i in range(1, n + 1):
        paper = models.Paper(
            arxiv_id=f"{prefix}.{i:05d}", title=f"P{i}", abstract=f"Abstract {i}", summary=summary(i)
        )
        db_session.add(paper)
        topic.papers.append(paper)
        digest.papers.append(paper)
        papers.append(paper)
    db_session.commit()
    for i, paper in enumerate(papers, start=1):
        crud.set_topic_paper_relevance(db_session, topic.id, paper.id, relevance(i))
    db_session.commit()
    return papers


async def _prep(client, topic, slot=SLOT) -> str:
    return await client.execute_workflow(
        PrepTopicEditionWorkflow.run,
        PrepTopicInput(topic_id=topic.id, slot=slot),
        id=f"prep-test-{uuid.uuid4()}",
        task_queue=TASK_QUEUE,
    )


def _edition(db_session, topic, slot=SLOT):
    db_session.expire_all()
    return crud.get_edition(db_session, topic.id, slot)


def _alerts_on(monkeypatch):
    monkeypatch.setattr(get_settings(), "alert_email", OWNER)
    monkeypatch.setattr(alerts_module, "EmailSender", FakeEmailSender)


# ---------- PrepTopicEditionWorkflow ----------
async def test_prep_summarizes_only_the_top_k(db_session, temporal_client):
    topic = _make_topic(db_session)
    _subscribed(db_session, topic, 5, 7)  # K = 7
    _window(db_session, topic, 12, relevance=lambda i: max(1, i - 2))  # P12 = 10, ..., P1-P3 = 1

    assert await _prep(temporal_client, topic) == "written"

    assert sorted(FakeSummarizer.summarized) == sorted(f"P{i}" for i in range(6, 13))
    edition = _edition(db_session, topic)
    assert edition.status == "ready" and edition.k == 7
    assert edition.overview == "Overview of RLHF: 7 papers."
    titles = [p.title for p in edition.papers]
    assert len(titles) == 12 and titles[:9] == [f"P{i}" for i in range(12, 3, -1)]
    assert FakeSummarizer.overview_histories != []


async def test_prep_reuses_a_summary_another_topic_wrote(db_session, temporal_client):
    topic = _make_topic(db_session)
    _subscribed(db_session, topic, 5)
    _window(db_session, topic, 5, summary=lambda i: "Already summarized." if i == 5 else None)

    await _prep(temporal_client, topic)

    assert sorted(FakeSummarizer.summarized) == ["P1", "P2", "P3", "P4"]
    assert _edition(db_session, topic).overview == "Overview of RLHF: 5 papers."


async def test_prep_keeps_the_ingest_rating(db_session, temporal_client):
    topic = _make_topic(db_session)
    _subscribed(db_session, topic, 5)
    _window(db_session, topic, 1, relevance=lambda i: 9)
    FakeSummarizer.relevance = {"P1": 2}  # what summarize_and_rate would rate

    await _prep(temporal_client, topic)

    db_session.expire_all()
    assert _relevance(db_session, topic, "P1") == 9
    assert FakeSummarizer.summarized == ["P1"]


async def test_prep_rerates_papers_left_unrated(db_session, temporal_client):
    topic = _make_topic(db_session)
    _subscribed(db_session, topic, 5)
    _window(db_session, topic, 2, relevance=lambda i: None if i == 1 else 6)
    FakeSummarizer.relevance = {"P1": 8}

    await _prep(temporal_client, topic)

    db_session.expire_all()
    assert _relevance(db_session, topic, "P1") == 8
    assert [t for t, _ in FakeSummarizer.rated if t == "P1"]
    assert [p.title for p in _edition(db_session, topic).papers] == ["P1", "P2"]


async def test_prep_for_the_same_slot_twice_is_a_noop(db_session, temporal_client):
    topic = _make_topic(db_session)
    _subscribed(db_session, topic, 5)
    _window(db_session, topic, 3)

    assert await _prep(temporal_client, topic) == "written"
    first = _edition(db_session, topic)
    FakeSummarizer.summarized = []
    FakeSummarizer.overview_histories = []

    assert await _prep(temporal_client, topic) == "exists"

    assert FakeSummarizer.summarized == []
    assert FakeSummarizer.overview_histories == []
    again = _edition(db_session, topic)
    assert again.id == first.id
    assert db_session.query(models.TopicEdition).filter_by(topic_id=topic.id).count() == 1


async def test_prep_with_an_empty_window_stores_nothing(db_session, temporal_client):
    topic = _make_topic(db_session)
    _subscribed(db_session, topic, 5)

    assert await _prep(temporal_client, topic) == "empty"

    assert _edition(db_session, topic) is None
    assert FakeSummarizer.summarized == []


async def test_prep_fetches_full_text_only_for_top_k_when_enabled(db_session, temporal_client, monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "rag_fulltext", True)
    monkeypatch.setattr(settings, "summary_backend", "ollama")  # embeddings need it on
    monkeypatch.setattr(temporal_activities, "Embedder", FakeEmbedder)
    monkeypatch.setattr(temporal_activities, "fulltext_excerpts", lambda *a, **k: [])
    fetched = []
    monkeypatch.setattr(
        temporal_activities, "index_fulltext",
        lambda db, embedder, paper, fetch=None: fetched.append(paper.title) or "ok",
    )
    topic = _make_topic(db_session)
    _subscribed(db_session, topic, 5)
    _window(db_session, topic, 8)

    await _prep(temporal_client, topic)

    assert sorted(fetched) == sorted(f"P{i}" for i in range(4, 9))


async def test_prep_makes_no_full_text_request_when_off(db_session, temporal_client, monkeypatch):
    monkeypatch.setattr(get_settings(), "rag_fulltext", False)
    fetched = []
    monkeypatch.setattr(
        temporal_activities, "index_fulltext",
        lambda db, embedder, paper, fetch=None: fetched.append(paper.title) or "ok",
    )
    topic = _make_topic(db_session)
    _subscribed(db_session, topic, 5)
    _window(db_session, topic, 3)

    await _prep(temporal_client, topic)

    assert fetched == []


async def test_prep_alerts_when_every_summary_fails(db_session, temporal_client, monkeypatch):
    _alerts_on(monkeypatch)
    topic = _make_topic(db_session)
    _subscribed(db_session, topic, 5)
    _window(db_session, topic, 2)
    FakeSummarizer.fail_titles = {"P1", "P2"}

    assert await _prep(temporal_client, topic) == "written"

    db_session.expire_all()
    keys = [a.key for a in db_session.query(models.SentAlert).all()]
    assert len(keys) == 1 and keys[0].startswith("summaries-failing:")
    assert [to for to, _ in FakeEmailSender.sent] == [OWNER]
    edition = _edition(db_session, topic)
    assert edition.overview is None  # nothing to write it from
    assert [p.title for p in edition.papers] == ["P2", "P1"]


async def test_prep_tolerates_partial_summary_failure(db_session, temporal_client, monkeypatch):
    _alerts_on(monkeypatch)
    topic = _make_topic(db_session)
    _subscribed(db_session, topic, 5)
    _window(db_session, topic, 2)
    FakeSummarizer.fail_titles = {"P1"}

    assert await _prep(temporal_client, topic) == "written"

    db_session.expire_all()
    by_title = {p.title: p for p in _edition(db_session, topic).papers}
    assert by_title["P2"].summary == "Summary of P2."
    assert by_title["P1"].summary is None
    assert _edition(db_session, topic).overview == "Overview of RLHF: 1 papers."
    assert db_session.query(models.SentAlert).count() == 0


async def test_prep_caps_concurrent_summaries(db_session, temporal_client, monkeypatch):
    monkeypatch.setattr(get_settings(), "llm_max_concurrency", 3)
    FakeSummarizer.delay = 0.05
    topic = _make_topic(db_session)
    _subscribed(db_session, topic, 10)
    _window(db_session, topic, 10)

    await _prep(temporal_client, topic)

    assert len(FakeSummarizer.summarized) == 10
    assert 1 < FakeSummarizer.max_in_flight <= 3


async def test_prep_llm_activities_pass_a_spend_ledger_and_the_topic(db_session, temporal_client):
    topic = _make_topic(db_session)
    _subscribed(db_session, topic, 5)
    _window(db_session, topic, 1)

    await _prep(temporal_client, topic)

    # One summary and one overview, each with the ledger and this topic.
    assert len(FakeSummarizer.constructed) == 2
    for kwargs in FakeSummarizer.constructed:
        assert isinstance(kwargs["ledger"], DbLedger)
        assert kwargs["topic_id"] == topic.id


async def test_second_edition_overview_sees_the_first(db_session, temporal_client):
    topic = _make_topic(db_session)
    _subscribed(db_session, topic, 5)
    _window(db_session, topic, 2)
    await _prep(temporal_client, topic)
    first_end = _edition(db_session, topic).window_end
    _window(db_session, topic, 2, prefix="2612", completed_at=first_end + timedelta(microseconds=1))
    later = SLOT + timedelta(days=3)

    await _prep(temporal_client, topic, slot=later)

    first, second = FakeSummarizer.overview_histories
    assert first is None or first.previous == ()
    assert [text for _, text in second.previous] == ["Overview of RLHF: 2 papers."]
    assert _edition(db_session, topic, later).window_start == _edition(db_session, topic).window_end


# ---------- PrepEditionsWorkflow ----------
async def test_prep_fans_out_in_batches(db_session, temporal_client, monkeypatch):
    monkeypatch.setattr(get_settings(), "fanout_batch_size", 2)
    reader = make_subscriber(db_session)
    sent = datetime.now(timezone.utc) - timedelta(days=2)
    topics = []
    for i in range(5):
        topic = _make_topic(db_session, name=f"Topic {i}", query=f"topic {i}")
        subscribe(db_session, topic, reader, last_sent_at=sent)
        _window(db_session, topic, 1, prefix=f"26{i:02d}")
        topics.append(topic)
    _make_topic(db_session, name="Nobody", query="nobody")  # no subscriptions: no edition
    FakeSummarizer.delay = 0.05

    written = await temporal_client.execute_workflow(
        PrepEditionsWorkflow.run, SLOT.isoformat(), id=f"prep-all-{uuid.uuid4()}", task_queue=TASK_QUEUE
    )

    assert written == 5
    assert all(_edition(db_session, t) is not None for t in topics)
    assert db_session.query(models.TopicEdition).count() == 5
    assert FakeSummarizer.max_in_flight <= 2


# ---------- email ordering ----------
def _ep(arxiv_id, relevance, day=1):
    return PaperForEmail(
        title=arxiv_id, summary=None, arxiv_id=arxiv_id, relevance=relevance,
        published_at=datetime(2026, 11, day, tzinfo=timezone.utc),
    )


def test_build_topic_section_respects_a_given_order():
    papers = [_ep("a", 10), _ep("b", 5), _ep("c", 1)]
    digest = DigestForEmail(generated_at=datetime(2026, 11, 2, tzinfo=timezone.utc), overview=None, papers=papers)

    section = build_topic_section("T", [digest], max_papers=2, order=["c", "b", "a"])

    assert [p.arxiv_id for p in section.papers] == ["c", "b"]
    assert section.omitted == 1


# ---------- overview history ----------
def test_overview_history_spans_editions_and_old_digests(db_session):
    topic = _make_topic(db_session)
    base = datetime(2026, 11, 1, tzinfo=timezone.utc)
    db_session.add(models.Digest(
        topic_id=topic.id, status=models.DigestStatus.completed, overview="old digest", completed_at=base,
    ))
    db_session.commit()
    for days, overview in ((3, "edition 1"), (6, None), (10, "edition 2")):
        crud.save_edition(
            db_session, topic_id=topic.id, slot=base + timedelta(days=days),
            window_start=base, window_end=base + timedelta(days=days), status="ready", k=5,
            overview=overview, ranked_paper_ids=[],
        )

    got = crud.recent_overviews(db_session, topic.id, exclude_digest_id=None, limit=3)

    assert [text for _, text in got] == ["old digest", "edition 1", "edition 2"]
    assert crud.recent_overviews(db_session, topic.id, exclude_digest_id=None, limit=1)[0][1] == "edition 2"
