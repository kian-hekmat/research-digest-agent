"""End-to-end tests for the Temporal workflow layer.

Runs DigestWorkflow / RunAllTopicDigestsWorkflow for real, against Temporal's
ephemeral *time-skipping* test server (no Docker needed) and a real worker
using the real Activities - which in turn hit the real (migrations-built)
Postgres test DB via app.database.SessionLocal. Only arXiv and Anthropic are
faked, patched directly onto the `app.temporal.activities` module.

Time-skipping matters here specifically: DigestWorkflow's arXiv retry policy
backs off up to 5s/10s/20s/40s between attempts. On a real server a persistent
failure test would take over a minute; time-skipping fast-forwards through
timers the workflow is merely waiting on, so the same scenario finishes in
about a second.
"""
from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

import pytest
import pytest_asyncio
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

from app import crud, models
from app.temporal import activities as temporal_activities
from app.temporal.schedule import DAILY_SCHEDULE_ID, ensure_daily_schedule
from app.temporal.workflows import (
    WORKFLOW_RUNNER,
    DigestWorkflow,
    RunAllTopicDigestsWorkflow,
    SendDigestEmailsWorkflow,
)
from tests.conftest import make_result

TASK_QUEUE = "test-digest-task-queue"

ALL_ACTIVITIES = [
    temporal_activities.list_active_topic_ids,
    temporal_activities.create_pending_digest,
    temporal_activities.get_digest_context,
    temporal_activities.fetch_arxiv,
    temporal_activities.ingest_papers,
    temporal_activities.summarize_paper,
    temporal_activities.write_overview,
    temporal_activities.finalize_digest,
    temporal_activities.advance_watermark,
    temporal_activities.list_due_subscriptions,
    temporal_activities.gather_digest_content,
    temporal_activities.send_digest_email,
    temporal_activities.mark_subscription_sent,
]


class FakeSummarizer:
    """Stands in for app.services.summarize.Summarizer. `fail_titles` is a
    class attribute (the activity calls `Summarizer()` with no args, so a
    test configures failures by mutating the class, not an instance)."""

    fail_titles: set[str] = set()
    # Concurrency tracking: summarize_paper runs on the activity thread pool.
    delay: float = 0.0
    in_flight = 0
    max_in_flight = 0
    _lock = threading.Lock()

    def summarize_paper(self, title, abstract):
        cls = type(self)
        with cls._lock:
            cls.in_flight += 1
            cls.max_in_flight = max(cls.max_in_flight, cls.in_flight)
        try:
            time.sleep(cls.delay)
            if title in cls.fail_titles:
                raise RuntimeError(f"summary failed for {title}")
            return f"Summary of {title}."
        finally:
            with cls._lock:
                cls.in_flight -= 1

    def write_overview(self, topic_name, summaries):
        return f"Overview of {topic_name}: {len(summaries)} papers."


class FakeEmailSender:
    """Stands in for app.services.email.EmailSender. Like FakeSummarizer, the
    activity calls `EmailSender()` with no args, so tests use class-level
    state: `sent` (list of (to, content)) and `fail_for` (a set of addresses
    whose send raises)."""

    sent: list[tuple[str, object]] = []
    fail_for: set[str] = set()

    def send(self, to, content):
        if to in type(self).fail_for:
            raise RuntimeError(f"send failed for {to}")
        type(self).sent.append((to, content))


def _reset_fake_state():
    FakeSummarizer.fail_titles = set()
    FakeSummarizer.delay = 0.0
    FakeSummarizer.in_flight = 0
    FakeSummarizer.max_in_flight = 0
    FakeEmailSender.sent = []
    FakeEmailSender.fail_for = set()


@pytest.fixture(autouse=True)
def _reset_fakes():
    _reset_fake_state()
    yield
    _reset_fake_state()


