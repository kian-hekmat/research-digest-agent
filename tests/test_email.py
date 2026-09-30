import re
from datetime import datetime, timezone
from email import message_from_string
from html.parser import HTMLParser

from app.config import Settings
from app.services.email import (
    DigestForEmail,
    EmailSender,
    PaperForEmail,
    build_topic_section,
    render_digest_email,
)
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
    # Pin every field this test asserts on explicitly - Settings reads a local
    # .env for anything omitted, so relying on the class-level default here
    # would make the test's outcome (and, worse, any assertion-failure message)
    # depend on - and potentially print - the developer's real SMTP credentials.
    settings = Settings(
        smtp_from_address="digest@example.com",
        smtp_use_tls=False,
        smtp_username="",
        smtp_password="",
    )
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


def test_email_sender_builds_a_well_formed_multipart_message():
    """What a mail client actually receives: multipart/alternative with a
    plain-text and an HTML part, both decoding back to exactly what was
    rendered - including non-ASCII characters common in paper titles."""
    fake = FakeSMTPClient()
    settings = Settings(smtp_from_address="digest@example.com", smtp_use_tls=False, smtp_username="")
    sender = EmailSender(settings=settings, smtp_client_factory=lambda: fake)
    content = render_digest_email(
        [_section("PINNs", [_paper("1", "Schrödinger–Poisson solvers", day=3)])]
    )

    sender.send("someone@example.com", content)

    msg = message_from_string(fake.sendmail_args[2])
    assert msg.get_content_type() == "multipart/alternative"
    assert msg["Subject"] == content.subject
    assert msg["To"] == "someone@example.com"
    plain, html_part = msg.get_payload()
    assert plain.get_content_type() == "text/plain"
    assert html_part.get_content_type() == "text/html"
    assert plain.get_payload(decode=True).decode(plain.get_content_charset()) == content.text_body
    assert html_part.get_payload(decode=True).decode(html_part.get_content_charset()) == content.html_body


# ---------- build_topic_section / render_digest_email ----------
def _paper(arxiv_id, title=None, *, day=None, summary="A summary."):
    return PaperForEmail(
        title=title or f"Paper {arxiv_id}",
        summary=summary,
        arxiv_id=arxiv_id,
        published_at=datetime(2026, 9, day, 14, 0, tzinfo=timezone.utc) if day else None,
    )


def _digest(papers, *, day=16, hour=6, overview=None):
    return DigestForEmail(
        generated_at=datetime(2026, 9, day, hour, 0, tzinfo=timezone.utc),
        overview=overview,
        papers=list(papers),
    )


def _section(name, papers, **kwargs):
    return build_topic_section(name, [_digest(papers)], **kwargs)


def _date_headers(text_body):
    """Every line the plain-text body renders as a date heading."""
    return [line for line in text_body.splitlines() if re.fullmatch(r"[A-Z][a-z]+ \d{1,2}, \d{4}", line)]


class _TagBalanceChecker(HTMLParser):
    VOID = {"br"}

    def __init__(self):
        super().__init__()
        self.stack, self.errors = [], []

    def handle_starttag(self, tag, attrs):
        if tag not in self.VOID:
            self.stack.append(tag)

    def handle_endtag(self, tag):
        if not self.stack or self.stack.pop() != tag:
            self.errors.append(tag)


def _assert_well_formed_html(html_body):
    checker = _TagBalanceChecker()
    checker.feed(html_body)
    checker.close()
    assert checker.errors == [] and checker.stack == [], (checker.errors, checker.stack)


