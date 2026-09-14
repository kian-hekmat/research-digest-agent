"""Idempotent setup of the daily "run every active topic" Temporal Schedule.

Called by the worker on startup; safe to call every time - it's a no-op once
the schedule already exists.
"""
from __future__ import annotations

from temporalio.client import (
    Client,
    Schedule,
    ScheduleActionStartWorkflow,
    ScheduleOverlapPolicy,
    SchedulePolicy,
    ScheduleSpec,
)
from temporalio.service import RPCError, RPCStatusCode

from app.config import get_settings
from app.temporal.workflows import RunAllTopicDigestsWorkflow

DAILY_SCHEDULE_ID = "daily-topic-digests"
DEFAULT_CRON = "0 6 * * *"  # 06:00 UTC daily


async def ensure_daily_schedule(client: Client, cron: str = DEFAULT_CRON) -> bool:
    """Create the daily digest schedule if it doesn't exist yet.

    Returns True if it created the schedule, False if one was already there.

    Each firing runs `RunAllTopicDigestsWorkflow`, which fans out a
    `DigestWorkflow` child per active topic. Temporal appends the fire time to
    the configured action id (`scheduled-topic-digests-<timestamp>`), so
    repeated or backfilled fires never collide. Overlap policy SKIP: if a
    previous run is still going when the next fire time arrives, skip it
    rather than pile another one on top.
    """
    handle = client.get_schedule_handle(DAILY_SCHEDULE_ID)
    try:
        await handle.describe()
        return False  # already exists
    except RPCError as exc:
        if exc.status != RPCStatusCode.NOT_FOUND:
            raise

    settings = get_settings()
    await client.create_schedule(
        DAILY_SCHEDULE_ID,
        Schedule(
            action=ScheduleActionStartWorkflow(
                RunAllTopicDigestsWorkflow.run,
                id="scheduled-topic-digests",
                task_queue=settings.temporal_task_queue,
            ),
            spec=ScheduleSpec(cron_expressions=[cron]),
            policy=SchedulePolicy(overlap=ScheduleOverlapPolicy.SKIP),
        ),
    )
    return True
