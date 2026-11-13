"""End-to-end tests for the Temporal workflow layer.

Runs DigestWorkflow / RunAllTopicDigestsWorkflow for real, against Temporal's
ephemeral *time-skipping* test server (no Docker needed) and a real worker
using the real Activities - which in turn hit the real (migrations-built)
Postgres test DB via app.database.SessionLocal. Only arXiv, the LLM, Semantic
Scholar and SMTP are faked, patched directly onto the `app.temporal.activities` module.

Time-skipping matters here specifically: DigestWorkflow's arXiv retry policy
backs off up to 5s/10s/20s/40s between attempts. On a real server a persistent
failure test would take over a minute; time-skipping fast-forwards through
timers the workflow is merely waiting on, so the same scenario finishes in
about a second.
"""
from __future__ import annotations

import dataclasses
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

import pytest
import pytest_asyncio
from temporalio.testing import WorkflowEnvironment

from app import crud, models
from app.services.summarize import RatedSummary
from app.temporal import activities as temporal_activities
from app.temporal.schedule import DAILY_SCHEDULE_ID, ensure_daily_schedule
from app.cadence import latest_slot
from app.temporal.types import PrepTopicInput
from app.temporal.workflows import (
    DigestWorkflow,
    PrepEditionsWorkflow,
    PrepTopicEditionWorkflow,
    RunAllTopicDigestsWorkflow,
    SendDigestEmailsWorkflow,
)
from app.worker import build_workers
from tests.conftest import EmbedderTextMixin, make_result, make_subscriber, subscribe

TASK_QUEUE = "test-digest-task-queue"

class FakeSummarizer:
    """Stands in for app.services.summarize.Summarizer. `fail_titles` is a
    class attribute (the activity calls `Summarizer()` with no args, so a
    test configures failures by mutating the class, not an instance)."""

    fail_titles: set[str] = set()
    # title -> relevance rating to return; unlisted titles get 5.
    relevance: dict[str, int] = {}
    # (title, topic_name) of every call, summarize_and_rate and rate_relevance.
    rated: list[tuple[str, str]] = []
    # Title of every summarize_and_rate call.
    summarized: list[str] = []
    # Concurrency tracking: LLM activities run on the activity thread pool.
    delay: float = 0.0
    in_flight = 0
    max_in_flight = 0
    overview_histories: list = []
    # Constructor kwargs of every instance: activities must pass a spend
    # ledger and the topic id (app.services.spend).
    constructed: list[dict] = []
    _lock = threading.Lock()

    def __init__(self, *args, **kwargs):
        type(self).constructed.append(kwargs)

    def _llm_call(self, title, failure):
        """One LLM call: counted for concurrency, slowed by `delay`, raising
        `failure` for titles in `fail_titles`."""
        cls = type(self)
        with cls._lock:
            cls.in_flight += 1
            cls.max_in_flight = max(cls.max_in_flight, cls.in_flight)
        try:
            time.sleep(cls.delay)
            if title in cls.fail_titles:
                raise RuntimeError(f"{failure} for {title}")
        finally:
            with cls._lock:
                cls.in_flight -= 1

    def summarize_paper(self, title, abstract):
        self._llm_call(title, "summary failed")
        return f"Summary of {title}."

    def summarize_and_rate(self, title, abstract, topic_name, topic_query, related=None, rating_context=None, excerpts=None):
        summary = self.summarize_paper(title, abstract)
        type(self).summarized.append(title)
        type(self).rated.append((title, topic_name))
        return RatedSummary(summary=summary, relevance=type(self).relevance.get(title, 5))

    def rate_relevance(self, title, abstract, topic_name, topic_query, rating_context=None):
        self._llm_call(title, "rating failed")
        type(self).rated.append((title, topic_name))
        return type(self).relevance.get(title, 5)

    def write_overview(self, topic_name, summaries, history=None):
        type(self).overview_histories.append(history)
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
    FakeSummarizer.relevance = {}
    FakeSummarizer.rated = []
    FakeSummarizer.summarized = []
    FakeSummarizer.delay = 0.0
    FakeSummarizer.in_flight = 0
    FakeSummarizer.max_in_flight = 0
    FakeSummarizer.overview_histories = []
    FakeSummarizer.constructed = []
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
    # Safe default: every email send now runs a digest refresh first, which
    # must never reach the real arXiv from a test. Tests needing results
    # re-patch this.
    monkeypatch.setattr(temporal_activities, "search_arxiv", lambda query, since=None: [])
    # Same for Semantic Scholar: every send looks up author h-indices first.
    monkeypatch.setattr(temporal_activities, "fetch_max_author_h_index", lambda arxiv_ids: {})
    # The worker process's own wiring (app.worker.build_workers): the main
    # worker, and the arXiv worker that runs one request at a time.
    async with await WorkflowEnvironment.start_time_skipping() as env:
        with ThreadPoolExecutor(max_workers=20) as pool, ThreadPoolExecutor(max_workers=1) as arxiv_pool:
            main_worker, arxiv_worker = build_workers(env.client, TASK_QUEUE, pool, arxiv_pool, 20)
            async with main_worker, arxiv_worker:
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
    # Ingest rates; summaries and the overview are send prep's.
    assert crud.relevance_by_paper(db_session, topic.id, [p.id for p in refreshed.papers]) == {
        p.id: 5 for p in refreshed.papers
    }
    assert not any(p.summary for p in refreshed.papers)
    assert refreshed.overview is None

    refreshed_topic = db_session.get(models.Topic, topic.id)
    assert refreshed_topic.last_checked_at is not None


