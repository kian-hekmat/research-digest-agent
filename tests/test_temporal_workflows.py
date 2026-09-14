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

from concurrent.futures import ThreadPoolExecutor

import pytest
import pytest_asyncio
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

from app import crud, models
from app.temporal import activities as temporal_activities
from app.temporal.schedule import DAILY_SCHEDULE_ID, ensure_daily_schedule
from app.temporal.workflows import WORKFLOW_RUNNER, DigestWorkflow, RunAllTopicDigestsWorkflow
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
]


class FakeSummarizer:
    """Stands in for app.services.summarize.Summarizer. `fail_titles` is a
    class attribute (the activity calls `Summarizer()` with no args, so a
    test configures failures by mutating the class, not an instance)."""

    fail_titles: set[str] = set()

    def summarize_paper(self, title, abstract):
        if title in type(self).fail_titles:
            raise RuntimeError(f"summary failed for {title}")
        return f"Summary of {title}."

    def write_overview(self, topic_name, summaries):
        return f"Overview of {topic_name}: {len(summaries)} papers."


@pytest.fixture(autouse=True)
def _reset_fake_summarizer():
    FakeSummarizer.fail_titles = set()
    yield
    FakeSummarizer.fail_titles = set()


@pytest_asyncio.fixture
async def temporal_client(monkeypatch):
    monkeypatch.setattr(temporal_activities, "Summarizer", FakeSummarizer)
    async with await WorkflowEnvironment.start_time_skipping() as env:
        with ThreadPoolExecutor(max_workers=8) as pool:
            async with Worker(
                env.client,
                task_queue=TASK_QUEUE,
                workflows=[DigestWorkflow, RunAllTopicDigestsWorkflow],
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


async def test_digest_workflow_rerun_dedupes_and_passes_watermark(
    db_session, temporal_client, monkeypatch
):
    topic = _make_topic(db_session)
    calls = []

    def fetch(query, since=None):
        calls.append(since)
        return [make_result("2408.0001", "Paper One"), make_result("2408.0002", "Paper Two")]

    monkeypatch.setattr(temporal_activities, "search_arxiv", fetch)

    first = crud.create_digest(db_session, topic.id)
    await _run_digest(temporal_client, first.id)

    second = crud.create_digest(db_session, topic.id)
    await _run_digest(temporal_client, second.id)

    db_session.expire_all()
    assert db_session.query(models.Paper).count() == 2  # deduped on arxiv_id
    second_refreshed = db_session.get(models.Digest, second.id)
    assert {p.title for p in second_refreshed.papers} == {"Paper One", "Paper Two"}

    assert calls[0] is None  # first run: no watermark yet
    assert calls[1] is not None  # second run: watermark from the first


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
    created_first = await ensure_daily_schedule(client)
    created_second = await ensure_daily_schedule(client)

    assert created_first is True
    assert created_second is False

    handle = client.get_schedule_handle(DAILY_SCHEDULE_ID)
    desc = await handle.describe()
    # The server normalizes the cron string into a calendar spec rather than
    # echoing it back verbatim - check the parsed hour instead of the string.
    [calendar] = desc.schedule.spec.calendars
    assert calendar.hour[0].start == 6
    assert desc.schedule.action.workflow == "RunAllTopicDigestsWorkflow"
