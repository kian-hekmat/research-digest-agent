"""Temporal worker entrypoint: `python -m app.worker`.

Connects to Temporal, ensures the schedules exist, and runs two workers in
this one process until stopped (Ctrl-C / SIGTERM / container stop):

- the main worker, on TEMPORAL_TASK_QUEUE: every workflow and every activity
  except arXiv's, on `worker_activity_threads` threads;
- the arXiv worker, on `arxiv_queue(TEMPORAL_TASK_QUEUE)`: `fetch_arxiv` and
  `fetch_fulltext`, one at a time, so there's never more than one arXiv
  request in flight however many topics ingest at once.
"""
from __future__ import annotations

import asyncio
import logging
from concurrent.futures import ThreadPoolExecutor

from temporalio.client import Client
from temporalio.worker import Worker

from app.config import MissingCalibration, Settings, get_settings
from app.temporal import activities
from app.temporal.schedule import ensure_daily_schedule, ensure_email_schedule
from app.temporal.workflows import (
    WORKFLOW_RUNNER,
    DigestWorkflow,
    RunAllTopicDigestsWorkflow,
    SendDigestEmailsWorkflow,
    arxiv_queue,
)

logger = logging.getLogger(__name__)

WORKFLOWS = [DigestWorkflow, RunAllTopicDigestsWorkflow, SendDigestEmailsWorkflow]

ACTIVITIES = [
    activities.list_active_topic_ids,
    activities.get_pipeline_limits,
    activities.create_pending_digest,
    activities.get_digest_context,
    activities.ingest_papers,
    activities.embed_papers,
    activities.summarize_paper,
    activities.rate_relevance,
    activities.write_overview,
    activities.finalize_digest,
    activities.advance_watermark,
    activities.send_pipeline_alert,
    activities.list_due_subscriptions,
    activities.enrich_author_h_index,
    activities.gather_digest_content,
    activities.send_digest_email,
    activities.mark_subscription_sent,
    activities.count_pending_digests,
]

# Every activity that sends a request to arXiv.
ARXIV_ACTIVITIES = [activities.fetch_arxiv, activities.fetch_fulltext]


def build_workers(
    client: Client,
    task_queue: str,
    activity_executor: ThreadPoolExecutor,
    arxiv_executor: ThreadPoolExecutor,
    activity_threads: int,
) -> tuple[Worker, Worker]:
    """The main worker and the arXiv worker for `task_queue`. The main one
    takes on no more activities than it has threads (`activity_threads`, the
    size of `activity_executor`), so an activity's timeout never runs while it
    waits for one."""
    main = Worker(
        client,
        task_queue=task_queue,
        workflows=WORKFLOWS,
        activities=ACTIVITIES,
        activity_executor=activity_executor,
        max_concurrent_activities=activity_threads,
        workflow_runner=WORKFLOW_RUNNER,
    )
    arxiv = Worker(
        client,
        task_queue=arxiv_queue(task_queue),
        activities=ARXIV_ACTIVITIES,
        activity_executor=arxiv_executor,
        max_concurrent_activities=1,
        workflow_runner=WORKFLOW_RUNNER,
    )
    return main, arxiv


def check_config(settings: Settings) -> None:
    """Fail at startup, where it's seen, for config that would otherwise
    only break at the next scheduled run: ranking needs the embedding model's
    calibration on every email, embeddings on or off."""
    try:
        settings.calibration
    except MissingCalibration as exc:
        raise SystemExit(f"Refusing to start: {exc}") from exc


async def main() -> None:
    logging.basicConfig(level=logging.INFO)
    settings = get_settings()
    check_config(settings)
    client = await Client.connect(
        settings.temporal_address, namespace=settings.temporal_namespace
    )
    logger.info(
        "Connected to Temporal at %s (namespace=%s)",
        settings.temporal_address,
        settings.temporal_namespace,
    )

    logger.info("Daily digest schedule: %s", await ensure_daily_schedule(client))
    logger.info("Digest email schedule: %s", await ensure_email_schedule(client))

    threads = settings.worker_activity_threads
    with (
        ThreadPoolExecutor(max_workers=threads) as activity_executor,
        ThreadPoolExecutor(max_workers=1) as arxiv_executor,
    ):
        main_worker, arxiv_worker = build_workers(
            client, settings.temporal_task_queue, activity_executor, arxiv_executor, threads
        )
        logger.info(
            "Workers polling task queues %r and %r (one arXiv request at a time)",
            settings.temporal_task_queue,
            arxiv_queue(settings.temporal_task_queue),
        )
        await asyncio.gather(main_worker.run(), arxiv_worker.run())


if __name__ == "__main__":
    asyncio.run(main())