@pytest_asyncio.fixture
async def temporal_client(monkeypatch):
    monkeypatch.setattr(temporal_activities, "Summarizer", FakeSummarizer)
    monkeypatch.setattr(temporal_activities, "EmailSender", FakeEmailSender)
    async with await WorkflowEnvironment.start_time_skipping() as env:
        with ThreadPoolExecutor(max_workers=8) as pool:
            async with Worker(
                env.client,
                task_queue=TASK_QUEUE,
                workflows=[DigestWorkflow, RunAllTopicDigestsWorkflow, SendDigestEmailsWorkflow],
                activities=ALL_ACTIVITIES,
                activity_executor=pool,
                workflow_runner=WORKFLOW_RUNNER,
            ):
                yield env.client


def _make_topic(db_session, name="RLHF", query="rlhf") -> models.Topic:
    topic = models.Topic(name=name, query=query)
    db_session.add(topic)
    db_session.commit()
    return topic


async def _run_digest(client, digest_id: str) -> None:
    await client.execute_workflow(
        DigestWorkflow.run, digest_id, id=f"digest-{digest_id}", task_queue=TASK_QUEUE
    )


# ---------- DigestWorkflow ----------
async def test_digest_workflow_happy_path(db_session, temporal_client, monkeypatch):
    topic = _make_topic(db_session)
    digest = crud.create_digest(db_session, topic.id)

    monkeypatch.setattr(
        temporal_activities,
        "search_arxiv",
        lambda query, since=None: [
            make_result("2408.0001", "Paper One"),
            make_result("2408.0002", "Paper Two"),
        ],
    )

    await _run_digest(temporal_client, digest.id)

    db_session.expire_all()
    refreshed = db_session.get(models.Digest, digest.id)
    assert refreshed.status == models.DigestStatus.completed
    assert refreshed.error is None
    assert {p.title for p in refreshed.papers} == {"Paper One", "Paper Two"}
    assert all(p.summary for p in refreshed.papers)
    assert refreshed.overview == "Overview of RLHF: 2 papers."

    refreshed_topic = db_session.get(models.Topic, topic.id)
    assert refreshed_topic.last_checked_at is not None


async def test_digest_workflow_no_new_papers_completes_empty(
    db_session, temporal_client, monkeypatch
):
    topic = _make_topic(db_session)
    digest = crud.create_digest(db_session, topic.id)
    monkeypatch.setattr(temporal_activities, "search_arxiv", lambda query, since=None: [])

    await _run_digest(temporal_client, digest.id)

    db_session.expire_all()
    refreshed = db_session.get(models.Digest, digest.id)
    assert refreshed.status == models.DigestStatus.completed
    assert refreshed.papers == []
    assert refreshed.overview is None
    assert refreshed.error is None
    # still a real, successful run - the watermark moves forward
    assert db_session.get(models.Topic, topic.id).last_checked_at is not None


async def test_digest_workflow_arxiv_failure_exhausts_retries_then_fails(
    db_session, temporal_client, monkeypatch
):
    topic = _make_topic(db_session)
    digest = crud.create_digest(db_session, topic.id)

    def always_fails(query, since=None):
        raise RuntimeError("arxiv is down")

    monkeypatch.setattr(temporal_activities, "search_arxiv", always_fails)

    await _run_digest(temporal_client, digest.id)

    db_session.expire_all()
    refreshed = db_session.get(models.Digest, digest.id)
    assert refreshed.status == models.DigestStatus.failed
    assert "arXiv fetch failed" in refreshed.error
    assert "arxiv is down" in refreshed.error
    assert refreshed.papers == []
    # failure -> the watermark must NOT advance, so a retry gets the full window
    assert db_session.get(models.Topic, topic.id).last_checked_at is None


async def test_digest_workflow_tolerates_partial_summary_failure(
    db_session, temporal_client, monkeypatch
):
    topic = _make_topic(db_session)
    digest = crud.create_digest(db_session, topic.id)
    monkeypatch.setattr(
        temporal_activities,
        "search_arxiv",
        lambda query, since=None: [
            make_result("2408.0001", "Paper One"),
            make_result("2408.0002", "Paper Two"),
        ],
    )
    FakeSummarizer.fail_titles = {"Paper Two"}

    await _run_digest(temporal_client, digest.id)

    db_session.expire_all()
    refreshed = db_session.get(models.Digest, digest.id)
    assert refreshed.status == models.DigestStatus.completed
    assert refreshed.error == "1 of 2 paper summaries failed"
    by_title = {p.title: p for p in refreshed.papers}
    assert by_title["Paper One"].summary == "Summary of Paper One."
    assert by_title["Paper Two"].summary is None
    # overview is still written from whatever did summarize
    assert refreshed.overview == "Overview of RLHF: 1 papers."