async def test_llm_activities_pass_a_spend_ledger_and_the_topic(db_session, temporal_client, monkeypatch):
    from app.spend_ledger import DbLedger

    topic = _make_topic(db_session)
    digest = crud.create_digest(db_session, topic.id)
    monkeypatch.setattr(
        temporal_activities, "search_arxiv", lambda query, since=None: [make_result("2408.0001", "Paper One")]
    )

    await _run_digest(temporal_client, digest.id)

    # rate_relevance: ingest's one LLM call per paper
    assert len(FakeSummarizer.constructed) == 1
    for kwargs in FakeSummarizer.constructed:
        assert isinstance(kwargs["ledger"], DbLedger)
        assert kwargs["topic_id"] == topic.id


async def test_at_100_percent_of_budget_digests_and_emails_go_out_without_ai_text(
    db_session, temporal_client, monkeypatch
):
    """The spend guard end to end: with this month's spend at the cap, the
    ingest, prep and send still run and email the new paper, with no OpenAI
    call and no AI-written text."""
    from decimal import Decimal

    import respx

    from app.config import get_settings
    from app.services.summarize import Summarizer

    settings = get_settings()
    monkeypatch.setattr(settings, "summary_backend", "openai")
    monkeypatch.setattr(settings, "openai_api_key", "test-key")
    monkeypatch.setattr(settings, "openai_base_url", "http://openai.test/v1")
    monkeypatch.setattr(settings, "rag_enabled", False)  # no embedding server in tests
    monkeypatch.setattr(temporal_activities, "Summarizer", Summarizer)
    monkeypatch.setattr(
        temporal_activities, "search_arxiv", lambda query, since=None: [make_result("2408.0001", "Paper One")]
    )

    topic = _make_topic(db_session)
    _make_subscription(
        db_session, topic, "reader@example.com", last_sent_at=datetime.now(timezone.utc) - timedelta(days=8)
    )
    db_session.add(
        models.LlmUsage(
            model="gpt-5.4-mini", purpose="summary", input_tokens=1, cached_input_tokens=0,
            output_tokens=1, cost_usd=Decimal("20"),
        )
    )
    db_session.commit()

    slot = latest_slot(datetime.now(timezone.utc))
    with respx.mock(assert_all_called=False) as router:
        openai = router.post("http://openai.test/v1/chat/completions")
        await temporal_client.execute_workflow(
            RunAllTopicDigestsWorkflow.run, id="ingest-over-budget", task_queue=TASK_QUEUE
        )
        await temporal_client.execute_workflow(
            PrepEditionsWorkflow.run, slot.isoformat(), id="prep-over-budget", task_queue=TASK_QUEUE
        )
        count = await _run_send_emails(temporal_client, "send-over-budget", slot)

    assert not openai.called
    assert count == 1
    (to, content), = FakeEmailSender.sent
    assert to == "reader@example.com"
    assert "Paper One" in content.text_body

    db_session.expire_all()
    paper = db_session.query(models.Paper).filter_by(arxiv_id="2408.0001").one()
    assert paper.summary is None
    assert crud.relevance_by_paper(db_session, topic.id, [paper.id]) == {}
    assert db_session.query(models.LlmUsage).count() == 1  # only the seeded row


