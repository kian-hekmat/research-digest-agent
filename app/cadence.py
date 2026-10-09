"""When each subscription cadence is due for an email.

Due-ness is anchored to the send *slots* - the scheduled send times - not to
time elapsed since the last send. A subscription is due when it hasn't been
sent anything since its cadence's most recent slot:

- twice_weekly: every slot (Mondays and Thursdays)
- weekly:       Monday slots
- biweekly:     Monday slots, skipping one in between

Why not "N days since the last send": that rule let any off-schedule send push
the next one back. A catch-up email sent on a Tuesday (after a Monday missed
to downtime) made Thursday's run see only ~2 days elapsed - less than the
twice-weekly interval - and skip, so the next email slipped to the following
Monday. Anchored to slots, Thursday's slot is after Tuesday's send, so it's
due. It also makes a slot that fires twice (a manual trigger plus the
schedule, or a retry) send only once.

This module is the single source of truth for the send times: the Temporal
email schedule's cron is built from the same constants (app.temporal.schedule).
Pure functions, no I/O.
"""
from __future__ import annotations

from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

from app.models import SubscriptionCadence

SEND_TIME_ZONE_NAME = "America/Los_Angeles"
SEND_TIME_ZONE = ZoneInfo(SEND_TIME_ZONE_NAME)
SEND_HOUR = 8
SEND_WEEKDAYS = (0, 3)  # datetime.weekday(): Monday, Thursday
_WEEKLY_WEEKDAYS = (0,)  # Monday


def latest_slot(now: datetime, weekdays: tuple[int, ...] = SEND_WEEKDAYS) -> datetime:
    """The most recent send time at or before `now` on one of `weekdays`,
    in SEND_TIME_ZONE (DST-aware: always 08:00 local wall-clock time)."""
    local_now = now.astimezone(SEND_TIME_ZONE)
    for days_back in range(8):
        day = (local_now - timedelta(days=days_back)).date()
        if day.weekday() in weekdays:
            slot = datetime.combine(day, time(SEND_HOUR), tzinfo=SEND_TIME_ZONE)
            if slot <= local_now:
                return slot
    raise ValueError(f"no send weekday in {weekdays}")


def due_cutoff(cadence: SubscriptionCadence, now: datetime) -> datetime:
    """A subscription is due iff its last send is before this instant."""
    if cadence == SubscriptionCadence.twice_weekly:
        return latest_slot(now, SEND_WEEKDAYS)
    if cadence == SubscriptionCadence.weekly:
        return latest_slot(now, _WEEKLY_WEEKDAYS)
    if cadence == SubscriptionCadence.biweekly:
        # Sent at Monday slot S: at the next Monday S+7, the cutoff is S
        # itself, and the send (at or after S) isn't before it - not due.
        # At S+14 the cutoff is S+7, so it is. No calendar parity needed, and
        # a late or off-schedule send just counts from wherever it landed.
        return latest_slot(now, _WEEKLY_WEEKDAYS) - timedelta(days=7)
    raise ValueError(f"unknown cadence {cadence!r}")


def is_due(cadence: SubscriptionCadence, last_sent_at: datetime, now: datetime) -> bool:
    return last_sent_at < due_cutoff(cadence, now)


def email_cron() -> str:
    """The Temporal cron for the send slots. Cron weekdays count Sunday=0,
    so Monday is 1 (datetime.weekday() + 1)."""
    days = ",".join(str(d + 1) for d in SEND_WEEKDAYS)
    return f"0 {SEND_HOUR} * * {days}"
