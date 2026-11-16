"""Public signup (phase 3a): the form, validation, Turnstile, rate limits and
enumeration-safe responses."""
import re

import httpx
import pytest
import respx

from app import models
from app.config import get_settings
from tests.conftest import make_subscriber, subscribe

CSRF_FIELD = re.compile(r'name="csrf" value="[^"]*"')


@pytest.fixture
def turnstile(monkeypatch):
    """Turnstile passes unless a test sets `verdict["ok"] = False`."""
    verdict = {"ok": True}

    async def fake(token, ip):
        return verdict["ok"]

    monkeypatch.setattr("app.web.verify_turnstile", fake)
    return verdict


@pytest.fixture
def sent(monkeypatch):
    """Every account email the background task asks for, before bucketing."""
    calls = []

    async def fake(client, kind, subscriber_id, to, url, now):
        calls.append({"kind": kind, "subscriber_id": subscriber_id, "to": to, "url": url})

    monkeypatch.setattr("app.account_emails.start_account_email", fake)
    return calls


def _csrf(c):
    page = c.get("/signup").text
    return re.search(r'name="csrf" value="([^"]+)"', page).group(1)


def form(c, **overrides):
    data = {"email": "new@example.com", "topic_0": "physics-informed neural networks",
            "label_0": "PINNs", "papers_0": "5", "us_resident": "on",
            "cf-turnstile-response": "ok", "csrf": _csrf(c)}
    data.update(overrides)
    return {k: v for k, v in data.items() if v is not None}


def _body(resp):
    return CSRF_FIELD.sub("", resp.text)


def _subscriber(db, email):
    db.expire_all()
    return db.query(models.Subscriber).filter(models.Subscriber.email.ilike(email)).one_or_none()


def test_signup_page_renders_form_with_csrf_and_site_key(anon_client):
    page = anon_client.get("/signup")
    assert page.status_code == 200
    assert 'name="csrf"' in page.text and "topic_4" in page.text
    assert get_settings().turnstile_site_key in page.text


def test_new_signup_creates_pending_subscriber_and_starts_confirm_email(anon_client, db_session, turnstile, sent):
    resp = anon_client.post("/signup", data=form(anon_client))
    assert resp.status_code == 200 and "check your inbox" in resp.text.lower()
    sub = _subscriber(db_session, "new@example.com")
    assert sub.confirmed_at is None and sub.us_resident_attested_at is not None
    [s] = sub.subscriptions
    assert (s.max_papers, s.label, s.topic.normalized_query) == (5, "PINNs", "physics-informed neural networks")
    assert [c["kind"] for c in sent] == ["confirm"] and "/confirm?token=" in sent[0]["url"]


def test_response_identical_for_new_pending_and_confirmed_addresses(anon_client, db_session, turnstile, sent):
    make_subscriber(db_session, "confirmed@example.com", confirmed=True)
    make_subscriber(db_session, "pending@example.com", confirmed=False)
    bodies = []
    for email in ("fresh@example.com", "pending@example.com", "confirmed@example.com"):
        resp = anon_client.post("/signup", data=form(anon_client, email=email))
        bodies.append((resp.status_code, _body(resp)))
    assert bodies[0] == bodies[1] == bodies[2]


def test_confirmed_address_gets_manage_email_and_no_changes(anon_client, db_session, turnstile, sent):
    sub = make_subscriber(db_session, "confirmed@example.com", confirmed=True)
    topic = models.Topic(name="RLHF", query="rlhf", normalized_query="rlhf")
    db_session.add(topic)
    db_session.commit()
    subscribe(db_session, topic, sub)
    anon_client.post("/signup", data=form(anon_client, email="confirmed@example.com"))
    sub = _subscriber(db_session, "confirmed@example.com")
    assert [s.topic.normalized_query for s in sub.subscriptions] == ["rlhf"]
    assert [c["kind"] for c in sent] == ["manage"] and "/manage?token=" in sent[0]["url"]


