"""Postgres-backed rate limits (app.ratelimit)."""
from datetime import datetime, timedelta, timezone

from app import models
from app.ratelimit import allow, email_key, signup_ip_key

T = datetime(2026, 11, 16, 12, tzinfo=timezone.utc)
HOUR = timedelta(hours=1)


def test_allows_up_to_limit_then_refuses(db_session):
    assert [allow(db_session, "k", 5, HOUR, T) for _ in range(6)] == [True] * 5 + [False]


def test_window_slides(db_session):
    for _ in range(5):
        allow(db_session, "k", 5, HOUR, T)
    assert not allow(db_session, "k", 5, HOUR, T + timedelta(minutes=30))
    assert allow(db_session, "k", 5, HOUR, T + HOUR + timedelta(seconds=1))


def test_keys_are_independent(db_session):
    for _ in range(5):
        allow(db_session, "a", 5, HOUR, T)
    assert allow(db_session, "b", 5, HOUR, T)


def test_old_rows_are_pruned(db_session):
    for _ in range(3):
        allow(db_session, "k", 5, HOUR, T)
    allow(db_session, "k", 5, HOUR, T + 2 * HOUR)
    assert db_session.query(models.RateLimitEvent).filter_by(key="k").count() == 1


def test_email_key_normalizes():
    assert email_key(" Me@Example.COM ") == "email:me@example.com"
    assert signup_ip_key("203.0.113.9") == "signup:ip:203.0.113.9"
