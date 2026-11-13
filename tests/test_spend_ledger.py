"""Month-to-date spend, the spend state, and DbLedger against real Postgres."""
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from app import models
from app.config import get_settings
from app.services.spend import Usage
from app.spend_ledger import DbLedger, month_start, month_to_date_usd, spend_state

NOV = datetime(2026, 11, 12, 15, tzinfo=timezone.utc)


class FakeSender:
    def __init__(self, fail: bool = False) -> None:
        self.sent: list[tuple[str, str]] = []
        self.fail = fail

    def send(self, to, content):
        if self.fail:
            raise OSError("smtp down")
        self.sent.append((to, content.subject))


def _usage_row(db, when, cost, purpose="summary", topic_id=None):
    db.add(
        models.LlmUsage(
            created_at=when,
            model="gpt-5.4-mini",
            purpose=purpose,
            topic_id=topic_id,
            input_tokens=1,
            cached_input_tokens=0,
            output_tokens=1,
            cost_usd=Decimal(cost),
        )
    )
    db.commit()


@pytest.fixture
def alerting(monkeypatch):
    """Alerts on, delivered to a fake sender; yields the sender."""
    monkeypatch.setattr(get_settings(), "alert_email", "owner@example.com")
    sender = FakeSender()
    monkeypatch.setattr("app.alerts._default_sender", lambda: sender)
    return sender


def _ledger(now=NOV):
    return DbLedger(now=lambda: now)


def test_month_start_is_utc():
    # 20:00 Pacific on Oct 31 is already November in UTC.
    late_oct_pacific = datetime(2026, 11, 1, 3, tzinfo=timezone.utc)
    assert month_start(late_oct_pacific) == datetime(2026, 11, 1, tzinfo=timezone.utc)


def test_month_to_date_counts_only_this_utc_month(db_session):
    _usage_row(db_session, datetime(2026, 10, 31, 23, 59, tzinfo=timezone.utc), "5")
    _usage_row(db_session, datetime(2026, 11, 1, 0, 0, tzinfo=timezone.utc), "2")
    assert month_to_date_usd(db_session, NOV) == Decimal("2")


def test_month_to_date_is_zero_with_no_rows(db_session):
    assert month_to_date_usd(db_session, NOV) == Decimal("0")


@pytest.mark.parametrize(
    "spent,state",
    [("15.99", "ok"), ("16", "soft"), ("19.99", "soft"), ("20", "hard"), ("25", "hard")],
)
def test_spend_state_thresholds(db_session, spent, state):
    _usage_row(db_session, NOV, spent)
    assert spend_state(db_session, NOV) == state


def test_record_writes_one_row_with_cost_and_topic(db_session):
    topic = models.Topic(name="RLHF", query="rlhf")
    db_session.add(topic)
    db_session.commit()

    _ledger().record(model="gpt-5.4-mini", purpose="rating", topic_id=topic.id, usage=Usage(400, 0, 5))

    rows = db_session.query(models.LlmUsage).all()
    assert len(rows) == 1
    row = rows[0]
    assert (row.purpose, row.topic_id, row.model) == ("rating", topic.id, "gpt-5.4-mini")
    assert (row.input_tokens, row.cached_input_tokens, row.output_tokens) == (400, 0, 5)
    assert row.cost_usd == Decimal("0.000323")
    assert row.estimated is False


def test_record_keeps_estimated_flag(db_session):
    _ledger().record(model="gpt-5.4-mini", purpose="summary", topic_id=None, usage=Usage(10, 0, 400, estimated=True))
    assert db_session.query(models.LlmUsage).one().estimated is True


def test_deleting_a_topic_keeps_its_spend(db_session):
    topic = models.Topic(name="RLHF", query="rlhf")
    db_session.add(topic)
    db_session.commit()
    _usage_row(db_session, NOV, "1", topic_id=topic.id)

    db_session.delete(topic)
    db_session.commit()

    row = db_session.query(models.LlmUsage).one()
    assert row.topic_id is None and row.cost_usd == Decimal("1")


def test_allow_blocks_chat_purposes_at_100_but_never_embeddings(db_session):
    _usage_row(db_session, NOV, "20")
    ledger = _ledger()
    assert ledger.allow("summary") is False
    assert ledger.allow("rating") is False
    assert ledger.allow("overview") is False
    assert ledger.allow("embedding") is True

    next_month = _ledger(datetime(2026, 12, 1, 0, 0, tzinfo=timezone.utc))
    assert next_month.allow("summary") is True


def test_existing_work_still_runs_at_80(db_session):
    _usage_row(db_session, NOV, "16")
    assert spend_state(db_session, NOV) == "soft"
    ledger = _ledger()
    assert all(ledger.allow(p) for p in ("summary", "rating", "overview"))


def test_crossing_80_sends_one_alert_even_when_recorded_twice(db_session, alerting):
    _usage_row(db_session, NOV, "15.99")
    ledger = _ledger()
    big = Usage(0, 0, 2_300)  # $0.01035 each at $4.50 / 1M output tokens

    ledger.record(model="gpt-5.4-mini", purpose="summary", topic_id=None, usage=big)
    ledger.record(model="gpt-5.4-mini", purpose="summary", topic_id=None, usage=big)

    assert len(alerting.sent) == 1
    assert alerting.sent[0][0] == "owner@example.com"
    assert "80%" in alerting.sent[0][1]
    keys = [a.key for a in db_session.query(models.SentAlert).all()]
    assert keys == ["spend-80:2026-11"]


