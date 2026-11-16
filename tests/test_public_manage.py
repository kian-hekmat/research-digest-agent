"""The manage page, one-click unsubscribe, re-subscribe and delete my data
(phase 3a)."""
import re
from datetime import datetime, timezone

import pytest

from app import crud, models, signup, tokens
from app.config import get_settings
from tests.conftest import make_subscriber, subscribe

NOW = datetime.now(timezone.utc)


def _csrf(c):
    return re.search(r'name="csrf" value="([^"]+)"', c.get("/signup").text).group(1)


def _active_member(db, email="me@example.com", queries=("rlhf",)):
    """A confirmed subscriber with active subscriptions to `queries`."""
    _, sub = signup.record_signup(db, email, [signup.SignupTopic(q, None, 5) for q in queries], NOW)
    signup.confirm_subscriber(db, sub, NOW, NOW)
    db.refresh(sub)
    return sub


def _post(c, path, token, **data):
    return c.post(path, data={"token": token, "csrf": _csrf(c), **data})


def _subs(db, sub_id):
    db.expire_all()
    s = db.get(models.Subscriber, sub_id)
    return {x.topic.normalized_query: x for x in s.subscriptions} if s else None


def test_manage_page_lists_only_this_subscribers_topics(anon_client, db_session):
    me = _active_member(db_session, queries=("rlhf", "reward modeling"))
    _active_member(db_session, "other@example.com", queries=("pinns",))
    page = anon_client.get("/manage", params={"token": tokens.manage_token(me)})
    assert page.status_code == 200
    assert "rlhf" in page.text and "reward modeling" in page.text
    assert "pinns" not in page.text and "other@example.com" not in page.text


def test_update_label_and_papers(anon_client, db_session):
    me = _active_member(db_session)
    topic_id = _subs(db_session, me.id)["rlhf"].topic_id
    resp = _post(anon_client, "/manage/update", tokens.manage_token(me), topic_id=topic_id, label="My <RLHF>", papers="8")
    assert resp.status_code == 200 and "My &lt;RLHF&gt;" in resp.text
    s = _subs(db_session, me.id)["rlhf"]
    assert (s.label, s.max_papers) == ("My <RLHF>", 8)


def test_update_rejects_papers_out_of_range(anon_client, db_session):
    me = _active_member(db_session)
    topic_id = _subs(db_session, me.id)["rlhf"].topic_id
    resp = _post(anon_client, "/manage/update", tokens.manage_token(me), topic_id=topic_id, label="", papers="12")
    assert resp.status_code == 422
    assert _subs(db_session, me.id)["rlhf"].max_papers == 5


def test_remove_topic_leaves_topic_row(anon_client, db_session):
    me = _active_member(db_session, queries=("rlhf", "pinns"))
    topic_id = _subs(db_session, me.id)["rlhf"].topic_id
    _post(anon_client, "/manage/remove", tokens.manage_token(me), topic_id=topic_id)
    assert set(_subs(db_session, me.id)) == {"pinns"}
    assert db_session.get(models.Topic, topic_id) is not None


def test_add_topic_validates_and_caps_at_five(anon_client, db_session):
    me = _active_member(db_session, queries=("a one", "b two", "c three", "d four"))
    token = tokens.manage_token(me)
    assert _post(anon_client, "/manage/add", token, topic="cat:cs.LG", label="", papers="5").status_code == 422
    assert _post(anon_client, "/manage/add", token, topic="e five", label="", papers="5").status_code == 200
    resp = _post(anon_client, "/manage/add", token, topic="f six", label="", papers="5")
    assert resp.status_code == 422 and "5 topics" in resp.text
    assert set(_subs(db_session, me.id)) == {"a one", "b two", "c three", "d four", "e five"}


def test_add_existing_topic_twice_rejected(anon_client, db_session):
    me = _active_member(db_session)
    resp = _post(anon_client, "/manage/add", tokens.manage_token(me), topic="RLHF", label="", papers="5")
    assert resp.status_code == 422


def test_add_new_topic_over_cap_waitlisted(anon_client, db_session, monkeypatch):
    me = _active_member(db_session)
    monkeypatch.setattr(get_settings(), "max_active_topics", 1)
    resp = _post(anon_client, "/manage/add", tokens.manage_token(me), topic="reward modeling", label="", papers="5")
    assert resp.status_code == 200 and "waitlist" in resp.text.lower()
    assert _subs(db_session, me.id)["reward modeling"].waitlisted_at is not None