class FakeEmbedder(EmbedderTextMixin):
    """Stands in for app.services.embed.Embedder (the activity calls
    `Embedder()` with no args). `fail` makes every call raise."""

    enabled = True
    model = "fake-embed"
    fail = False

    def embed(self, texts):
        if type(self).fail:
            raise RuntimeError("ollama embed is down")
        return [[1.0] + [0.0] * (models.EMBEDDING_DIM - 1) for _ in texts]


async def test_digest_workflow_embeds_new_papers(db_session, temporal_client, monkeypatch):
    monkeypatch.setattr(temporal_activities, "Embedder", FakeEmbedder)
    monkeypatch.setattr(FakeEmbedder, "fail", False)
    topic = _make_topic(db_session)
    digest = crud.create_digest(db_session, topic.id)
    monkeypatch.setattr(
        temporal_activities,
        "search_arxiv",
        lambda query, since=None: [make_result("2408.0001", "Paper One"), make_result("2408.0002", "Paper Two")],
    )

    await _run_digest(temporal_client, digest.id)

    db_session.expire_all()
    stored = db_session.query(models.PaperEmbedding).all()
    assert len(stored) == 2 and {e.model for e in stored} == {"fake-embed"}


async def test_digest_workflow_embeds_its_topic_once(db_session, temporal_client, monkeypatch):
    texts: list[str] = []

    class RecordingEmbedder(FakeEmbedder):
        def embed(self, batch):
            texts.extend(batch)
            return super().embed(batch)

    monkeypatch.setattr(temporal_activities, "Embedder", RecordingEmbedder)
    monkeypatch.setattr(FakeEmbedder, "fail", False)
    topic = _make_topic(db_session, name="RLHF", query="RLHF")
    monkeypatch.setattr(
        temporal_activities, "search_arxiv", lambda query, since=None: [make_result("2408.0001", "Paper One")]
    )
    await _run_digest(temporal_client, crud.create_digest(db_session, topic.id).id)
    monkeypatch.setattr(
        temporal_activities, "search_arxiv", lambda query, since=None: [make_result("2408.0002", "Paper Two")]
    )
    await _run_digest(temporal_client, crud.create_digest(db_session, topic.id).id)

    db_session.expire_all()
    (row,) = db_session.query(models.TopicEmbedding).all()
    assert (row.topic_id, row.model) == (topic.id, "fake-embed")
    topic_texts = [t for t in texts if "rlhf" in t and "Paper" not in t]
    assert topic_texts == ["task: search result | query: rlhf"]


async def test_digest_workflow_completes_when_embedding_fails(db_session, temporal_client, monkeypatch):
    """Retrieval is an enhancement: an embedding outage must not cost the
    digest its ratings or fail the run."""
    monkeypatch.setattr(temporal_activities, "Embedder", FakeEmbedder)
    monkeypatch.setattr(FakeEmbedder, "fail", True)
    topic = _make_topic(db_session)
    digest = crud.create_digest(db_session, topic.id)
    monkeypatch.setattr(
        temporal_activities, "search_arxiv", lambda query, since=None: [make_result("2408.0001", "Paper One")]
    )

    await _run_digest(temporal_client, digest.id)

    db_session.expire_all()
    refreshed = db_session.get(models.Digest, digest.id)
    assert refreshed.status == models.DigestStatus.completed
    assert refreshed.error is None
    assert _relevance(db_session, topic, "Paper One") == 5
    assert db_session.query(models.PaperEmbedding).count() == 0


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