def test_regression_same_day_runs_and_fetch_date_mislabeling():
    """The 2026-09-28 email, reproduced: four RLHF runs on Sept 16 (three of
    them empty) plus a week of empty daily runs, and one run that fetched 25
    papers all *submitted* Sept 15. It rendered "September 16" four times,
    a heading for every empty run, and filed all 25 papers under the day they
    were fetched. Each date must now appear once, only for days that have
    papers, and be the papers' own submission date."""
    backlog = [_paper(f"2609.{i:05d}", day=15) for i in range(25)]
    digests = [
        _digest([], day=16, hour=1),
        _digest(backlog, day=16, hour=1, overview="Overview of the backlog."),
        _digest([], day=16, hour=5),
        _digest([], day=16, hour=6),
        *[_digest([], day=d) for d in (17, 18, 22, 23, 24, 25, 26, 27)],
    ]

    section = build_topic_section("RLHF", digests)
    content = render_digest_email([section])

    assert _date_headers(content.text_body) == ["September 15, 2026"]
    assert "September 16" not in content.text_body
    assert content.html_body.count("<h3>") == 1
    assert "<h3>September 15, 2026</h3>" in content.html_body
    assert content.subject == "RLHF: 25 new papers"
    assert content.text_body.count("https://arxiv.org/abs/") == 25


def test_section_is_none_when_every_digest_is_empty():
    assert build_topic_section("RLHF", [_digest([], day=d) for d in (16, 17, 18)]) is None


def test_papers_grouped_by_submission_date_newest_first_each_date_once():
    # Papers from the same day arrive via two different runs - still one heading.
    digests = [
        _digest([_paper("a", day=20), _paper("b", day=22)], day=23),
        _digest([_paper("c", day=22), _paper("d", day=25)], day=26),
    ]

    content = render_digest_email([build_topic_section("RLHF", digests)])

    assert _date_headers(content.text_body) == [
        "September 25, 2026",
        "September 22, 2026",
        "September 20, 2026",
    ]
    body = content.text_body
    assert body.index("Paper d") < body.index("Paper b") < body.index("Paper a")
    assert body.index("Paper c") < body.index("September 20, 2026")


def test_paper_found_by_two_runs_is_listed_once():
    digests = [
        _digest([_paper("dup", day=20)], day=21),
        _digest([_paper("dup", day=20), _paper("new", day=22)], day=23),
    ]

    section = build_topic_section("RLHF", digests)

    assert [p.arxiv_id for p in section.papers] == ["new", "dup"]
    assert render_digest_email([section]).text_body.count("abs/dup") == 1


def test_paper_without_submission_date_falls_back_to_its_run_date():
    section = build_topic_section("RLHF", [_digest([_paper("x")], day=21)])

    assert _date_headers(render_digest_email([section]).text_body) == ["September 21, 2026"]


def test_max_papers_keeps_the_newest_and_reports_the_rest():
    papers = [_paper(f"p{d}", day=d) for d in range(1, 13)]  # 12 papers, Sept 1-12

    section = build_topic_section("RLHF", [_digest(papers, day=13)], max_papers=10)
    content = render_digest_email([section])

    assert [p.arxiv_id for p in section.papers] == [f"p{d}" for d in range(12, 2, -1)]
    assert section.omitted == 2
    assert content.subject == "RLHF: 10 new papers"
    assert content.text_body.count("https://arxiv.org/abs/") == 10
    assert "abs/p1\n" not in content.text_body and "abs/p2\n" not in content.text_body
    assert "+ 2 more RLHF papers not shown (showing the 10 most recent)." in content.text_body
    assert "2 more RLHF papers not shown" in content.html_body


def test_no_omitted_note_when_under_the_cap():
    section = _section("RLHF", [_paper("a", day=1)], max_papers=10)

    assert section.omitted == 0
    assert "not shown" not in render_digest_email([section]).text_body


def test_overviews_only_from_runs_whose_papers_made_the_cut():
    digests = [
        _digest([_paper("old", day=1)], day=2, overview="Old run overview."),
        _digest([_paper("new", day=9)], day=10, overview="New run overview."),
        _digest([], day=11, overview="Empty run overview."),
    ]

    section = build_topic_section("RLHF", digests, max_papers=1)

    assert section.overviews == ["New run overview."]


