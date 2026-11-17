"""Confirmation, the cap check (a hand-admitted waitlist) and admin admit
(phase 3a)."""
import re
from datetime import datetime, timedelta, timezone

import pytest

from app import crud, models, signup, tokens
from app.config import get_settings
from tests.conftest import make_subscriber, subscribe

NOW = datetime.now(timezone.utc)


def _pending(db, email="new@example.com", queries=("reward modeling",)):
    _, sub = signup.record_signup(db, email, [signup.SignupTopic(q, 5) for q in queries], NOW)
    return sub


def _topic(db, query):
    return signup.topic_for(db, query)


def _active_elsewhere(db, query, email):
    """`query`'s topic with one active subscriber, `email`."""
    t = _topic(db, query)
    db.commit()
    subscribe(db, t, make_subscriber(db, email))
    return t


def _csrf(c):
    return re.search(r'name="csrf" value="([^"]+)"', c.get("/signup").text).group(1)


def _confirm(c, token):
    return c.post("/confirm", data={"token": token, "csrf": _csrf(c)})


def test_get_confirm_changes_nothing(anon_client, db_session):
    sub = _pending(db_session)
    page = anon_client.get("/confirm", params={"token": tokens.confirm_token(sub)})
    assert page.status_code == 200 and 'action="/confirm"' in page.text
    db_session.expire_all()
    assert db_session.get(models.Subscriber, sub.id).confirmed_at is None


def test_post_confirm_activates_and_sets_last_sent(anon_client, db_session):
    sub = _pending(db_session)
    before = datetime.now(timezone.utc)
    resp = _confirm(anon_client, tokens.confirm_token(sub))
    assert resp.status_code == 200 and "monday" in resp.text.lower()
    db_session.expire_all()
    sub = db_session.get(models.Subscriber, sub.id)
    assert sub.confirmed_at is not None
    assert all(s.last_sent_at >= before for s in sub.subscriptions)
    assert [s.id for s in crud.active_subscriptions_query(db_session)] == [sub.subscriptions[0].id]


def test_repeat_confirm_keeps_last_sent_at(anon_client, db_session):
    sub = _pending(db_session)
    token = tokens.confirm_token(sub)
    _confirm(anon_client, token)
    db_session.expire_all()
    first = db_session.get(models.Subscriber, sub.id).subscriptions[0].last_sent_at
    _confirm(anon_client, token)
    db_session.expire_all()
    assert db_session.get(models.Subscriber, sub.id).subscriptions[0].last_sent_at == first


def test_confirm_link_from_before_unsubscribe_is_expired(anon_client, db_session):
    sub = _pending(db_session)
    token = tokens.confirm_token(sub)
    _confirm(anon_client, token)
    db_session.expire_all()
    sub = db_session.get(models.Subscriber, sub.id)
    sub.unsubscribed_at = datetime.now(timezone.utc) + timedelta(seconds=1)
    db_session.commit()
    resp = _confirm(anon_client, token)
    assert resp.status_code == 400 and "expired" in resp.text.lower()
    db_session.expire_all()
    assert db_session.get(models.Subscriber, sub.id).unsubscribed_at is not None


def test_bad_confirm_token_expired_400(anon_client):
    assert anon_client.get("/confirm", params={"token": "junk"}).status_code == 400
    assert _confirm(anon_client, "junk").status_code == 400


def test_at_subscriber_cap_confirm_waitlists_and_page_says_so(anon_client, db_session, monkeypatch):
    monkeypatch.setattr(get_settings(), "max_subscribers", 1)
    make_subscriber(db_session, "first@example.com")
    sub = _pending(db_session)
    resp = _confirm(anon_client, tokens.confirm_token(sub))
    assert resp.status_code == 200 and "waitlist" in resp.text.lower()
    db_session.expire_all()
    assert db_session.get(models.Subscriber, sub.id).waitlisted_at is not None
    assert crud.active_subscriptions_query(db_session).filter(models.Subscription.subscriber_id == sub.id).count() == 0


def test_new_topic_over_topic_cap_waitlists_only_that_subscription(anon_client, db_session, monkeypatch):
    monkeypatch.setattr(get_settings(), "max_active_topics", 1)
    _active_elsewhere(db_session, "rlhf", "other@example.com")
    sub = _pending(db_session, queries=("rlhf", "reward modeling"))
    resp = _confirm(anon_client, tokens.confirm_token(sub))
    assert "reward modeling" in resp.text
    db_session.expire_all()
    held = {s.topic.normalized_query: s.waitlisted_at is not None for s in db_session.get(models.Subscriber, sub.id).subscriptions}
    assert held == {"rlhf": False, "reward modeling": True}


def test_new_topic_at_soft_spend_waitlisted_existing_topic_not(anon_client, db_session, monkeypatch):
    monkeypatch.setattr("app.waitlist.spend_state", lambda db, now: "soft")
    _active_elsewhere(db_session, "rlhf", "other@example.com")
    sub = _pending(db_session, queries=("rlhf", "reward modeling"))
    _confirm(anon_client, tokens.confirm_token(sub))
    db_session.expire_all()
    held = {s.topic.normalized_query: s.waitlisted_at is not None for s in db_session.get(models.Subscriber, sub.id).subscriptions}
    assert held == {"rlhf": False, "reward modeling": True}


def test_admin_waitlist_and_admit_make_active(client, anon_client, db_session, monkeypatch):
    monkeypatch.setattr(get_settings(), "max_subscribers", 1)
    make_subscriber(db_session, "first@example.com")
    sub = _pending(db_session)
    _confirm(anon_client, tokens.confirm_token(sub))
    listing = client.get("/admin/waitlist").json()
    assert [s["id"] for s in listing["subscribers"]] == [sub.id]
    assert client.post(f"/admin/subscribers/{sub.id}/admit").status_code == 204
    db_session.expire_all()
    assert crud.active_subscriptions_query(db_session).filter(models.Subscription.subscriber_id == sub.id).count() == 1


def test_admin_admit_subscription(client, anon_client, db_session, monkeypatch):
    monkeypatch.setattr(get_settings(), "max_active_topics", 0)
    sub = _pending(db_session)
    _confirm(anon_client, tokens.confirm_token(sub))
    [held] = client.get("/admin/waitlist").json()["subscriptions"]
    assert client.post(f"/admin/subscriptions/{held['id']}/admit").status_code == 204
    db_session.expire_all()
    assert crud.active_subscriptions_query(db_session).count() == 1


def test_admit_unknown_ids_404(client):
    assert client.post("/admin/subscribers/not-a-uuid/admit").status_code == 404
    assert client.post("/admin/subscriptions/00000000-0000-0000-0000-000000000000/admit").status_code == 404


def test_admin_routes_need_admin_token(anon_client):
    assert anon_client.get("/admin/waitlist").status_code == 401