def test_pending_signup_replaces_its_topics(anon_client, db_session, turnstile, sent):
    anon_client.post("/signup", data=form(anon_client))
    anon_client.post("/signup", data=form(anon_client, topic_0="reward modeling", label_0=None))
    sub = _subscriber(db_session, "new@example.com")
    assert [s.topic.normalized_query for s in sub.subscriptions] == ["reward modeling"]


def test_email_case_and_space_variants_hit_one_subscriber(anon_client, db_session, turnstile, sent):
    anon_client.post("/signup", data=form(anon_client, email="me@example.com"))
    anon_client.post("/signup", data=form(anon_client, email=" Me@Example.COM "))
    db_session.expire_all()
    assert db_session.query(models.Subscriber).count() == 1


def test_topics_are_shared_by_normalized_query(anon_client, db_session, turnstile, sent):
    anon_client.post("/signup", data=form(anon_client, email="a@example.com", topic_0="PINNs"))
    anon_client.post("/signup", data=form(anon_client, email="b@example.com", topic_0=' "pinns" '))
    db_session.expire_all()
    assert db_session.query(models.Topic).count() == 1


def test_missing_residency_box_rejected_inline_422(anon_client, turnstile, sent):
    resp = anon_client.post("/signup", data=form(anon_client, us_resident=None))
    assert resp.status_code == 422 and "resident" in resp.text.lower()
    assert sent == []


def test_duplicate_topics_after_normalizing_rejected(anon_client, turnstile, sent):
    resp = anon_client.post("/signup", data=form(anon_client, topic_0="PINNs", topic_1=" pinns "))
    assert resp.status_code == 422 and "already listed" in resp.text.lower()


def test_typographic_apostrophe_accepted(anon_client, db_session, turnstile, sent):
    resp = anon_client.post("/signup", data=form(anon_client, topic_0="Pólya’s urn"))
    assert resp.status_code == 200
    assert _subscriber(db_session, "new@example.com").subscriptions[0].topic.normalized_query == "pólya's urn"


def test_empty_topic_message_is_friendly(anon_client, turnstile, sent):
    resp = anon_client.post("/signup", data=form(anon_client, topic_0='""'))
    assert resp.status_code == 422 and "empty query" not in resp.text
    resp = anon_client.post("/signup", data=form(anon_client, topic_0=""))
    assert resp.status_code == 422 and "at least one topic" in resp.text.lower()


@pytest.mark.parametrize("papers", ["4", "11", "ten"])
def test_papers_outside_5_10_rejected(anon_client, turnstile, sent, papers):
    assert anon_client.post("/signup", data=form(anon_client, papers_0=papers)).status_code == 422


def test_label_html_escaped_and_control_chars_stripped(anon_client, db_session, turnstile, sent):
    resp = anon_client.post("/signup", data=form(anon_client, label_0="<b>x</b>\x07y", us_resident=None))
    assert "<b>x</b>" not in resp.text and "&lt;b&gt;x&lt;/b&gt;" in resp.text
    anon_client.post("/signup", data=form(anon_client, label_0="<b>x</b>\x07y"))
    assert _subscriber(db_session, "new@example.com").subscriptions[0].label == "<b>x</b>y"


def test_crlf_in_email_rejected(anon_client, turnstile, sent):
    resp = anon_client.post("/signup", data=form(anon_client, email="a@example.com\r\nBcc: x@evil.test"))
    assert resp.status_code == 422 and sent == []


def test_turnstile_failure_rejected(anon_client, turnstile, sent):
    turnstile["ok"] = False
    resp = anon_client.post("/signup", data=form(anon_client))
    assert resp.status_code == 422 and sent == []


def test_sixth_signup_from_one_ip_in_an_hour_gets_429(anon_client, turnstile, sent):
    codes = [anon_client.post("/signup", data=form(anon_client, email=f"u{i}@example.com")).status_code
             for i in range(6)]
    assert codes == [200] * 5 + [429]