async def test_digest_workflow_caps_concurrent_summaries(db_session, temporal_client, monkeypatch):
    """A local Ollama server handles a few requests at a time and each
    activity's timeout runs while it queues - so the workflow must never have
    more than _MAX_CONCURRENT_SUMMARIES in flight, and still summarize all."""
    from app.temporal.workflows import _MAX_CONCURRENT_SUMMARIES

    topic = _make_topic(db_session)
    digest = crud.create_digest(db_session, topic.id)
    monkeypatch.setattr(
        temporal_activities,
        "search_arxiv",
        lambda query, since=None: [make_result(f"2409.{i:04d}", f"Paper {i}") for i in range(12)],
    )
    FakeSummarizer.delay = 0.1  # long enough that uncapped calls would overlap

    await _run_digest(temporal_client, digest.id)

    assert FakeSummarizer.max_in_flight == _MAX_CONCURRENT_SUMMARIES
    db_session.expire_all()
    refreshed = db_session.get(models.Digest, digest.id)
    assert len(refreshed.papers) == 12
    assert all(p.summary for p in refreshed.papers)


async def test_digest_workflow_without_api_key_completes_with_no_summaries(
    db_session, temporal_client, monkeypatch
):
    """Uses the REAL Summarizer (undoing the fixture's FakeSummarizer patch) to
    prove the no-key path end-to-end: the test environment has no
    ANTHROPIC_API_KEY set, so this exercises Summarizer's actual short-circuit,
    not a stand-in for it."""
    from app.services.summarize import Summarizer as RealSummarizer

    monkeypatch.setattr(temporal_activities, "Summarizer", RealSummarizer)
    monkeypatch.setattr(
        temporal_activities,
        "search_arxiv",
        lambda query, since=None: [make_result("2408.0001", "Paper One")],
    )
    topic = _make_topic(db_session)
    digest = crud.create_digest(db_session, topic.id)

    await _run_digest(temporal_client, digest.id)

    db_session.expire_all()
    refreshed = db_session.get(models.Digest, digest.id)
    assert refreshed.status == models.DigestStatus.completed
    assert refreshed.error is None  # not a failure - deliberately disabled
    assert refreshed.overview is None
    assert refreshed.papers[0].summary is None
    assert db_session.get(models.Topic, topic.id).last_checked_at is not None


async def test_digest_workflow_rerun_skips_papers_the_topic_already_has(
    db_session, temporal_client, monkeypatch
):
    """The lookback window means consecutive runs re-fetch overlapping
    results; a paper already ingested for this topic must not land in the
    next digest too (it'd be emailed twice)."""
    topic = _make_topic(db_session)

    monkeypatch.setattr(
        temporal_activities,
        "search_arxiv",
        lambda query, since=None: [
            make_result("2408.0001", "Paper One"),
            make_result("2408.0002", "Paper Two"),
        ],
    )

    first = crud.create_digest(db_session, topic.id)
    await _run_digest(temporal_client, first.id)

    second = crud.create_digest(db_session, topic.id)
    await _run_digest(temporal_client, second.id)

    db_session.expire_all()
    assert db_session.query(models.Paper).count() == 2  # deduped on arxiv_id
    assert {p.title for p in db_session.get(models.Digest, first.id).papers} == {
        "Paper One",
        "Paper Two",
    }
    second_refreshed = db_session.get(models.Digest, second.id)
    assert second_refreshed.status == models.DigestStatus.completed
    assert second_refreshed.papers == []


