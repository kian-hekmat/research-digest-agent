from datetime import datetime, timezone

from app import cadence
from app.models import SubscriptionCadence


def _utc(*args):
    return datetime(*args, tzinfo=timezone.utc)


def test_latest_slot_is_the_most_recent_mon_or_thu_8am_pacific():
    # Fri Oct 9 2026, 07:31 PDT -> Thu Oct 8 08:00 PDT (15:00 UTC).
    assert cadence.latest_slot(_utc(2026, 10, 9, 14, 31)) == _utc(2026, 10, 8, 15, 0)


def test_a_slot_counts_from_the_exact_send_time():
    assert cadence.latest_slot(_utc(2026, 10, 8, 15, 0)) == _utc(2026, 10, 8, 15, 0)
    # One second before Thursday's slot, the latest is still Monday's.
    assert cadence.latest_slot(_utc(2026, 10, 8, 14, 59, 59)) == _utc(2026, 10, 5, 15, 0)


def test_slots_stay_at_8am_local_across_daylight_saving():
    """DST ends Sun Nov 1 2026: Thursday's slot is 08:00 PDT (15:00 UTC),
    the next Monday's is 08:00 PST (16:00 UTC)."""
    assert cadence.latest_slot(_utc(2026, 10, 30, 0, 0)) == _utc(2026, 10, 29, 15, 0)
    assert cadence.latest_slot(_utc(2026, 11, 2, 16, 0)) == _utc(2026, 11, 2, 16, 0)
    assert cadence.latest_slot(_utc(2026, 11, 2, 15, 30)) == _utc(2026, 10, 29, 15, 0)


def test_biweekly_cutoff_is_a_week_before_the_latest_monday():
    now = _utc(2026, 10, 19, 15, 0, 3)  # Mon Oct 19, just after the slot
    assert cadence.due_cutoff(SubscriptionCadence.biweekly, now) == _utc(2026, 10, 12, 15, 0)


def test_email_schedule_is_built_from_the_same_slots():
    """The Temporal cron and the due check must describe the same send
    times; a slot the schedule never fires would leave subscribers waiting."""
    from app.temporal import schedule

    assert cadence.email_cron() == "0 8 * * 1,4"
    assert schedule.EMAIL_CRON == cadence.email_cron()
    assert schedule.SCHEDULE_TIME_ZONE == cadence.SEND_TIME_ZONE_NAME
