"""Temporal worker entrypoint: `python -m app.worker`.

Connects to Temporal, ensures the daily digest schedule exists, registers
DigestWorkflow + RunAllTopicDigestsWorkflow and their Activities, and polls
the task queue until stopped (Ctrl-C / SIGTERM / container stop).
"""
from __future__ import annotations

import asyncio
import logging
from concurrent.futures import ThreadPoolExecutor

from temporalio.client import Client
from temporalio.worker import Worker

from app.config import get_settings
from app.temporal import activities
from app.temporal.schedule import ensure_daily_schedule, ensure_email_schedule
from app.temporal.workflows import (
    WORKFLOW_RUNNER,
    DigestWorkflow,
    RunAllTopicDigestsWorkflow,
    SendDigestEmailsWorkflow,
)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

ACTIVITIES = [
    activities.list_active_topic_ids,
    activities.create_pending_digest,
    activities.get_digest_context,
    activities.fetch_arxiv,
    activities.ingest_papers,
    activities.summarize_paper,
    activities.write_overview,
    activities.finalize_digest,
    activities.advance_watermark,
    activities.list_due_subscriptions,
    activities.gather_digest_content,
    activities.send_digest_email,
    activities.mark_subscription_sent,
    activities.count_pending_digests,
]


async def main() -> None:
    settings = get_settings()
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

    with ThreadPoolExecutor(max_workers=20) as activity_executor:
        worker = Worker(
            client,
            task_queue=settings.temporal_task_queue,
            workflows=[DigestWorkflow, RunAllTopicDigestsWorkflow, SendDigestEmailsWorkflow],
            activities=ACTIVITIES,
            activity_executor=activity_executor,
            workflow_runner=WORKFLOW_RUNNER,
        )
        logger.info("Worker polling task queue %r", settings.temporal_task_queue)
        await worker.run()


if __name__ == "__main__":
    asyncio.run(main())