def test_fourth_account_email_in_a_day_not_sent_page_unchanged(anon_client, monkeypatch, turnstile, sent):
    monkeypatch.setattr(get_settings(), "signup_per_ip_per_hour", 100)
    pages = [_body(anon_client.post("/signup", data=form(anon_client))) for _ in range(4)]
    assert len(sent) == 3 and len(set(pages)) == 1


def test_manage_link_form_sends_only_to_confirmed(anon_client, db_session, turnstile, sent):
    make_subscriber(db_session, "confirmed@example.com", confirmed=True)
    make_subscriber(db_session, "pending@example.com", confirmed=False)
    bodies = set()
    for email in ("confirmed@example.com", "pending@example.com", "nobody@example.com"):
        resp = anon_client.post("/manage-link", data={"email": email, "csrf": _csrf(anon_client)})
        assert resp.status_code == 200
        bodies.add(_body(resp))
    assert len(bodies) == 1
    assert [(c["kind"], c["to"]) for c in sent] == [("manage", "confirmed@example.com")]


def test_missing_or_bad_csrf_403(anon_client, turnstile, sent):
    assert anon_client.post("/signup", data=form(anon_client, csrf=None)).status_code == 403
    assert anon_client.post("/signup", data=form(anon_client, csrf="junk")).status_code == 403


def test_cross_site_origin_403(anon_client, turnstile, sent):
    resp = anon_client.post("/signup", data=form(anon_client), headers={"Origin": "https://evil.test"})
    assert resp.status_code == 403
    ok = anon_client.post("/signup", data=form(anon_client), headers={"Origin": "http://testserver"})
    assert ok.status_code == 200


def test_503_when_secret_key_empty(anon_client, monkeypatch):
    monkeypatch.setattr(get_settings(), "secret_key", "")
    assert anon_client.get("/signup").status_code == 503


def test_no_public_page_shows_other_subscribers_email_or_labels(anon_client, db_session, turnstile, sent):
    a = make_subscriber(db_session, "alice@example.com")
    topic = models.Topic(name="RLHF", query="rlhf", normalized_query="rlhf")
    db_session.add(topic)
    db_session.commit()
    sub = subscribe(db_session, topic, a)
    sub.label = "alice's secret label"
    db_session.commit()
    pages = [anon_client.get("/signup").text, anon_client.post("/signup", data=form(anon_client)).text]
    assert all("alice" not in p for p in pages)


@respx.mock
async def test_verify_turnstile_posts_secret_and_reads_success(monkeypatch):
    import inspect

    from app import web

    assert inspect.iscoroutinefunction(web.verify_turnstile)  # never blocks the event loop
    route = respx.post("https://challenges.cloudflare.com/turnstile/v0/siteverify").mock(
        return_value=httpx.Response(200, json={"success": True})
    )
    assert await web.verify_turnstile("tok", "203.0.113.9") is True
    assert b"secret=" in route.calls.last.request.content and b"remoteip=203.0.113.9" in route.calls.last.request.content
    route.mock(return_value=httpx.Response(200, json={"success": False}))
    assert await web.verify_turnstile("tok", "203.0.113.9") is False
    assert await web.verify_turnstile(None, "203.0.113.9") is False
    monkeypatch.setattr(get_settings(), "turnstile_secret_key", "")
    assert await web.verify_turnstile("tok", "203.0.113.9") is False


# ---------- final-review fixes ----------
def test_failed_turnstile_counts_toward_the_ip_limit(anon_client, turnstile, sent):
    turnstile["ok"] = False
    codes = [anon_client.post("/signup", data=form(anon_client, email=f"u{i}@example.com")).status_code
             for i in range(6)]
    assert codes == [422] * 5 + [429]


def test_sixth_manage_link_request_from_one_ip_gets_429(anon_client, sent):
    codes = [anon_client.post("/manage-link", data={"email": f"u{i}@example.com", "csrf": _csrf(anon_client)}).status_code
             for i in range(6)]
    assert codes == [200] * 5 + [429]