async def test_digest_workflow_tolerates_partial_rating_failure(
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
    assert refreshed.error == "1 of 2 ratings failed"
    assert {p.title for p in refreshed.papers} == {"Paper One", "Paper Two"}
    assert _relevance(db_session, topic, "Paper One") == 5
    assert _relevance(db_session, topic, "Paper Two") is None  # left for prep to re-rate
    assert db_session.get(models.Topic, topic.id).last_checked_at is not None


async def test_digest_workflow_caps_concurrent_ratings(db_session, temporal_client, monkeypatch):
    """Each activity's timeout runs while it queues at the LLM backend, so
    the workflow never has more than llm_max_concurrency ratings in flight,
    and still rates every paper."""
    from app.config import get_settings

    monkeypatch.setattr(get_settings(), "llm_max_concurrency", 3)
    topic = _make_topic(db_session)
    digest = crud.create_digest(db_session, topic.id)
    monkeypatch.setattr(
        temporal_activities,
        "search_arxiv",
        lambda query, since=None: [make_result(f"2409.{i:04d}", f"Paper {i}") for i in range(12)],
    )
    FakeSummarizer.delay = 0.1  # long enough that uncapped calls would overlap

    await _run_digest(temporal_client, digest.id)

    assert FakeSummarizer.max_in_flight == 3
    assert len(FakeSummarizer.rated) == 12
    db_session.expire_all()
    refreshed = db_session.get(models.Digest, digest.id)
    assert len(refreshed.papers) == 12
    assert len(crud.relevance_by_paper(db_session, topic.id, [p.id for p in refreshed.papers])) == 12


async def test_digest_workflow_with_summaries_off_completes_with_no_summaries(
    db_session, temporal_client, monkeypatch
):
    """Uses the REAL Summarizer (undoing the fixture's FakeSummarizer patch) to
    prove no-summary mode end-to-end: conftest sets SUMMARY_BACKEND=none, so
    this exercises Summarizer's actual short-circuit, not a stand-in for it."""
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
    # Only topics with a confirmed, active subscriber are fetched.
    reader = make_subscriber(db_session)
    subscribe(db_session, rlhf, reader)
    subscribe(db_session, interp, reader)

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
        completed_at=generated_at,
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


async def _run_send_emails(client, run_id: str, slot=None) -> int:
    """The send at `slot` (default: the latest slot by the real clock - the
    time-skipping server's own clock can jump far ahead between workflows)."""
    slot = slot or latest_slot(datetime.now(timezone.utc))
    return await client.execute_workflow(
        SendDigestEmailsWorkflow.run, slot.isoformat(), id=run_id, task_queue=TASK_QUEUE
    )


async def _run_prep(client, topic, slot=None) -> str:
    slot = slot or latest_slot(datetime.now(timezone.utc))
    return await client.execute_workflow(
        PrepTopicEditionWorkflow.run, PrepTopicInput(topic_id=topic.id, slot=slot),
        id=f"prep-{topic.id}-{slot:%Y%m%dT%H%M}", task_queue=TASK_QUEUE,
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
    # Sent just now: no send slot can have passed since, whatever the weekday.
    just_sent = datetime.now(timezone.utc)
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
    # Weekday-proof: a weekly send 8 days ago always predates the latest
    # Monday slot; a biweekly send 6 days ago can never predate its cutoff
    # (latest Monday slot minus a week, so at least 7 days back).
    eight_days_ago = datetime.now(timezone.utc) - timedelta(days=8)
    six_days_ago = datetime.now(timezone.utc) - timedelta(days=6)
    _make_subscription(
        db_session, topic, "weekly@example.com", cadence="weekly", last_sent_at=eight_days_ago
    )
    _make_subscription(
        db_session, topic, "biweekly@example.com", cadence="biweekly", last_sent_at=six_days_ago
    )
    _make_completed_digest(
        db_session, topic, generated_at=eight_days_ago + timedelta(hours=1),
        papers=[("Paper One", "Summary one.")],
    )

    count = await _run_send_emails(temporal_client, "send-4")

    assert count == 1
    sent_to = {to for to, _ in FakeEmailSender.sent}
    assert sent_to == {"weekly@example.com"}  # biweekly skips a Monday in between


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


# ---------- delivery robustness (missed 2026-10-05 send) ----------
async def test_digest_still_running_during_a_send_goes_out_with_the_next_one(
    db_session, temporal_client
):
    """Delivery used to key off generated_at: a digest *started* before a
    send but completed after it fell behind the new watermark and was never
    emailed. (The two Oct 2 digests stuck pending across the reboot were
    exactly this shape.) It must land in the next email instead."""
    topic = _make_topic(db_session)
    sent_before = datetime.now(timezone.utc) - timedelta(days=4)
    sub = _make_subscription(
        db_session, topic, "me@example.com", cadence="twice_weekly", last_sent_at=sent_before
    )
    # Started before the previous send...
    straggler = _make_completed_digest(
        db_session, topic, generated_at=sent_before - timedelta(hours=1),
        papers=[("Straggler", "S.")],
    )
    # ...but only completed after it.
    straggler.completed_at = sent_before + timedelta(hours=1)
    db_session.commit()

    assert await _run_send_emails(temporal_client, "send-straggler") == 1
    [(_, content)] = FakeEmailSender.sent
    assert "Straggler" in content.text_body
    db_session.expire_all()
    assert db_session.get(models.Subscription, sub.id).last_sent_at > straggler.completed_at


def test_finalize_stamps_completed_at_only_on_completion(db_session):
    topic = _make_topic(db_session)
    done = crud.finalize_digest(
        db_session, crud.create_digest(db_session, topic.id), status=models.DigestStatus.completed
    )
    failed = crud.finalize_digest(
        db_session, crud.create_digest(db_session, topic.id),
        status=models.DigestStatus.failed, error="x",
    )

    assert done.completed_at is not None
    assert failed.completed_at is None


# ---------- cadence due-ness (slot-anchored, see app.cadence) ----------
# Slots are Mon/Thu 08:00 Eastern. October 2026 is EDT (UTC-4): Mon Oct 5 and
# Thu Oct 8 08:00 EDT are 12:00 UTC.
_PDT = timezone(timedelta(hours=-4))


def _pdt(day, hour=8, minute=0, second=0):
    return datetime(2026, 10, day, hour, minute, second, tzinfo=_PDT)


@pytest.mark.parametrize(
    "cadence, last_sent, now, due",
    [
        # Regression, 2026-10-08: a catch-up email sent Tue Oct 6 (after
        # Monday's slot was missed to downtime) made Thursday's run skip,
        # because only ~2 days had elapsed. Thursday's slot is after Tuesday's
        # send, so it's due.
        ("twice_weekly", _pdt(6, 8, 4, 27), _pdt(8, 8, 0, 3), True),
        # Normal rhythm: Mon send -> due at Thu slot, not before it.
        ("twice_weekly", _pdt(5, 8, 0, 5), _pdt(8, 8, 0, 3), True),
        ("twice_weekly", _pdt(5, 8, 0, 5), _pdt(8, 7, 59, 59), False),
        # The same slot firing twice (manual trigger + schedule) sends once.
        ("twice_weekly", _pdt(8, 8, 0, 5), _pdt(8, 8, 30), False),
        # A late catch-up fire (laptop woke at 13:00) still counts for its slot.
        ("twice_weekly", _pdt(5, 8, 0, 5), _pdt(8, 13, 0), True),
        # Weekly: Monday slots only - Thursday doesn't count.
        ("weekly", _pdt(5, 8, 0, 5), _pdt(8, 8, 0, 3), False),
        ("weekly", _pdt(5, 8, 0, 5), _pdt(12, 8, 0, 3), True),
        # A Tuesday catch-up send doesn't push weekly out a whole extra week.
        ("weekly", _pdt(6, 8, 4, 27), _pdt(12, 8, 0, 3), True),
        # Biweekly: skips the Monday in between, due the one after.
        ("biweekly", _pdt(5, 8, 0, 5), _pdt(12, 8, 0, 3), False),
        ("biweekly", _pdt(5, 8, 0, 5), _pdt(19, 8, 0, 3), True),
    ],
)
def test_list_due_subscriptions_cadence(db_session, cadence, last_sent, now, due):
    topic = _make_topic(db_session)
    _make_subscription(db_session, topic, "me@example.com", cadence=cadence, last_sent_at=last_sent)

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
    assert calendar.hour[0].start == 10
    assert desc.schedule.spec.time_zone_name == "America/New_York"
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
    assert desc.schedule.spec.time_zone_name == "America/New_York"
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


# ---------- ranking signals ----------
def _relevance(db_session, topic, title):
    paper = db_session.query(models.Paper).filter_by(title=title).one()
    return crud.relevance_by_paper(db_session, topic.id, [paper.id]).get(paper.id)


async def test_digest_workflow_stores_each_papers_relevance_to_the_topic(
    db_session, temporal_client, monkeypatch
):
    topic = _make_topic(db_session, name="PINNs", query="physics-informed neural networks")
    monkeypatch.setattr(
        temporal_activities,
        "search_arxiv",
        lambda query, since=None: [make_result("1", "Central"), make_result("2", "Tangential")],
    )
    FakeSummarizer.relevance = {"Central": 9, "Tangential": 2}

    await _run_digest(temporal_client, crud.create_digest(db_session, topic.id).id)

    db_session.expire_all()
    assert _relevance(db_session, topic, "Central") == 9
    assert _relevance(db_session, topic, "Tangential") == 2
    # One rating call per paper, made with the topic it's being rated for.
    assert sorted(FakeSummarizer.rated) == [("Central", "PINNs"), ("Tangential", "PINNs")]


async def test_a_paper_is_rated_separately_for_each_topic(
    db_session, temporal_client, monkeypatch
):
    """Relevance is per topic: a paper the second topic also fetches gets its
    own rating, and the first topic's rating is untouched."""
    rlhf = _make_topic(db_session, name="RLHF", query="rlhf")
    other = _make_topic(db_session, name="Alignment", query="alignment")
    monkeypatch.setattr(
        temporal_activities, "search_arxiv", lambda query, since=None: [make_result("1", "Shared")]
    )

    FakeSummarizer.relevance = {"Shared": 8}
    await _run_digest(temporal_client, crud.create_digest(db_session, rlhf.id).id)
    FakeSummarizer.relevance = {"Shared": 3}
    await _run_digest(temporal_client, crud.create_digest(db_session, other.id).id)

    db_session.expire_all()
    assert _relevance(db_session, rlhf, "Shared") == 8
    assert _relevance(db_session, other, "Shared") == 3
    assert FakeSummarizer.rated == [("Shared", "RLHF"), ("Shared", "Alignment")]


async def test_rerun_refreshes_an_ingested_papers_arxiv_comment(
    db_session, temporal_client, monkeypatch
):
    """Authors add "Accepted at ..." in a later version; the re-fetch inside
    the lookback window should pick it up even though the paper is skipped."""
    topic = _make_topic(db_session)
    result = make_result("1", "Paper")
    monkeypatch.setattr(temporal_activities, "search_arxiv", lambda query, since=None: [result])
    await _run_digest(temporal_client, crud.create_digest(db_session, topic.id).id)

    accepted = dataclasses.replace(result, comment="Accepted at ICLR 2027")
    monkeypatch.setattr(temporal_activities, "search_arxiv", lambda query, since=None: [accepted])
    await _run_digest(temporal_client, crud.create_digest(db_session, topic.id).id)

    db_session.expire_all()
    assert db_session.query(models.Paper).one().comment == "Accepted at ICLR 2027"


def _link_with_relevance(db_session, topic, title, relevance):
    paper = db_session.query(models.Paper).filter_by(title=title).one()
    paper.topics.append(topic)
    db_session.flush()
    crud.set_topic_paper_relevance(db_session, topic.id, paper.id, relevance)
    db_session.commit()


async def test_prep_ranks_the_cap_by_relevance_and_author_h_index(
    db_session, temporal_client, monkeypatch
):
    """h-index is looked up at prep, and the send keeps the edition's order."""
    last_sent = datetime.now(timezone.utc) - timedelta(days=4)
    topic = _make_topic(db_session)
    _make_subscription(
        db_session, topic, "me@example.com", cadence="twice_weekly",
        last_sent_at=last_sent, max_papers=2,
    )
    # Titles double as arxiv_ids in _make_completed_digest; listed newest first.
    _make_completed_digest(
        db_session, topic, generated_at=last_sent + timedelta(days=1),
        papers=[("Newest Tangential", "S."), ("Strong Group", "S."), ("Oldest Central", "S.")],
    )
    for title, relevance in [("Newest Tangential", 2), ("Strong Group", 7), ("Oldest Central", 10)]:
        _link_with_relevance(db_session, topic, title, relevance)
    looked_up = []

    def fake_scholar(arxiv_ids):
        looked_up.extend(arxiv_ids)
        return {"Newest Tangential": 4, "Strong Group": 60}  # "Oldest Central": not indexed

    monkeypatch.setattr(temporal_activities, "fetch_max_author_h_index", fake_scholar)

    assert await _run_prep(temporal_client, topic) == "written"
    assert await _run_send_emails(temporal_client, "send-ranked") == 1

    [(_, content)] = FakeEmailSender.sent
    assert "Strong Group" in content.text_body and "Oldest Central" in content.text_body
    assert "Newest Tangential" not in content.text_body
    assert "+ 1 more RLHF paper not shown (showing the top 2)." in content.text_body
    assert sorted(looked_up) == ["Newest Tangential", "Oldest Central", "Strong Group"]
    db_session.expire_all()
    assert db_session.query(models.Paper).filter_by(title="Strong Group").one().max_author_h_index == 60
    assert db_session.query(models.Paper).filter_by(title="Oldest Central").one().max_author_h_index is None


async def test_prep_still_writes_the_edition_when_semantic_scholar_is_down(
    db_session, temporal_client, monkeypatch
):
    last_sent = datetime.now(timezone.utc) - timedelta(days=4)
    topic = _make_topic(db_session)
    _make_subscription(db_session, topic, "me@example.com", cadence="twice_weekly", last_sent_at=last_sent)
    _make_completed_digest(
        db_session, topic, generated_at=last_sent + timedelta(days=1), papers=[("Paper", "S.")]
    )
    attempts = []

    def scholar_down(arxiv_ids):
        attempts.append(1)
        raise RuntimeError("429 Too Many Requests")

    monkeypatch.setattr(temporal_activities, "fetch_max_author_h_index", scholar_down)

    assert await _run_prep(temporal_client, topic) == "written"
    assert len(attempts) == 4  # _SCHOLAR_RETRY exhausted, then ranked without it
    assert await _run_send_emails(temporal_client, "send-scholar-down") == 1
    [(_, content)] = FakeEmailSender.sent
    assert "Paper" in content.text_body


async def test_send_digest_emails_skips_unconfirmed_subscriber(db_session, temporal_client):
    topic = _make_topic(db_session)
    eight_days_ago = datetime.now(timezone.utc) - timedelta(days=8)
    confirmed = make_subscriber(db_session, "confirmed@example.com")
    unconfirmed = make_subscriber(db_session, "pending@example.com", confirmed=False)
    subscribe(db_session, topic, confirmed, last_sent_at=eight_days_ago)
    subscribe(db_session, topic, unconfirmed, last_sent_at=eight_days_ago)
    _make_completed_digest(
        db_session, topic, generated_at=eight_days_ago + timedelta(hours=1),
        papers=[("Paper One", "Summary one.")],
    )

    count = await _run_send_emails(temporal_client, "send-unconfirmed")

    assert count == 1
    assert [to for to, _ in FakeEmailSender.sent] == ["confirmed@example.com"]