async def test_digest_workflow_rerun_still_ingests_a_new_paper_for_another_topic(
    db_session, temporal_client, monkeypatch
):
    """The skip is per topic: a paper one topic already has is still new to
    a different topic whose query matches it."""
    rlhf = _make_topic(db_session, name="RLHF", query="rlhf")
    other = _make_topic(db_session, name="Alignment", query="alignment")
    monkeypatch.setattr(
        temporal_activities, "search_arxiv", lambda query, since=None: [make_result("1", "Shared")]
    )

    await _run_digest(temporal_client, crud.create_digest(db_session, rlhf.id).id)
    second = crud.create_digest(db_session, other.id)
    await _run_digest(temporal_client, second.id)

    db_session.expire_all()
    assert [p.title for p in db_session.get(models.Digest, second.id).papers] == ["Shared"]


async def test_digest_workflow_fetch_looks_back_past_the_watermark(
    db_session, temporal_client, monkeypatch
):
    topic = _make_topic(db_session)
    watermark = datetime(2026, 9, 27, 6, 0, tzinfo=timezone.utc)
    topic.last_checked_at = watermark
    db_session.commit()
    calls = []

    def fetch(query, since=None):
        calls.append(since)
        return []

    monkeypatch.setattr(temporal_activities, "search_arxiv", fetch)

    await _run_digest(temporal_client, crud.create_digest(db_session, topic.id).id)

    assert calls == [watermark - timedelta(days=7)]


async def test_digest_workflow_catches_a_paper_announced_after_the_watermark_passed_it(
    db_session, temporal_client, monkeypatch
):
    """The bug behind two weeks of empty RLHF digests: a paper submitted at
    05:03 isn't in the API until it's announced, a day or more later. The
    06:00 run that day doesn't see it but moves the watermark to 06:00 - and
    filtering strictly on `published > watermark`, every later run discarded
    it forever. Goes through the real `search_arxiv` (only HTTP is faked), so
    this exercises the actual `since` filter that dropped it."""
    import httpx
    import respx

    from app.services.arxiv import search_arxiv as real_search_arxiv

    now = datetime.now(timezone.utc)
    submitted = now - timedelta(days=2)
    entry = f"""<entry>
      <id>http://arxiv.org/abs/2609.33221v1</id>
      <published>{submitted:%Y-%m-%dT%H:%M:%SZ}</published>
      <title>RMB: Reward Model Boosting Mitigates Reward Hacking</title>
      <summary>...</summary>
    </entry>"""
    feed = '<?xml version="1.0"?><feed xmlns="http://www.w3.org/2005/Atom">{}</feed>'
    announced = {"yet": False}

    monkeypatch.setattr(
        temporal_activities,
        "search_arxiv",
        lambda query, since=None: real_search_arxiv(query, since=since, delay=0),
    )
    topic = _make_topic(db_session)
    topic.last_checked_at = now - timedelta(days=3)
    db_session.commit()

    with respx.mock:
        respx.get("https://export.arxiv.org/api/query").mock(
            side_effect=lambda request: httpx.Response(
                200, text=feed.format(entry if announced["yet"] else "")
            )
        )

        # Run before announcement: sees nothing, moves the watermark past it.
        await _run_digest(temporal_client, crud.create_digest(db_session, topic.id).id)
        db_session.expire_all()
        assert db_session.get(models.Topic, topic.id).last_checked_at > submitted

        # Now announced; the next run must still pick it up.
        announced["yet"] = True
        later = crud.create_digest(db_session, topic.id)
        await _run_digest(temporal_client, later.id)

    db_session.expire_all()
    assert [p.arxiv_id for p in db_session.get(models.Digest, later.id).papers] == ["2609.33221"]


