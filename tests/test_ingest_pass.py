"""The daily ingest (`DigestWorkflow`) after Phase 2c: fetch, embed and rate
only - no summaries, overview or full text, which move to send prep - plus
the arXiv queue that keeps one request in flight, the batched fan-out and
the alert when every rating in a run fails.

Runs on the same time-skipping Temporal harness as test_temporal_workflows.py.
"""
from __future__ import annotations

import threading
import time
from datetime import datetime
from pathlib import Path

from app import crud, models
from app import alerts as alerts_module
from app.cadence import SEND_TIME_ZONE
from app.config import Settings, get_settings
from app.services import arxiv as arxiv_service
from app.temporal import activities as temporal_activities
from app.temporal.types import PipelineAlertInput
from app.temporal.workflows import RunAllTopicDigestsWorkflow
from tests.conftest import make_result, make_subscriber, subscribe
from tests.test_temporal_workflows import (  # noqa: F401 - fixtures used by name
    TASK_QUEUE,
    FakeEmailSender,
    FakeEmbedder,
    FakeSummarizer,
    _make_topic,
    _relevance,
    _reset_fakes,
    _run_digest,
    temporal_client,
)

OWNER = "owner@example.com"


def _three_papers(query, since=None):
    return [make_result(f"2411.000{i}", f"Paper {i}") for i in (1, 2, 3)]


def _alerts_on(monkeypatch):
    """Alerts email OWNER through the fake sender."""
    monkeypatch.setattr(get_settings(), "alert_email", OWNER)
    monkeypatch.setattr(alerts_module, "EmailSender", FakeEmailSender)


def _sent_alert_keys(db_session) -> list[str]:
    db_session.expire_all()
    return [a.key for a in db_session.query(models.SentAlert).all()]


def _eastern_date() -> str:
    return f"{datetime.now(SEND_TIME_ZONE):%Y-%m-%d}"


async def test_ingest_rates_and_embeds_but_writes_no_summary_or_overview(
    db_session, temporal_client, monkeypatch
):
    settings = get_settings()
    # Full text and summaries switched on, to show ingest skips them anyway.
    monkeypatch.setattr(settings, "rag_fulltext", True)
    monkeypatch.setattr(settings, "summary_backend", "ollama")
    monkeypatch.setattr(temporal_activities, "Embedder", FakeEmbedder)
    monkeypatch.setattr(FakeEmbedder, "fail", False)
    fulltext_fetched = []
    monkeypatch.setattr(
        temporal_activities, "index_fulltext",
        lambda db, embedder, paper, fetch=None: fulltext_fetched.append(paper.arxiv_id) or "ok",
    )
    topic = _make_topic(db_session)
    # Another topic already summarized Paper Two: it's rated the same way.
    db_session.add(
        models.Paper(
            arxiv_id="2408.0002", title="Paper Two", abstract="Abstract for Paper Two",
            summary="Existing summary.",
        )
    )
    db_session.commit()
    monkeypatch.setattr(
        temporal_activities, "search_arxiv",
        lambda query, since=None: [make_result("2408.0001", "Paper One"), make_result("2408.0002", "Paper Two")],
    )
    FakeSummarizer.relevance = {"Paper One": 9, "Paper Two": 3}
    digest = crud.create_digest(db_session, topic.id)

    await _run_digest(temporal_client, digest.id)

    assert FakeSummarizer.summarized == []
    assert FakeSummarizer.overview_histories == []  # write_overview never called
    assert sorted(FakeSummarizer.rated) == [("Paper One", "RLHF"), ("Paper Two", "RLHF")]
    assert fulltext_fetched == []
    db_session.expire_all()
    refreshed = db_session.get(models.Digest, digest.id)
    assert refreshed.status == models.DigestStatus.completed
    assert refreshed.overview is None
    assert refreshed.error is None
    by_title = {p.title: p for p in refreshed.papers}
    assert by_title["Paper One"].summary is None
    assert by_title["Paper Two"].summary == "Existing summary."
    assert _relevance(db_session, topic, "Paper One") == 9
    assert _relevance(db_session, topic, "Paper Two") == 3
    assert db_session.query(models.PaperEmbedding).count() == 2
    assert db_session.get(models.Topic, topic.id).last_checked_at is not None


