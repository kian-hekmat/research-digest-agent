from datetime import datetime, timezone

from app import cadence
from app.models import SubscriptionCadence


def _utc(*args):
    return datetime(*args, tzinfo=timezone.utc)


def test_latest_slot_is_the_most_recent_mon_or_thu_8am_eastern():
    # Fri Oct 9 2026, 07:31 EDT -> Thu Oct 8 08:00 EDT (12:00 UTC).
    assert cadence.latest_slot(_utc(2026, 10, 9, 11, 31)) == _utc(2026, 10, 8, 12, 0)


def test_a_slot_counts_from_the_exact_send_time():
    assert cadence.latest_slot(_utc(2026, 10, 8, 12, 0)) == _utc(2026, 10, 8, 12, 0)
    # One second before Thursday's slot, the latest is still Monday's.
    assert cadence.latest_slot(_utc(2026, 10, 8, 11, 59, 59)) == _utc(2026, 10, 5, 12, 0)


def test_slots_stay_at_8am_local_across_daylight_saving():
    """DST ends Sun Nov 1 2026: Thursday's slot is 08:00 EDT (12:00 UTC),
    the next Monday's is 08:00 EST (13:00 UTC)."""
    assert cadence.latest_slot(_utc(2026, 10, 30, 0, 0)) == _utc(2026, 10, 29, 12, 0)
    assert cadence.latest_slot(_utc(2026, 11, 2, 13, 0)) == _utc(2026, 11, 2, 13, 0)
    assert cadence.latest_slot(_utc(2026, 11, 2, 12, 30)) == _utc(2026, 10, 29, 12, 0)


def test_biweekly_cutoff_is_a_week_before_the_latest_monday():
    now = _utc(2026, 10, 19, 12, 0, 3)  # Mon Oct 19, just after the slot
    assert cadence.due_cutoff(SubscriptionCadence.biweekly, now) == _utc(2026, 10, 12, 12, 0)


def test_email_schedule_is_built_from_the_same_slots():
    """The Temporal cron and the due check must describe the same send
    times; a slot the schedule never fires would leave subscribers waiting."""
    from app.temporal import schedule

    assert cadence.email_cron() == "0 8 * * 1,4"
    assert schedule.EMAIL_CRON == cadence.email_cron()
    assert schedule.SCHEDULE_TIME_ZONE == cadence.SEND_TIME_ZONE_NAME


def test_next_slot_is_strictly_after():
    from zoneinfo import ZoneInfo

    et = ZoneInfo("America/New_York")

    def at(*args):
        return datetime(*args, tzinfo=et)

    # Mon Oct 5 2026 08:00 ET exactly -> Thu Oct 8 08:00 ET.
    assert cadence.next_slot(at(2026, 10, 5, 8, 0)) == at(2026, 10, 8, 8, 0)
    # One minute earlier -> the same Monday.
    assert cadence.next_slot(at(2026, 10, 5, 7, 59)) == at(2026, 10, 5, 8, 0)
    # Friday -> Monday.
    assert cadence.next_slot(at(2026, 10, 9, 12, 0)) == at(2026, 10, 12, 8, 0)
    # Aware result in SEND_TIME_ZONE, also for a UTC input.
    result = cadence.next_slot(_utc(2026, 10, 5, 12, 0))
    assert result == at(2026, 10, 8, 8, 0)
    assert result.tzinfo == cadence.SEND_TIME_ZONE
    # Across the DST end (Nov 1): Thu Oct 29 EDT -> Mon Nov 2 08:00 EST.
    assert cadence.next_slot(at(2026, 10, 29, 8, 0)) == at(2026, 11, 2, 8, 0)
