"""Signed links (app.tokens): confirm, manage, unsubscribe and csrf."""
import pytest

from app import tokens
from app.tokens import TokenError
from tests.conftest import make_subscriber


def test_round_trip_each_purpose():
    for purpose, payload in [("confirm", {"s": "a", "v": 0}), ("manage", {"s": "a", "v": 2}),
                             ("unsubscribe", {"s": "a", "t": None}), ("csrf", {"n": "x"})]:
        data, signed_at = tokens.read_token(purpose, tokens.make_token(purpose, payload), max_age=None)
        assert data == payload and signed_at.tzinfo is not None


def test_tampered_token_rejected():
    t = tokens.make_token("manage", {"s": "a", "v": 0})
    bad = t[:-2] + ("A" if t[-2] != "A" else "B") + t[-1]
    with pytest.raises(TokenError):
        tokens.read_token("manage", bad, max_age=None)


def test_wrong_purpose_rejected():
    with pytest.raises(TokenError):
        tokens.read_token("confirm", tokens.make_token("manage", {"s": "a", "v": 0}), max_age=None)


def test_confirm_expires_after_48h(monkeypatch):
    import itsdangerous.timed as timed

    t = tokens.make_token("confirm", {"s": "a", "v": 0})
    real = timed.time.time()
    monkeypatch.setattr(timed.time, "time", lambda: real + 47 * 3600)
    tokens.read_token("confirm", t, max_age=48 * 3600)
    monkeypatch.setattr(timed.time, "time", lambda: real + 48 * 3600 + 1)
    with pytest.raises(TokenError):
        tokens.read_token("confirm", t, max_age=48 * 3600)


def test_stale_token_version_rejected(db_session):
    sub = make_subscriber(db_session, confirmed=False)
    t = tokens.confirm_token(sub)
    assert tokens.subscriber_for(db_session, "confirm", t)[0].id == sub.id
    sub.token_version += 1
    db_session.commit()
    with pytest.raises(TokenError):
        tokens.subscriber_for(db_session, "confirm", t)


def test_deleted_subscriber_rejected(db_session):
    sub = make_subscriber(db_session)
    t = tokens.manage_token(sub)
    db_session.delete(sub)
    db_session.commit()
    with pytest.raises(TokenError):
        tokens.subscriber_for(db_session, "manage", t)


def test_csrf_ok_only_for_fresh_signed_tokens():
    assert tokens.csrf_ok(tokens.csrf_token())
    assert not tokens.csrf_ok(None)
    assert not tokens.csrf_ok("junk")
    assert not tokens.csrf_ok(tokens.make_token("manage", {"s": "a", "v": 0}))


def test_link_builds_public_url():
    assert tokens.link("/manage", "abc") == "http://testserver/manage?token=abc"