def test_pause_then_resume(anon_client, db_session):
    me = _active_member(db_session)
    token = tokens.manage_token(me)
    _post(anon_client, "/manage/pause", token)
    assert not _subs(db_session, me.id)["rlhf"].active
    assert crud.active_subscriptions_query(db_session).count() == 0
    _post(anon_client, "/manage/resume", token)
    assert _subs(db_session, me.id)["rlhf"].active


def test_resume_rechecks_caps(anon_client, db_session, monkeypatch):
    me = _active_member(db_session)
    token = tokens.manage_token(me)
    _post(anon_client, "/manage/pause", token)
    _active_member(db_session, "other@example.com", queries=("pinns",))
    monkeypatch.setattr(get_settings(), "max_active_topics", 1)
    _post(anon_client, "/manage/resume", token)
    assert _subs(db_session, me.id)["rlhf"].waitlisted_at is not None


def test_one_click_unsubscribe_topic_without_cookies_or_csrf(anon_client, db_session):
    me = _active_member(db_session, queries=("rlhf", "pinns"))
    topic_id = _subs(db_session, me.id)["rlhf"].topic_id
    url = "/unsubscribe?token=" + tokens.unsubscribe_token(me.id, topic_id)
    resp = anon_client.post(url, content="List-Unsubscribe=One-Click",
                            headers={"Content-Type": "application/x-www-form-urlencoded"})
    assert resp.status_code == 200
    assert set(_subs(db_session, me.id)) == {"pinns"}
    assert anon_client.post(url, content="List-Unsubscribe=One-Click").status_code == 200  # idempotent


def test_one_click_unsubscribe_all(anon_client, db_session):
    me = _active_member(db_session)
    resp = anon_client.post("/unsubscribe?token=" + tokens.unsubscribe_token(me.id, None))
    assert resp.status_code == 200
    db_session.expire_all()
    assert db_session.get(models.Subscriber, me.id).unsubscribed_at is not None
    assert crud.active_subscriptions_query(db_session).count() == 0


def test_unsubscribe_get_shows_button_and_changes_nothing(anon_client, db_session):
    me = _active_member(db_session)
    page = anon_client.get("/unsubscribe", params={"token": tokens.unsubscribe_token(me.id, None)})
    assert page.status_code == 200 and 'method="post"' in page.text
    db_session.expire_all()
    assert db_session.get(models.Subscriber, me.id).unsubscribed_at is None


def test_resume_does_not_restore_unsubscribed_topic(anon_client, db_session):
    me = _active_member(db_session, queries=("rlhf", "pinns"))
    topic_id = _subs(db_session, me.id)["rlhf"].topic_id
    anon_client.post("/unsubscribe?token=" + tokens.unsubscribe_token(me.id, topic_id))
    token = tokens.manage_token(me)
    _post(anon_client, "/manage/pause", token)
    _post(anon_client, "/manage/resume", token)
    assert set(_subs(db_session, me.id)) == {"pinns"}


def _resubscribe_token(page):
    """The short-lived re-subscribe link the unsubscribed page carries."""
    return re.search(r'action="/resubscribe".*?name="token" value="([^"]+)"', page.text, re.S).group(1)


def test_resubscribe_topic_recreates_with_defaults_and_rechecks_caps(anon_client, db_session, monkeypatch):
    me = _active_member(db_session)
    topic_id = _subs(db_session, me.id)["rlhf"].topic_id
    page = anon_client.post("/unsubscribe?token=" + tokens.unsubscribe_token(me.id, topic_id))
    _active_member(db_session, "other@example.com", queries=("pinns",))
    monkeypatch.setattr(get_settings(), "max_active_topics", 1)
    resp = _post(anon_client, "/resubscribe", _resubscribe_token(page))
    assert resp.status_code == 200
    s = _subs(db_session, me.id)["rlhf"]
    assert (s.max_papers, s.label) == (5, None) and s.waitlisted_at is not None


def test_resubscribe_all_clears_unsubscribed(anon_client, db_session):
    me = _active_member(db_session)
    page = anon_client.post("/unsubscribe?token=" + tokens.unsubscribe_token(me.id, None))
    _post(anon_client, "/resubscribe", _resubscribe_token(page))
    db_session.expire_all()
    assert db_session.get(models.Subscriber, me.id).unsubscribed_at is None


# ---------- final-review fixes ----------
def test_old_unsubscribe_link_cannot_resubscribe(anon_client, db_session):
    """A digest's never-expiring unsubscribe link (forwarded, archived)
    must not undo the person's opt-out."""
    me = _active_member(db_session)
    utoken = tokens.unsubscribe_token(me.id, None)
    anon_client.post("/unsubscribe?token=" + utoken)
    resp = _post(anon_client, "/resubscribe", utoken)
    assert resp.status_code == 400
    db_session.expire_all()
    assert db_session.get(models.Subscriber, me.id).unsubscribed_at is not None