async def test_ingest_alerts_once_when_every_rating_fails(db_session, temporal_client, monkeypatch):
    _alerts_on(monkeypatch)
    monkeypatch.setattr(temporal_activities, "search_arxiv", _three_papers)
    FakeSummarizer.fail_titles = {"Paper 1", "Paper 2", "Paper 3"}
    # Two topics fail in the same run (one run, because the time-skipping
    # server can jump its clock years ahead between separate workflows): the
    # second one's alert has the same dated key, so it isn't sent again.
    topics = _subscribed_topics(db_session, 2)
    before = _eastern_date()

    await temporal_client.execute_workflow(
        RunAllTopicDigestsWorkflow.run, id="run-all-ratings-failing", task_queue=TASK_QUEUE
    )

    assert _sent_alert_keys(db_session) in (
        [f"ratings-failing:{before}"], [f"ratings-failing:{_eastern_date()}"],
    )
    [(to, content)] = FakeEmailSender.sent
    assert to == OWNER
    assert "rating failed for Paper" in content.text_body  # the first error's message
    for topic in topics:
        [digest] = db_session.get(models.Topic, topic.id).digests
        assert digest.status == models.DigestStatus.completed  # ingest still completes
        assert digest.error == "3 of 3 ratings failed"
        assert db_session.get(models.Topic, topic.id).last_checked_at is not None


async def test_ingest_budget_skipped_ratings_are_not_failures(db_session, temporal_client, monkeypatch):
    _alerts_on(monkeypatch)
    monkeypatch.setattr(temporal_activities, "search_arxiv", _three_papers)
    FakeSummarizer.relevance = {"Paper 1": None, "Paper 2": None, "Paper 3": None}
    topic = _make_topic(db_session)
    digest = crud.create_digest(db_session, topic.id)

    await _run_digest(temporal_client, digest.id)

    assert len(FakeSummarizer.rated) == 3
    assert _sent_alert_keys(db_session) == []
    assert FakeEmailSender.sent == []
    assert db_session.get(models.Digest, digest.id).error is None


async def test_ingest_does_not_alert_below_three_ratings(db_session, temporal_client, monkeypatch):
    _alerts_on(monkeypatch)
    monkeypatch.setattr(
        temporal_activities, "search_arxiv",
        lambda query, since=None: [make_result("1", "Paper 1"), make_result("2", "Paper 2")],
    )
    FakeSummarizer.fail_titles = {"Paper 1", "Paper 2"}
    topic = _make_topic(db_session)

    await _run_digest(temporal_client, crud.create_digest(db_session, topic.id).id)

    assert _sent_alert_keys(db_session) == []
    assert FakeEmailSender.sent == []


def _subscribed_topics(db_session, n: int) -> list[models.Topic]:
    reader = make_subscriber(db_session)
    topics = []
    for i in range(n):
        topic = _make_topic(db_session, name=f"Topic {i}", query=f"topic {i}")
        subscribe(db_session, topic, reader)
        topics.append(topic)
    return topics


async def test_fanout_runs_children_in_batches(db_session, temporal_client, monkeypatch):
    monkeypatch.setattr(get_settings(), "fanout_batch_size", 10)
    topics = _subscribed_topics(db_session, 25)
    lock = threading.Lock()
    running = {"now": 0, "max": 0}
    real_context = temporal_activities.DigestContext
    real_advance = crud.advance_topic_watermark

    def child_started(*args, **kwargs):  # get_digest_context: a child's first activity
        with lock:
            running["now"] += 1
            running["max"] = max(running["max"], running["now"])
        return real_context(*args, **kwargs)

    def child_finished(db, topic, checked_at):  # advance_watermark: its last
        real_advance(db, topic, checked_at)
        with lock:
            running["now"] -= 1

    monkeypatch.setattr(temporal_activities, "DigestContext", child_started)
    monkeypatch.setattr(crud, "advance_topic_watermark", child_finished)

    count = await temporal_client.execute_workflow(
        RunAllTopicDigestsWorkflow.run, id="run-all-batched", task_queue=TASK_QUEUE
    )

    assert count == 25
    assert 1 < running["max"] <= 10
    db_session.expire_all()
    for topic in topics:
        [digest] = db_session.get(models.Topic, topic.id).digests
        assert digest.status == models.DigestStatus.completed


