"""Digest emails gain one-click unsubscribe headers and footer links, the
send re-checks eligibility, and the phase 3a exit test: signup -> confirm ->
digest -> one-click unsubscribe -> no more digests."""
import re
from datetime import datetime, timedelta, timezone
from urllib.parse import urlsplit

from app import crud, models, signup, tokens
from app.config import Settings
from app.services.email import EmailLinks, EmailSender, PaperForEmail, TopicSection, render_digest_email
from app.temporal import activities as temporal_activities
from app.temporal.types import DigestEmailContent, GatherContentInput, TopicWindow
from tests.test_email import FakeSMTPClient
from tests.test_send_pass import SLOT, _edition, _papers, _send
from tests.test_temporal_workflows import FakeEmailSender, _reset_fakes, temporal_client  # noqa: F401

NOW = datetime.now(timezone.utc)


def _section(topic_id="t1", name="RL <HF>"):
    paper = PaperForEmail(title="A paper", summary="It did a thing.", arxiv_id="2611.00001", published_at=NOW)
    return TopicSection(topic_name=name, papers=[paper], overviews=[], topic_id=topic_id)


LINKS = EmailLinks(
    manage_url="http://testserver/manage?token=m",
    unsubscribe_all_url="http://testserver/unsubscribe?token=all",
    topic_unsubscribe_urls={"t1": "http://testserver/unsubscribe?token=t1"},
)


def test_headers_and_footer_links_present_when_links_given():
    c = render_digest_email([_section()], links=LINKS)
    assert c.headers == {
        "List-Unsubscribe": "<http://testserver/unsubscribe?token=all>",
        "List-Unsubscribe-Post": "List-Unsubscribe=One-Click",
    }
    for body in (c.text_body, c.html_body):
        assert "http://testserver/unsubscribe?token=t1" in body
        assert "http://testserver/manage?token=m" in body
        assert "arXiv" in body and "AI" in body


def test_no_links_no_headers():
    c = render_digest_email([_section()])
    assert c.headers == {} and "unsubscribe" not in c.text_body.lower()


def test_label_and_topic_html_escaped_in_footer():
    c = render_digest_email([_section()], links=LINKS)
    assert "RL <HF>" not in c.html_body and "RL &lt;HF&gt;" in c.html_body


def test_sender_sets_headers_on_the_message():
    fake = FakeSMTPClient()
    settings = Settings(smtp_from_address="digest@example.com", smtp_use_tls=False, smtp_username="", smtp_password="")
    content = DigestEmailContent(subject="S", text_body="t", html_body="<p>h</p>",
                                 headers={"List-Unsubscribe": "<http://x/u>", "List-Unsubscribe-Post": "List-Unsubscribe=One-Click"})
    EmailSender(settings=settings, smtp_client_factory=lambda: fake).send("a@example.com", content)
    raw = fake.sendmail_args[2]
    assert "List-Unsubscribe: <http://x/u>" in raw and "List-Unsubscribe-Post: List-Unsubscribe=One-Click" in raw


def _member(db, email="me@example.com", query="rlhf"):
    _, sub = signup.record_signup(db, email, [signup.SignupTopic(query, None, 5)], NOW)
    signup.confirm_subscriber(db, sub, NOW, NOW)
    db.refresh(sub)
    return sub


def test_unsubscribed_between_listing_and_delivery_is_skipped(db_session):
    me = _member(db_session)
    s = me.subscriptions[0]
    papers = _papers(db_session, s.topic, 3, completed_at=NOW - timedelta(days=1))
    edition = _edition(db_session, s.topic, papers, start=NOW - timedelta(days=7), end=NOW)
    window = TopicWindow(subscription_id=s.id, topic_id=s.topic_id, topic_name="rlhf", edition_id=edition.id)
    assert temporal_activities.gather_digest_content(GatherContentInput(topics=[window])) is not None
    me.unsubscribed_at = datetime.now(timezone.utc)
    db_session.commit()
    assert temporal_activities.gather_digest_content(GatherContentInput(topics=[window])) is None


async def test_signup_to_digest_to_unsubscribe_exit(anon_client, fake_temporal_client, db_session, temporal_client, monkeypatch):  # noqa: F811
    async def turnstile_ok(token, ip):
        return True

    monkeypatch.setattr("app.web.verify_turnstile", turnstile_ok)
    csrf = re.search(r'name="csrf" value="([^"]+)"', anon_client.get("/signup").text).group(1)
    resp = anon_client.post("/signup", data={
        "email": "reader@example.com", "topic_0": "reward modeling", "papers_0": "5",
        "us_resident": "on", "cf-turnstile-response": "ok", "csrf": csrf,
    })
    assert resp.status_code == 200
    [(_, email_input, _, _)] = fake_temporal_client.started
    confirm = urlsplit(email_input.url)
    token = confirm.query.removeprefix("token=")
    page = anon_client.get(confirm.path, params={"token": token})
    csrf = re.search(r'name="csrf" value="([^"]+)"', page.text).group(1)
    assert anon_client.post("/confirm", data={"token": token, "csrf": csrf}).status_code == 200

    db_session.expire_all()
    sub = signup.find_subscriber(db_session, "reader@example.com")
    s = sub.subscriptions[0]
    papers = _papers(db_session, s.topic, 3, completed_at=SLOT - timedelta(days=1))
    _edition(db_session, s.topic, papers, start=SLOT - timedelta(days=7), end=SLOT - timedelta(hours=7))
    s.last_sent_at = SLOT - timedelta(days=7)  # stands in for waiting until the next Monday
    db_session.commit()

    assert await _send(temporal_client) == 1
    [(to, content)] = FakeEmailSender.sent
    assert to == "reader@example.com"
    assert content.headers["List-Unsubscribe-Post"] == "List-Unsubscribe=One-Click"
    unsubscribe = urlsplit(content.headers["List-Unsubscribe"].strip("<>"))

    resp = anon_client.post(f"{unsubscribe.path}?{unsubscribe.query}", content="List-Unsubscribe=One-Click",
                            headers={"Content-Type": "application/x-www-form-urlencoded"})
    assert resp.status_code == 200

    db_session.expire_all()
    s = signup.find_subscriber(db_session, "reader@example.com").subscriptions[0]
    s.last_sent_at = SLOT - timedelta(days=7)  # would be due again if still subscribed
    db_session.commit()
    FakeEmailSender.sent = []
    assert await _send(temporal_client) == 0 and FakeEmailSender.sent == []