def test_resubscribe_link_expires_after_an_hour(anon_client, db_session, monkeypatch):
    import itsdangerous.timed as timed

    me = _active_member(db_session)
    page = anon_client.post("/unsubscribe?token=" + tokens.unsubscribe_token(me.id, None))
    rtoken = _resubscribe_token(page)
    real = timed.time.time()
    monkeypatch.setattr(timed.time, "time", lambda: real + 3601)
    assert _post(anon_client, "/resubscribe", rtoken).status_code == 400


def test_unsubscribe_all_from_manage_page(anon_client, db_session):
    me = _active_member(db_session)
    _post(anon_client, "/manage/unsubscribe-all", tokens.manage_token(me))
    db_session.expire_all()
    assert db_session.get(models.Subscriber, me.id).unsubscribed_at is not None


def test_delete_removes_subscriber_rows_keeps_topics(anon_client, db_session):
    me = _active_member(db_session)
    topic_id = _subs(db_session, me.id)["rlhf"].topic_id
    resp = _post(anon_client, "/manage/delete", tokens.manage_token(me))
    assert resp.status_code == 200
    db_session.expire_all()
    assert db_session.get(models.Subscriber, me.id) is None
    assert db_session.query(models.Subscription).count() == 0
    assert db_session.get(models.Topic, topic_id) is not None


def test_old_unsubscribe_link_after_delete_is_200_noop(anon_client, db_session):
    me = _active_member(db_session)
    utoken = tokens.unsubscribe_token(me.id, None)
    _post(anon_client, "/manage/delete", tokens.manage_token(me))
    assert anon_client.post("/unsubscribe?token=" + utoken).status_code == 200


def _bad_tokens(db):
    me = _active_member(db, "gone@example.com")
    gone_manage, gone_unsub = tokens.manage_token(me), tokens.unsubscribe_token(me.id, None)
    db.delete(me)
    db.commit()
    import itsdangerous.timed  # noqa: F401
    return {
        "junk": ("junk", "junk"),
        "wrong_purpose": (tokens.make_token("csrf", {"n": "x"}), tokens.make_token("csrf", {"n": "x"})),
        "deleted": (gone_manage, gone_unsub),
    }


MANAGE_POSTS = ["/manage/update", "/manage/remove", "/manage/add", "/manage/pause", "/manage/resume",
                "/manage/unsubscribe-all", "/manage/delete"]


@pytest.mark.parametrize("kind", ["junk", "wrong_purpose", "deleted"])
def test_bad_links_render_expired_400_on_every_route(anon_client, db_session, kind):
    manage_bad, unsub_bad = _bad_tokens(db_session)[kind]
    responses = [anon_client.get("/manage", params={"token": manage_bad})]
    responses += [_post(anon_client, p, manage_bad, topic_id="x", topic="y", label="", papers="5") for p in MANAGE_POSTS]
    responses += [anon_client.get("/unsubscribe", params={"token": unsub_bad}),
                  _post(anon_client, "/resubscribe", unsub_bad)]
    if kind != "deleted":  # a deleted subscriber's one-click link is a 200 no-op (above)
        responses.append(anon_client.post("/unsubscribe?token=" + unsub_bad))
    assert [r.status_code for r in responses] == [400] * len(responses)
    assert all("expired" in r.text.lower() for r in responses)


def test_expired_confirm_link_renders_expired(anon_client, db_session, monkeypatch):
    import itsdangerous.timed as timed

    me = make_subscriber(db_session, "pending@example.com", confirmed=False)
    token = tokens.confirm_token(me)
    real = timed.time.time()
    monkeypatch.setattr(timed.time, "time", lambda: real + 49 * 3600)
    assert anon_client.get("/confirm", params={"token": token}).status_code == 400


def test_resignup_after_unsubscribe_needs_confirmation(anon_client, db_session):
    me = _active_member(db_session)
    anon_client.post("/unsubscribe?token=" + tokens.unsubscribe_token(me.id, None))
    db_session.expire_all()
    kind, sub = signup.record_signup(db_session, "me@example.com", [signup.SignupTopic("pinns", None, 5)], NOW)
    assert kind == "confirm"
    assert crud.active_subscriptions_query(db_session).count() == 0
    signup.confirm_subscriber(db_session, sub, datetime.now(timezone.utc), datetime.now(timezone.utc))
    assert crud.active_subscriptions_query(db_session).count() == 1