def test_combined_email_for_the_10_5_5_subscription_mix():
    """The actual configuration: one email, RLHF capped at 10, PINNs at 5,
    numerical analysis at 5 - each topic its own section, in order, with
    exactly its capped number of papers and a working link for each."""
    def many(prefix, n):
        return [_paper(f"{prefix}{i}", f"{prefix} paper {i}", day=1 + i % 28) for i in range(n)]

    sections = [
        build_topic_section("RLHF", [_digest(many("rlhf", 14))], max_papers=10),
        build_topic_section("PINNs", [_digest(many("pinn", 8))], max_papers=5),
        build_topic_section("Numerical Analysis", [_digest(many("na", 30))], max_papers=5),
    ]

    content = render_digest_email(sections)

    assert content.subject == "Research digest: 20 new papers (RLHF, PINNs, Numerical Analysis)"
    text = content.text_body
    rlhf_at, pinn_at, na_at = (
        text.index("RLHF - 10 new papers"),
        text.index("PINNs - 5 new papers"),
        text.index("Numerical Analysis - 5 new papers"),
    )
    assert rlhf_at < pinn_at < na_at
    assert text[rlhf_at:pinn_at].count("https://arxiv.org/abs/rlhf") == 10
    assert text[pinn_at:na_at].count("https://arxiv.org/abs/pinn") == 5
    assert text[na_at:].count("https://arxiv.org/abs/na") == 5
    assert "+ 4 more RLHF papers not shown" in text
    assert "+ 3 more PINNs papers not shown" in text
    assert "+ 25 more Numerical Analysis papers not shown" in text

    html_body = content.html_body
    assert html_body.count("<h2>") == 3
    assert html_body.count('<a href="https://arxiv.org/abs/') == 20
    _assert_well_formed_html(html_body)


def test_every_paper_has_title_summary_and_link_in_both_bodies():
    papers = [_paper("2609.00001", "First", day=5, summary="First summary."),
              _paper("2609.00002", "Second", day=4, summary=None)]

    content = render_digest_email([_section("RLHF", papers)])

    for body in (content.text_body, content.html_body):
        assert "First" in body and "First summary." in body and "Second" in body
        assert "arxiv.org/abs/2609.00001" in body and "arxiv.org/abs/2609.00002" in body
    assert '<a href="https://arxiv.org/abs/2609.00002">' in content.html_body


def test_html_escapes_titles_and_summaries():
    """Math-heavy titles (numerical analysis especially) routinely contain
    `<` and `&`; unescaped, they corrupt the HTML part."""
    paper = _paper("1", "Error bounds for u<v & p>1", day=3, summary="Holds when a<b.")

    content = render_digest_email([_section("Numerical Analysis", [paper])])

    assert "Error bounds for u&lt;v &amp; p&gt;1" in content.html_body
    assert "a&lt;b" in content.html_body
    assert "Error bounds for u<v & p>1" in content.text_body  # plain text stays literal
    _assert_well_formed_html(content.html_body)


def test_singular_paper_count_in_subject_and_heading():
    content = render_digest_email([_section("RLHF", [_paper("a", day=1)])])

    assert content.subject == "RLHF: 1 new paper"
    assert "RLHF - 1 new paper\n" in content.text_body


def test_render_notes_when_summaries_are_disabled():
    content = render_digest_email(
        [_section("RLHF", [_paper("a", day=1, summary=None)])], summaries_enabled=False
    )

    assert "ANTHROPIC_API_KEY" in content.text_body
    assert "ANTHROPIC_API_KEY" in content.html_body
    assert "Paper a" in content.text_body  # still lists papers, just no summary text


def test_render_omits_the_note_by_default():
    content = render_digest_email([_section("RLHF", [_paper("a", day=1)])])

    assert "ANTHROPIC_API_KEY" not in content.text_body
    assert "ANTHROPIC_API_KEY" not in content.html_body