# ---------- RunAllTopicDigestsWorkflow ----------
async def test_run_all_topic_digests_fans_out_one_digest_per_topic(
    db_session, temporal_client, monkeypatch
):
    rlhf = _make_topic(db_session, name="RLHF", query="rlhf")
    interp = _make_topic(db_session, name="Mech Interp", query="mechanistic interpretability")

    # Same paper matches both topics' queries - proves cross-topic dedup/link.
    monkeypatch.setattr(
        temporal_activities,
        "search_arxiv",
        lambda query, since=None: [make_result("2409.0001", "Shared Paper")],
    )

    count = await temporal_client.execute_workflow(
        RunAllTopicDigestsWorkflow.run, id="run-all-1", task_queue=TASK_QUEUE
    )
    assert count == 2

    db_session.expire_all()
    assert db_session.query(models.Paper).count() == 1  # deduped across topics
    assert db_session.query(models.Digest).count() == 2

    for topic in (rlhf, interp):
        refreshed_topic = db_session.get(models.Topic, topic.id)
        assert len(refreshed_topic.digests) == 1
        [digest] = refreshed_topic.digests
        assert digest.status == models.DigestStatus.completed
        assert [p.title for p in digest.papers] == ["Shared Paper"]


async def test_run_all_topic_digests_with_no_topics_returns_zero(db_session, temporal_client):
    count = await temporal_client.execute_workflow(
        RunAllTopicDigestsWorkflow.run, id="run-all-empty", task_queue=TASK_QUEUE
    )
    assert count == 0
    assert db_session.query(models.Digest).count() == 0


# ---------- SendDigestEmailsWorkflow ----------
def _make_subscription(
    db_session, topic, email, *, cadence="weekly", last_sent_at=None, max_papers=None
):
    sub = crud.create_subscription(
        db_session, topic.id, email, models.SubscriptionCadence(cadence), max_papers
    )
    if last_sent_at is not None:
        sub.last_sent_at = last_sent_at
        db_session.commit()
        db_session.refresh(sub)
    return sub


def _make_completed_digest(db_session, topic, *, generated_at, papers=(), published_at=None):
    digest = models.Digest(
        topic_id=topic.id,
        status=models.DigestStatus.completed,
        overview="Overview.",
        generated_at=generated_at,
    )
    db_session.add(digest)
    db_session.commit()
    for i, (title, summary) in enumerate(papers):
        paper = models.Paper(
            arxiv_id=title,
            title=title,
            summary=summary,
            # Distinct, descending submission times so "newest N" is well defined.
            published_at=(published_at or generated_at) - timedelta(minutes=i),
        )
        db_session.add(paper)
        db_session.commit()
        digest.papers.append(paper)
    db_session.commit()
    return digest


async def _run_send_emails(client, run_id: str) -> int:
    return await client.execute_workflow(
        SendDigestEmailsWorkflow.run, id=run_id, task_queue=TASK_QUEUE
    )


async def test_send_digest_emails_delivers_to_due_subscription(db_session, temporal_client):
    topic = _make_topic(db_session)
    eight_days_ago = datetime.now(timezone.utc) - timedelta(days=8)
    sub = _make_subscription(db_session, topic, "reader@example.com", last_sent_at=eight_days_ago)
    _make_completed_digest(
        db_session, topic, generated_at=eight_days_ago + timedelta(hours=1),
        papers=[("Paper One", "Summary one.")],
    )

    count = await _run_send_emails(temporal_client, "send-1")

    assert count == 1
    assert len(FakeEmailSender.sent) == 1
    to, content = FakeEmailSender.sent[0]
    assert to == "reader@example.com"
    assert "Paper One" in content.text_body
    assert "Summary one." in content.text_body

    db_session.expire_all()
    refreshed = db_session.get(models.Subscription, sub.id)
    assert refreshed.last_sent_at > eight_days_ago


async def test_send_digest_emails_skips_not_yet_due_subscription(db_session, temporal_client):
    topic = _make_topic(db_session)
    just_sent = datetime.now(timezone.utc) - timedelta(days=1)
    _make_subscription(db_session, topic, "reader@example.com", last_sent_at=just_sent)
    _make_completed_digest(db_session, topic, generated_at=just_sent, papers=[("P", "S")])

    count = await _run_send_emails(temporal_client, "send-2")

    assert count == 0
    assert FakeEmailSender.sent == []


