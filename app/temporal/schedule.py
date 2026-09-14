"""Idempotent setup of the Temporal Schedules the worker relies on.

Called by the worker on startup; safe to call every time - each is a no-op
once its schedule already exists.
"""
from __future__ import annotations

from typing import Any, Callable

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
from app.temporal.workflows import RunAllTopicDigestsWorkflow, SendDigestEmailsWorkflow

DAILY_SCHEDULE_ID = "daily-topic-digests"
DAILY_CRON = "0 6 * * *"  # 06:00 UTC daily

EMAIL_SCHEDULE_ID = "weekly-digest-emails"
# Fires weekly; SendDigestEmailsWorkflow judges each subscription against its
# own weekly/biweekly cadence internally (see that workflow's docstring) -
# cron itself has no native "every other week".
EMAIL_CRON = "0 8 * * 1"  # Mondays 08:00 UTC


async def _ensure_schedule(
    client: Client, schedule_id: str, action_id: str, workflow: Callable[..., Any], cron: str
) -> bool:
    """Create `schedule_id` targeting `workflow` if it doesn't exist yet.

    Returns True if it created the schedule, False if one was already there.
    Temporal appends the fire time to `action_id` for each actual run, so
    repeated or backfilled fires never collide on workflow id. Overlap policy
    SKIP: if a previous run is still going when the next fire time arrives,
    skip it rather than pile another one on top.
    """
    handle = client.get_schedule_handle(schedule_id)
    try:
        await handle.describe()
        return False  # already exists
    except RPCError as exc:
        if exc.status != RPCStatusCode.NOT_FOUND:
            raise

    settings = get_settings()
    await client.create_schedule(
        schedule_id,
        Schedule(
            action=ScheduleActionStartWorkflow(
                workflow, id=action_id, task_queue=settings.temporal_task_queue
            ),
            spec=ScheduleSpec(cron_expressions=[cron]),
            policy=SchedulePolicy(overlap=ScheduleOverlapPolicy.SKIP),
        ),
    )
    return True


async def ensure_daily_schedule(client: Client, cron: str = DAILY_CRON) -> bool:
    """Each firing runs `RunAllTopicDigestsWorkflow`, which fans out a
    `DigestWorkflow` child per active topic."""
    return await _ensure_schedule(
        client, DAILY_SCHEDULE_ID, "scheduled-topic-digests", RunAllTopicDigestsWorkflow.run, cron
    )


async def ensure_weekly_email_schedule(client: Client, cron: str = EMAIL_CRON) -> bool:
    """Each firing runs `SendDigestEmailsWorkflow`, which delivers to every
    subscription whose cadence is due."""
    return await _ensure_schedule(
        client, EMAIL_SCHEDULE_ID, "scheduled-digest-emails", SendDigestEmailsWorkflow.run, cron
    )