def test_crossing_100_sends_both_alerts_once(db_session, alerting):
    _usage_row(db_session, NOV, "19.999")
    ledger = _ledger()
    ledger.record(model="gpt-5.4-mini", purpose="summary", topic_id=None, usage=Usage(0, 0, 2_300))
    ledger.record(model="gpt-5.4-mini", purpose="summary", topic_id=None, usage=Usage(0, 0, 2_300))

    keys = sorted(a.key for a in db_session.query(models.SentAlert).all())
    assert keys == ["spend-100:2026-11", "spend-80:2026-11"]
    assert len(alerting.sent) == 2
    assert any("100%" in subject for _, subject in alerting.sent)


def test_alerts_rearm_next_month(db_session, alerting):
    _usage_row(db_session, NOV, "16")
    _ledger().record(model="gpt-5.4-mini", purpose="summary", topic_id=None, usage=Usage(1, 0, 1))
    dec = datetime(2026, 12, 20, tzinfo=timezone.utc)
    _usage_row(db_session, dec, "16")
    _ledger(dec).record(model="gpt-5.4-mini", purpose="summary", topic_id=None, usage=Usage(1, 0, 1))
    assert sorted(a.key for a in db_session.query(models.SentAlert).all()) == [
        "spend-80:2026-11",
        "spend-80:2026-12",
    ]


def test_hard_limit_hit_alerts_once_per_month(db_session, alerting):
    ledger = _ledger()
    ledger.hard_limit_hit()
    ledger.hard_limit_hit()
    assert len(alerting.sent) == 1
    assert [a.key for a in db_session.query(models.SentAlert).all()] == ["openai-hard-limit:2026-11"]


def test_failed_alert_never_fails_record(db_session, monkeypatch):
    monkeypatch.setattr(get_settings(), "alert_email", "owner@example.com")
    monkeypatch.setattr("app.alerts._default_sender", lambda: FakeSender(fail=True))
    _usage_row(db_session, NOV, "16")

    _ledger().record(model="gpt-5.4-mini", purpose="summary", topic_id=None, usage=Usage(1, 0, 1))

    assert db_session.query(models.LlmUsage).count() == 2
    assert db_session.query(models.SentAlert).count() == 0


def test_80_percent_alert_claims_nothing_that_isnt_enforced_yet(db_session, monkeypatch):
    # Waitlisting new topics arrives with public signup (Phase 3); until then
    # the alert must not say topics are waiting.
    monkeypatch.setattr(get_settings(), "alert_email", "owner@example.com")
    bodies = []

    def capture(db, key, subject, body, **kwargs):
        bodies.append((key, body))
        return True

    _usage_row(db_session, NOV, "16")
    DbLedger(now=lambda: NOV, alert=capture).record(
        model="gpt-5.4-mini", purpose="summary", topic_id=None, usage=Usage(1, 0, 1)
    )

    ((key, body),) = bodies
    assert key == "spend-80:2026-11"
    assert "$16.00 of the $20 budget" in body
    assert "new topics" not in body.lower()


class SlowSender:
    def __init__(self) -> None:
        self.sent = []
        self._lock = __import__("threading").Lock()

    def send(self, to, content):
        __import__("time").sleep(0.3)
        with self._lock:
            self.sent.append(content.subject)


def test_concurrent_records_crossing_80_send_one_alert(db_session, monkeypatch):
    import threading

    monkeypatch.setattr(get_settings(), "alert_email", "owner@example.com")
    sender = SlowSender()
    monkeypatch.setattr("app.alerts._default_sender", lambda: sender)
    _usage_row(db_session, NOV, "15.99")
    ledger = _ledger()
    barrier = threading.Barrier(4)
    errors = []

    def record():
        try:
            barrier.wait()
            ledger.record(model="gpt-5.4-mini", purpose="summary", topic_id=None, usage=Usage(0, 0, 2_300))
        except Exception as exc:  # pragma: no cover - surfaced by the assert below
            errors.append(exc)

    threads = [threading.Thread(target=record) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert errors == []
    assert len(sender.sent) == 1
    assert db_session.query(models.LlmUsage).count() == 5


def test_month_boundary_holds_in_a_non_utc_database_session(db_session):
    from sqlalchemy import text

    # Session-level, so it would outlive this test on the pooled connection:
    # always reset it.
    db_session.execute(text("SET TIME ZONE 'America/Los_Angeles'"))
    try:
        _usage_row(db_session, datetime(2026, 10, 31, 23, 59, tzinfo=timezone.utc), "5")
        _usage_row(db_session, datetime(2026, 11, 1, 0, 0, 30, tzinfo=timezone.utc), "2")
        assert month_to_date_usd(db_session, NOV) == Decimal("2")
    finally:
        db_session.rollback()
        db_session.execute(text("RESET TIME ZONE"))
        db_session.commit()


def test_month_to_date_counts_rows_stamped_slightly_ahead(db_session):
    # The database's clock can run ahead of the app's (colima's VM is ~0.1s
    # ahead here). A row stamped by Postgres must still count, or the guard
    # under-counts spend.
    _usage_row(db_session, NOV + timedelta(seconds=2), "7")
    assert month_to_date_usd(db_session, NOV) == Decimal("7")