async def test_send_digest_emails_skips_and_preserves_watermark_when_nothing_new(
    db_session, temporal_client
):
    topic = _make_topic(db_session)
    eight_days_ago = datetime.now(timezone.utc) - timedelta(days=8)
    sub = _make_subscription(db_session, topic, "reader@example.com", last_sent_at=eight_days_ago)
    # no completed digest since eight_days_ago

    count = await _run_send_emails(temporal_client, "send-3")

    assert count == 0
    assert FakeEmailSender.sent == []
    db_session.expire_all()
    refreshed = db_session.get(models.Subscription, sub.id)
    assert refreshed.last_sent_at == eight_days_ago  # untouched - not silently reset


async def test_send_digest_emails_respects_weekly_vs_biweekly_cadence(db_session, temporal_client):
    topic = _make_topic(db_session)
    eight_days_ago = datetime.now(timezone.utc) - timedelta(days=8)
    _make_subscription(
        db_session, topic, "weekly@example.com", cadence="weekly", last_sent_at=eight_days_ago
    )
    _make_subscription(
        db_session, topic, "biweekly@example.com", cadence="biweekly", last_sent_at=eight_days_ago
    )
    _make_completed_digest(
        db_session, topic, generated_at=eight_days_ago + timedelta(hours=1),
        papers=[("Paper One", "Summary one.")],
    )

    count = await _run_send_emails(temporal_client, "send-4")

    assert count == 1
    sent_to = {to for to, _ in FakeEmailSender.sent}
    assert sent_to == {"weekly@example.com"}  # biweekly isn't due for another 6 days


async def test_send_digest_emails_tolerates_one_subscriber_failing(db_session, temporal_client):
    topic = _make_topic(db_session)
    eight_days_ago = datetime.now(timezone.utc) - timedelta(days=8)
    ok_sub = _make_subscription(db_session, topic, "ok@example.com", last_sent_at=eight_days_ago)
    bad_sub = _make_subscription(db_session, topic, "bad@example.com", last_sent_at=eight_days_ago)
    _make_completed_digest(
        db_session, topic, generated_at=eight_days_ago + timedelta(hours=1),
        papers=[("Paper One", "Summary one.")],
    )
    FakeEmailSender.fail_for = {"bad@example.com"}

    count = await _run_send_emails(temporal_client, "send-5")

    assert count == 1  # only the successful one counted
    assert {to for to, _ in FakeEmailSender.sent} == {"ok@example.com"}

    db_session.expire_all()
    assert db_session.get(models.Subscription, ok_sub.id).last_sent_at > eight_days_ago
    assert db_session.get(models.Subscription, bad_sub.id).last_sent_at == eight_days_ago


async def test_send_digest_emails_with_nothing_due_returns_zero(db_session, temporal_client):
    count = await _run_send_emails(temporal_client, "send-6")
    assert count == 0
    assert FakeEmailSender.sent == []


async def test_send_digest_emails_combines_a_recipients_topics_into_one_capped_email(
    db_session, temporal_client
):
    """The real setup: one address subscribed to RLHF (max 10), PINNs (max 5)
    and numerical analysis (max 5) gets ONE email with a section per topic,
    each cut to its own cap, and every contributing watermark advances."""
    last_sent = datetime.now(timezone.utc) - timedelta(days=4)
    topics_and_caps = [("RLHF", 10, 14), ("PINNs", 5, 7), ("Numerical Analysis", 5, 30)]
    subs = []
    for name, cap, available in topics_and_caps:
        topic = _make_topic(db_session, name=name, query=name.lower())
        subs.append(
            _make_subscription(
                db_session, topic, "me@example.com",
                cadence="twice_weekly", last_sent_at=last_sent, max_papers=cap,
            )
        )
        _make_completed_digest(
            db_session, topic, generated_at=last_sent + timedelta(days=1),
            papers=[(f"{name} paper {i}", f"Summary {i}.") for i in range(available)],
        )

    count = await _run_send_emails(temporal_client, "send-combined")

    assert count == 1
    [(to, content)] = FakeEmailSender.sent
    assert to == "me@example.com"
    assert content.subject == "Research digest: 20 new papers (RLHF, PINNs, Numerical Analysis)"
    text = content.text_body
    assert text.index("RLHF - 10 new papers") < text.index("PINNs - 5 new papers")
    assert text.index("PINNs - 5 new papers") < text.index("Numerical Analysis - 5 new papers")
    assert text.count("https://arxiv.org/abs/") == 20
    # Newest kept, oldest cut: paper 0 is the newest of each topic.
    assert "RLHF paper 0\n" in text and "RLHF paper 13\n" not in text
    assert "+ 4 more RLHF papers not shown" in text
    assert "+ 25 more Numerical Analysis papers not shown" in text

    db_session.expire_all()
    for sub in subs:
        assert db_session.get(models.Subscription, sub.id).last_sent_at > last_sent