async def test_fanout_carries_on_past_a_failed_topic(db_session, temporal_client, monkeypatch):
    """A topic deleted between listing and digest creation fails only itself."""
    monkeypatch.setattr(get_settings(), "fanout_batch_size", 2)
    gone, *others = _subscribed_topics(db_session, 4)
    real_get_topic = crud.get_topic
    monkeypatch.setattr(
        crud, "get_topic", lambda db, topic_id: None if topic_id == gone.id else real_get_topic(db, topic_id)
    )

    count = await temporal_client.execute_workflow(
        RunAllTopicDigestsWorkflow.run, id="run-all-one-gone", task_queue=TASK_QUEUE
    )

    assert count == 3
    db_session.expire_all()
    assert db_session.get(models.Topic, gone.id).digests == []
    for topic in others:
        [digest] = db_session.get(models.Topic, topic.id).digests
        assert digest.status == models.DigestStatus.completed


async def test_arxiv_requests_one_in_flight_and_spaced(db_session, temporal_client, monkeypatch):
    """Review Focus 4: fifty topics' ingest never has more than one arXiv
    request in flight, and requests start at least arxiv_page_delay apart.
    Each fake request outlasts the delay, so the throttle alone (which only
    spaces out starts) would let them overlap; the one-slot queue must not."""
    settings = get_settings()
    monkeypatch.setattr(settings, "arxiv_page_delay", 0.05)
    _subscribed_topics(db_session, 50)
    lock = threading.Lock()
    calls: list[tuple[float, float]] = []

    def fake_search(query, since=None):
        arxiv_service.throttle(settings.arxiv_page_delay)
        start = time.monotonic()
        time.sleep(0.08)
        end = time.monotonic()
        with lock:
            calls.append((start, end))
        return []

    monkeypatch.setattr(temporal_activities, "search_arxiv", fake_search)

    count = await temporal_client.execute_workflow(
        RunAllTopicDigestsWorkflow.run, id="run-all-arxiv", task_queue=TASK_QUEUE
    )

    assert count == 50
    assert len(calls) == 50
    events = sorted([(s, 1) for s, _ in calls] + [(e, -1) for _, e in calls], key=lambda x: (x[0], x[1]))
    in_flight = max_overlap = 0
    for _, delta in events:
        in_flight += delta
        max_overlap = max(max_overlap, in_flight)
    assert max_overlap == 1
    starts = sorted(s for s, _ in calls)
    assert min(b - a for a, b in zip(starts, starts[1:])) >= 0.05


def test_rag_fulltext_defaults_off():
    assert Settings.model_fields["rag_fulltext"].default is False
    compose = (Path(__file__).resolve().parent.parent / "docker-compose.yml").read_text()
    assert "RAG_FULLTEXT: ${RAG_FULLTEXT:-true}" not in compose
    assert compose.count("RAG_FULLTEXT: ${RAG_FULLTEXT:-false}") == 2  # api and worker


def test_pipeline_concurrency_defaults():
    fields = Settings.model_fields
    assert fields["llm_max_concurrency"].default == 8
    assert fields["fanout_batch_size"].default == 10
    assert fields["worker_activity_threads"].default == 20


def test_send_pipeline_alert_never_raises(db_session, monkeypatch):
    def smtp_down(*args, **kwargs):
        raise RuntimeError("smtp is down")

    monkeypatch.setattr(temporal_activities, "send_alert", smtp_down)

    assert temporal_activities.send_pipeline_alert(
        PipelineAlertInput(key="ratings-failing:2026-11-12", subject="s", body="b")
    ) is False
