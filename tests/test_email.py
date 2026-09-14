from datetime import datetime, timezone

from app.config import Settings
from app.services.email import DigestForEmail, EmailSender, PaperForEmail, render_digest_email
from app.temporal.types import DigestEmailContent


class FakeSMTPClient:
    """Stands in for smtplib.SMTP - records calls, never touches a socket."""

    def __init__(self):
        self.starttls_called = False
        self.login_args = None
        self.sendmail_args = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def starttls(self):
        self.starttls_called = True

    def login(self, username, password):
        self.login_args = (username, password)

    def sendmail(self, from_addr, to_addrs, msg):
        self.sendmail_args = (from_addr, to_addrs, msg)


# ---------- EmailSender ----------
def test_email_sender_sends_via_the_smtp_client():
    fake = FakeSMTPClient()
    settings = Settings(smtp_from_address="digest@example.com")
    sender = EmailSender(settings=settings, smtp_client_factory=lambda: fake)
    content = DigestEmailContent(subject="Subj", text_body="a plain text body", html_body="<p>html</p>")

    sender.send("someone@example.com", content)

    from_addr, to_addrs, raw = fake.sendmail_args
    assert from_addr == "digest@example.com"
    assert to_addrs == ["someone@example.com"]
    assert "Subj" in raw
    assert "a plain text body" in raw
    assert fake.starttls_called is False
    assert fake.login_args is None


def test_email_sender_starts_tls_and_logs_in_when_configured():
    fake = FakeSMTPClient()
    settings = Settings(smtp_use_tls=True, smtp_username="user", smtp_password="pass")
    sender = EmailSender(settings=settings, smtp_client_factory=lambda: fake)

    sender.send("someone@example.com", DigestEmailContent("s", "t", "h"))

    assert fake.starttls_called is True
    assert fake.login_args == ("user", "pass")


def test_email_sender_skips_login_without_credentials():
    fake = FakeSMTPClient()
    settings = Settings(smtp_username="", smtp_password="")
    sender = EmailSender(settings=settings, smtp_client_factory=lambda: fake)

    sender.send("someone@example.com", DigestEmailContent("s", "t", "h"))

    assert fake.login_args is None


# ---------- render_digest_email ----------
def test_render_digest_email_includes_overview_and_papers():
    digests = [
        DigestForEmail(
            generated_at=datetime(2024, 8, 1, tzinfo=timezone.utc),
            overview="Overview text.",
            papers=[
                PaperForEmail(title="Paper One", summary="Summary one.", arxiv_id="2408.0001"),
                PaperForEmail(title="Paper Two", summary=None, arxiv_id="2408.0002"),
            ],
        )
    ]

    content = render_digest_email("RLHF", digests)

    assert content.subject == "RLHF: 2 new papers"
    assert "Overview text." in content.text_body
    assert "Paper One" in content.text_body
    assert "Summary one." in content.text_body
    assert "2408.0001" in content.text_body
    assert "Paper Two" in content.html_body
    assert "<h1>RLHF</h1>" in content.html_body


def test_render_digest_email_singular_paper_count_in_subject():
    digests = [
        DigestForEmail(
            generated_at=datetime(2024, 8, 1, tzinfo=timezone.utc),
            overview=None,
            papers=[PaperForEmail(title="Only Paper", summary=None, arxiv_id="2408.0001")],
        )
    ]

    content = render_digest_email("RLHF", digests)

    assert content.subject == "RLHF: 1 new paper"


def test_render_digest_email_spans_multiple_days():
    digests = [
        DigestForEmail(
            generated_at=datetime(2024, 8, 1, tzinfo=timezone.utc),
            overview=None,
            papers=[PaperForEmail(title="Day One Paper", summary=None, arxiv_id="1")],
        ),
        DigestForEmail(
            generated_at=datetime(2024, 8, 3, tzinfo=timezone.utc),
            overview=None,
            papers=[PaperForEmail(title="Day Two Paper", summary=None, arxiv_id="2")],
        ),
    ]

    content = render_digest_email("RLHF", digests)

    assert content.subject == "RLHF: 2 new papers"
    assert "Day One Paper" in content.text_body
    assert "Day Two Paper" in content.text_body
    assert content.text_body.index("Day One Paper") < content.text_body.index("Day Two Paper")
