"""Email delivery via SMTP, plus rendering a batch of digests into an email.

`EmailSender` wraps smtplib so it can be injected as a Temporal Activity and
faked in tests - constructing it never opens a connection, only `.send()`
does, and the SMTP client itself is swappable via `smtp_client_factory` (same
injection pattern as `Summarizer(client=...)`). `render_digest_email` is a
pure function, independently testable without a DB, SMTP, or Temporal.
"""
from __future__ import annotations

import smtplib
from dataclasses import dataclass
from datetime import datetime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

from app.config import get_settings
from app.temporal.types import DigestEmailContent


@dataclass
class PaperForEmail:
    title: str
    summary: str | None
    arxiv_id: str


@dataclass
class DigestForEmail:
    """One day's completed digest, reduced to what the email needs."""

    generated_at: datetime
    overview: str | None
    papers: list[PaperForEmail]


def render_digest_email(topic_name: str, digests: list[DigestForEmail]) -> DigestEmailContent:
    total_papers = sum(len(d.papers) for d in digests)
    subject = f"{topic_name}: {total_papers} new paper{'' if total_papers == 1 else 's'}"

    text_lines = [f"Your {topic_name} digest", ""]
    html_parts = [f"<h1>{topic_name}</h1>"]

    for digest in digests:
        day = digest.generated_at.strftime("%B %-d, %Y")
        text_lines.append(f"-- {day} --")
        html_parts.append(f"<h2>{day}</h2>")

        if digest.overview:
            text_lines.append(digest.overview)
            html_parts.append(f"<p><em>{digest.overview}</em></p>")

        for paper in digest.papers:
            text_lines.append(f"* {paper.title}")
            if paper.summary:
                text_lines.append(f"  {paper.summary}")
            text_lines.append(f"  https://arxiv.org/abs/{paper.arxiv_id}")

            html_parts.append(f"<p><strong>{paper.title}</strong>")
            if paper.summary:
                html_parts.append(f"<br>{paper.summary}")
            html_parts.append(
                f'<br><a href="https://arxiv.org/abs/{paper.arxiv_id}">'
                f"arxiv.org/abs/{paper.arxiv_id}</a></p>"
            )
        text_lines.append("")

    return DigestEmailContent(
        subject=subject,
        text_body="\n".join(text_lines),
        html_body="\n".join(html_parts),
    )


class EmailSender:
    def __init__(self, settings=None, smtp_client_factory=None) -> None:
        self._settings = settings or get_settings()
        # Defaults to a real smtplib.SMTP connected to the configured host -
        # tests pass a fake factory instead.
        self._smtp_client_factory = smtp_client_factory

    def _make_client(self) -> smtplib.SMTP:
        if self._smtp_client_factory is not None:
            return self._smtp_client_factory()
        return smtplib.SMTP(self._settings.smtp_host, self._settings.smtp_port, timeout=10)

    def send(self, to: str, content: DigestEmailContent) -> None:
        msg = MIMEMultipart("alternative")
        msg["Subject"] = content.subject
        msg["From"] = self._settings.smtp_from_address
        msg["To"] = to
        msg.attach(MIMEText(content.text_body, "plain"))
        msg.attach(MIMEText(content.html_body, "html"))

        with self._make_client() as client:
            if self._settings.smtp_use_tls:
                client.starttls()
            if self._settings.smtp_username:
                client.login(self._settings.smtp_username, self._settings.smtp_password)
            client.sendmail(self._settings.smtp_from_address, [to], msg.as_string())