async def test_send_digest_emails_leaves_out_a_topic_with_nothing_new(
    db_session, temporal_client
):
    last_sent = datetime.now(timezone.utc) - timedelta(days=4)
    rlhf = _make_topic(db_session, name="RLHF", query="rlhf")
    quiet = _make_topic(db_session, name="PINNs", query="pinns")
    rlhf_sub = _make_subscription(
        db_session, rlhf, "me@example.com", cadence="twice_weekly", last_sent_at=last_sent
    )
    quiet_sub = _make_subscription(
        db_session, quiet, "me@example.com", cadence="twice_weekly", last_sent_at=last_sent
    )
    _make_completed_digest(
        db_session, rlhf, generated_at=last_sent + timedelta(days=1), papers=[("R1", "S.")]
    )
    # PINNs ran, but every run came back empty.
    _make_completed_digest(db_session, quiet, generated_at=last_sent + timedelta(days=1))

    await _run_send_emails(temporal_client, "send-partial")

    [(_, content)] = FakeEmailSender.sent
    assert content.subject == "RLHF: 1 new paper"
    assert "PINNs" not in content.text_body

    db_session.expire_all()
    assert db_session.get(models.Subscription, rlhf_sub.id).last_sent_at > last_sent
    assert db_session.get(models.Subscription, quiet_sub.id).last_sent_at == last_sent


async def test_send_digest_emails_stamps_email_dates_from_paper_submission(
    db_session, temporal_client
):
    """End-to-end version of the date regression: several same-day runs, one
    with papers submitted the day before - the email heading is the
    submission day, once."""
    last_sent = datetime(2026, 9, 14, 19, 11, tzinfo=timezone.utc)
    topic = _make_topic(db_session)
    _make_subscription(db_session, topic, "me@example.com", last_sent_at=last_sent)
    for hour in (1, 5, 6):
        _make_completed_digest(
            db_session, topic, generated_at=datetime(2026, 9, 16, hour, tzinfo=timezone.utc)
        )
    _make_completed_digest(
        db_session, topic,
        generated_at=datetime(2026, 9, 16, 1, 18, tzinfo=timezone.utc),
        papers=[(f"P{i}", None) for i in range(3)],
        published_at=datetime(2026, 9, 15, 17, tzinfo=timezone.utc),
    )

    await _run_send_emails(temporal_client, "send-dates")

    [(_, content)] = FakeEmailSender.sent
    assert content.text_body.count("September 15, 2026") == 1
    assert "September 16" not in content.text_body


# ---------- cadence due-ness ----------
@pytest.mark.parametrize(
    "cadence, since_last_send, due",
    [
        # The schedule fires at 08:00:00 but the watermark is stamped a few
        # seconds later, so the next fire is a few seconds short of a full
        # interval - these must still count as due.
        ("twice_weekly", timedelta(days=3) - timedelta(seconds=5), True),   # Mon -> Thu
        ("twice_weekly", timedelta(days=4) - timedelta(seconds=5), True),   # Thu -> Mon
        ("twice_weekly", timedelta(days=1), False),
        ("weekly", timedelta(days=7) - timedelta(seconds=5), True),
        ("weekly", timedelta(days=3), False),                               # skips Thursday
        ("weekly", timedelta(days=4), False),                               # skips Monday after a Thursday send
        ("biweekly", timedelta(days=14) - timedelta(seconds=5), True),
        ("biweekly", timedelta(days=7), False),
        # A catch-up send at 13:17 Monday (laptop asleep at 08:00) must not
        # push the next weekly send out a whole extra week.
        ("weekly", timedelta(days=6, hours=18, minutes=43), True),
    ],
)
def test_list_due_subscriptions_cadence(db_session, cadence, since_last_send, due):
    now = datetime(2026, 10, 1, 8, 0, 1, tzinfo=timezone.utc)
    topic = _make_topic(db_session)
    _make_subscription(
        db_session, topic, "me@example.com", cadence=cadence, last_sent_at=now - since_last_send
    )

    assert bool(crud.list_due_subscriptions(db_session, now)) is due


