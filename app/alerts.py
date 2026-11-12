"""Operational alerts to the owner by email, each sent at most once per key.

The key carries its own period (e.g. "spend-80:2026-11"), so an alert re-arms
when the period changes. The `sent_alerts` row is inserted before sending and
committed only after the send succeeds: if two callers race, the second waits
on the first's insert and then finds the key taken; if the send fails, the
insert is rolled back and the next caller tries again.
"""
from __future__ import annotations

import logging
from html import escape

from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from app import models
from app.config import Settings, get_settings
from app.services.email import EmailSender
from app.temporal.types import DigestEmailContent

logger = logging.getLogger(__name__)


def _default_sender() -> EmailSender:
    return EmailSender()


def send_alert(
    db: Session,
    key: str,
    subject: str,
    body: str,
    *,
    settings: Settings | None = None,
    sender=None,
) -> bool:
    """Email `subject`/`body` to ALERT_EMAIL unless `key` was already sent.
    Returns True if this call sent it. With no ALERT_EMAIL, logs a warning
    and records nothing, so the alert still fires once an address is set.
    A failed send is re-raised after rolling back, leaving the key unsent."""
    settings = settings or get_settings()
    if not settings.alert_email:
        logger.warning("Alert %r not emailed: ALERT_EMAIL is not set. %s", key, subject)
        return False

    claimed = db.execute(
        insert(models.SentAlert)
        .values(key=key)
        .on_conflict_do_nothing(index_elements=["key"])
        .returning(models.SentAlert.key)
    ).first()
    if claimed is None:
        db.rollback()
        return False

    try:
        (sender or _default_sender()).send(
            settings.alert_email,
            DigestEmailContent(subject=subject, text_body=body, html_body=f"<pre>{escape(body)}</pre>"),
        )
    except Exception:
        db.rollback()
        raise
    db.commit()
    return True
