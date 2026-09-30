"""Idempotent setup of the Temporal Schedules the worker relies on.

Called by the worker on startup; safe to call every time. Creates each
schedule if it's missing, and - since Temporal state now persists across
restarts - also brings an existing schedule's cron up to date if the code's
cron has changed since it was created (otherwise a changed cron constant
would silently never take effect).
"""
from __future__ import annotations

from typing import Any, Callable, Literal

from temporalio.client import (
    Client,
    Schedule,
    ScheduleActionStartWorkflow,
    ScheduleOverlapPolicy,
    SchedulePolicy,
    ScheduleSpec,
    ScheduleState,
    ScheduleUpdate,
    ScheduleUpdateInput,
)
from temporalio.service import RPCError, RPCStatusCode

from app.config import get_settings
from app.temporal.workflows import RunAllTopicDigestsWorkflow, SendDigestEmailsWorkflow

DAILY_SCHEDULE_ID = "daily-topic-digests"
DAILY_CRON = "0 6 * * *"  # 06:00 UTC daily

# The id predates the move to twice-weekly sends; it's kept so existing
# deployments update their schedule in place instead of gaining a second one.
EMAIL_SCHEDULE_ID = "weekly-digest-emails"
# SendDigestEmailsWorkflow judges each subscription against its own
# twice_weekly/weekly/biweekly cadence internally (see that workflow's
# docstring) - the schedule just needs to fire at least as often as the
# most frequent cadence.
EMAIL_CRON = "0 8 * * 1,4"  # Mondays and Thursdays 08:00 UTC

EnsureResult = Literal["created", "updated", "unchanged"]


def _cron_note(cron: str) -> str:
    # The server normalizes cron strings into calendar specs rather than
    # echoing them back, so the cron a schedule was built from is recorded
    # in its note to compare against on the next startup.
    return f"cron: {cron}"


async def _ensure_schedule(
    client: Client, schedule_id: str, action_id: str, workflow: Callable[..., Any], cron: str
) -> EnsureResult:
    """Create `schedule_id` targeting `workflow`, or update its cron in place
    if it exists with a different one.

    Temporal appends the fire time to `action_id` for each actual run, so
    repeated or backfilled fires never collide on workflow id. Overlap policy
    SKIP: if a previous run is still going when the next fire time arrives,
    skip it rather than pile another one on top.
    """
    handle = client.get_schedule_handle(schedule_id)
    try:
        desc = await handle.describe()
    except RPCError as exc:
        if exc.status != RPCStatusCode.NOT_FOUND:
            raise
    else:
        if desc.schedule.state.note == _cron_note(cron):
            return "unchanged"

        def _updater(input: ScheduleUpdateInput) -> ScheduleUpdate:
            schedule = input.description.schedule
            schedule.spec = ScheduleSpec(cron_expressions=[cron])
            schedule.state.note = _cron_note(cron)
            return ScheduleUpdate(schedule=schedule)

        await handle.update(_updater)
        return "updated"

    settings = get_settings()
    await client.create_schedule(
        schedule_id,
        Schedule(
            action=ScheduleActionStartWorkflow(
                workflow, id=action_id, task_queue=settings.temporal_task_queue
            ),
            spec=ScheduleSpec(cron_expressions=[cron]),
            policy=SchedulePolicy(overlap=ScheduleOverlapPolicy.SKIP),
            state=ScheduleState(note=_cron_note(cron)),
        ),
    )
    return "created"


async def ensure_daily_schedule(client: Client, cron: str = DAILY_CRON) -> EnsureResult:
    """Each firing runs `RunAllTopicDigestsWorkflow`, which fans out a
    `DigestWorkflow` child per active topic."""
    return await _ensure_schedule(
        client, DAILY_SCHEDULE_ID, "scheduled-topic-digests", RunAllTopicDigestsWorkflow.run, cron
    )


async def ensure_email_schedule(client: Client, cron: str = EMAIL_CRON) -> EnsureResult:
    """Each firing runs `SendDigestEmailsWorkflow`, which delivers to every
    subscription whose cadence is due."""
    return await _ensure_schedule(
        client, EMAIL_SCHEDULE_ID, "scheduled-digest-emails", SendDigestEmailsWorkflow.run, cron
    )