# ---------- schedule + status-literal drift guard ----------
def test_digest_status_literals_match_the_model_enum():
    from app.temporal import types

    assert types.DIGEST_STATUS_COMPLETED == models.DigestStatus.completed.value
    assert types.DIGEST_STATUS_FAILED == models.DigestStatus.failed.value


@pytest_asyncio.fixture
async def temporal_env_only():
    # Schedules aren't implemented by the time-skipping test server (only the
    # full dev server) - this fixture is for schedule tests only.
    async with await WorkflowEnvironment.start_local() as env:
        yield env


async def test_ensure_daily_schedule_is_idempotent(temporal_env_only):
    client = temporal_env_only.client
    assert await ensure_daily_schedule(client) == "created"
    assert await ensure_daily_schedule(client) == "unchanged"

    handle = client.get_schedule_handle(DAILY_SCHEDULE_ID)
    desc = await handle.describe()
    # The server normalizes the cron string into a calendar spec rather than
    # echoing it back verbatim - check the parsed hour instead of the string.
    [calendar] = desc.schedule.spec.calendars
    assert calendar.hour[0].start == 6
    assert desc.schedule.action.workflow == "RunAllTopicDigestsWorkflow"


def _days_of_week(calendar) -> set[int]:
    return {
        day
        for r in calendar.day_of_week
        for day in range(r.start, (r.end or r.start) + 1, r.step or 1)
    }


async def test_ensure_email_schedule_fires_monday_and_thursday(temporal_env_only):
    from app.temporal.schedule import EMAIL_SCHEDULE_ID, ensure_email_schedule

    client = temporal_env_only.client
    assert await ensure_email_schedule(client) == "created"
    assert await ensure_email_schedule(client) == "unchanged"

    desc = await client.get_schedule_handle(EMAIL_SCHEDULE_ID).describe()
    [calendar] = desc.schedule.spec.calendars
    assert calendar.hour[0].start == 8
    assert _days_of_week(calendar) == {1, 4}  # Monday, Thursday
    assert desc.schedule.action.workflow == "SendDigestEmailsWorkflow"


async def test_ensure_email_schedule_updates_an_existing_schedule_in_place(temporal_env_only):
    """Temporal state persists across restarts now, so the schedule created
    back when sends were Monday-only still exists. A create-if-missing check
    alone would leave it Monday-only forever - it must be updated in place."""
    from temporalio.client import (
        Schedule,
        ScheduleActionStartWorkflow,
        ScheduleSpec,
    )

    from app.temporal.schedule import EMAIL_SCHEDULE_ID, ensure_email_schedule

    client = temporal_env_only.client
    # Exactly how the old code created it: Mondays only, no note.
    await client.create_schedule(
        EMAIL_SCHEDULE_ID,
        Schedule(
            action=ScheduleActionStartWorkflow(
                SendDigestEmailsWorkflow.run, id="scheduled-digest-emails", task_queue=TASK_QUEUE
            ),
            spec=ScheduleSpec(cron_expressions=["0 8 * * 1"]),
        ),
    )

    assert await ensure_email_schedule(client) == "updated"
    assert await ensure_email_schedule(client) == "unchanged"

    desc = await client.get_schedule_handle(EMAIL_SCHEDULE_ID).describe()
    [calendar] = desc.schedule.spec.calendars
    assert _days_of_week(calendar) == {1, 4}
    assert desc.schedule.action.workflow == "SendDigestEmailsWorkflow"  # action untouched
