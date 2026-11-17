"""Public signup and self-service state changes. The routes in
app.routers.public parse forms and render; everything that touches the
database for a subscriber lives here."""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from typing import Mapping

from email_validator import EmailNotValidError, validate_email
from sqlalchemy import func
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from app import models
from app.config import get_settings
from app.models import gen_uuid
from app.topic_query import validate_public_query

MAX_TOPIC_ROWS = 5
MAX_LABEL = 200
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")
_TYPOGRAPHIC_APOSTROPHES = str.maketrans({"’": "'", "‘": "'"})


@dataclass
class SignupTopic:
    query: str  # normalized phrase
    label: str | None
    max_papers: int


def parse_email(raw: str | None) -> str | None:
    """The address, normalized by email-validator; None if it isn't one, or
    if it carries a line break (header injection)."""
    if not raw or "\r" in raw or "\n" in raw:
        return None
    try:
        return validate_email(raw.strip(), check_deliverability=False).normalized
    except EmailNotValidError:
        return None


def parse_topic(raw: str) -> str:
    """The normalized public phrase, or ValueError with a message for people."""
    try:
        return validate_public_query(raw.translate(_TYPOGRAPHIC_APOSTROPHES))
    except ValueError as exc:
        if str(exc) == "empty query":
            raise ValueError("Enter a topic phrase, like \"reward modeling\".") from None
        raise


def parse_label(raw: str | None) -> tuple[str | None, str | None]:
    """(label, error)."""
    label = _CONTROL.sub("", raw or "").strip()
    if len(label) > MAX_LABEL:
        return None, f"Labels can be at most {MAX_LABEL} characters."
    return label or None, None


def parse_papers(raw: str | None) -> tuple[int | None, str | None]:
    s = get_settings()
    if not (raw or "").strip():
        return s.public_papers_per_topic_default, None
    try:
        n = int(raw)
    except ValueError:
        n = None
    if n is None or not s.public_papers_per_topic_min <= n <= s.public_papers_per_topic_max:
        return None, (
            f"Choose between {s.public_papers_per_topic_min} and "
            f"{s.public_papers_per_topic_max} papers."
        )
    return n, None


def parse_signup(form: Mapping[str, str]) -> tuple[str | None, list[SignupTopic], dict[str, str]]:
    """(normalized email, topics, field errors) from the signup form: fields
    `email`, `us_resident`, and `topic_i` / `label_i` / `papers_i` for i in 0..4."""
    errors: dict[str, str] = {}
    email = parse_email(form.get("email"))
    if email is None:
        errors["email"] = "Enter a valid email address."
    topics: list[SignupTopic] = []
    seen: set[str] = set()
    for i in range(MAX_TOPIC_ROWS):
        raw = (form.get(f"topic_{i}") or "").strip()
        if not raw:
            continue
        try:
            query = parse_topic(raw)
        except ValueError as exc:
            errors[f"topic_{i}"] = str(exc)
            continue
        if query in seen:
            errors[f"topic_{i}"] = "You already listed this topic."
            continue
        seen.add(query)
        label, label_error = parse_label(form.get(f"label_{i}"))
        papers, papers_error = parse_papers(form.get(f"papers_{i}"))
        if label_error:
            errors[f"label_{i}"] = label_error
        if papers_error:
            errors[f"papers_{i}"] = papers_error
        if not (label_error or papers_error):
            topics.append(SignupTopic(query, label, papers))
    if not topics and not any(k.startswith("topic_") for k in errors):
        errors["topics"] = "Add at least one topic."
    if form.get("us_resident") not in ("on", "yes", "true", "1"):
        errors["us_resident"] = "This service is for US residents only; please confirm you are one."
    return email, topics, errors


def find_subscriber(db: Session, email: str) -> models.Subscriber | None:
    return (
        db.query(models.Subscriber)
        .filter(func.lower(models.Subscriber.email) == email.lower())
        .one_or_none()
    )


def _upsert_subscriber(db: Session, email: str) -> models.Subscriber:
    # Safe under concurrent signups for one address: the unique index on
    # lower(email) turns the loser's insert into a no-op.
    db.execute(insert(models.Subscriber).values(id=gen_uuid(), email=email).on_conflict_do_nothing())
    return find_subscriber(db, email)


def topic_for(db: Session, query: str) -> models.Topic:
    """The topic for a normalized public phrase, created if new (safe under
    concurrent signups, by the unique normalized_query)."""
    db.execute(
        insert(models.Topic)
        .values(id=gen_uuid(), name=query, query=query, normalized_query=query)
        .on_conflict_do_nothing(index_elements=["normalized_query"])
    )
    return db.query(models.Topic).filter(models.Topic.normalized_query == query).one()


def is_active(sub: models.Subscriber) -> bool:
    return sub.confirmed_at is not None and sub.unsubscribed_at is None


def record_signup(
    db: Session, email: str, topics: list[SignupTopic], now: datetime
) -> tuple[str | None, models.Subscriber | None]:
    """Apply a valid signup. A confirmed, subscribed address is left as is and
    gets a manage link ("manage"); a new, pending or unsubscribed one gets the
    submitted topics in place of any it had, and a confirmation ("confirm").
    Commits."""
    sub = _upsert_subscriber(db, email)
    if is_active(sub):
        db.commit()
        return "manage", sub
    for old in list(sub.subscriptions):
        db.delete(old)
    db.flush()
    for t in topics:
        db.add(models.Subscription(
            topic_id=topic_for(db, t.query).id, subscriber_id=sub.id,
            max_papers=t.max_papers, label=t.label, active=True,
        ))
    sub.us_resident_attested_at = now
    db.commit()
    db.refresh(sub)
    return "confirm", sub


def confirm_subscriber(
    db: Session, sub: models.Subscriber, signed_at: datetime, now: datetime
) -> list[str] | None:
    """Confirm from a link signed at `signed_at`. None: the link predates the
    person's latest unsubscribe (treat as expired). []: already active, so
    nothing changes - a repeat can't push back their next digest. Otherwise
    activate, start every subscription's window now, apply the caps, and
    return the waitlist reasons. Commits."""
    from app.waitlist import apply_waitlist

    if sub.unsubscribed_at is not None and signed_at < sub.unsubscribed_at:
        return None
    if is_active(sub):
        return []
    sub.confirmed_at = now
    sub.unsubscribed_at = None
    for s in sub.subscriptions:
        s.last_sent_at = now
    reasons = apply_waitlist(db, sub, list(sub.subscriptions), now)
    db.commit()
    return reasons


# ---------- self-service (manage page, unsubscribe, re-subscribe) ----------
def subscription_for(sub: models.Subscriber, topic_id: str | None) -> models.Subscription | None:
    return next((s for s in sub.subscriptions if s.topic_id == topic_id), None)


def add_topic(
    db: Session, sub: models.Subscriber, raw_topic: str, raw_label: str | None, raw_papers: str | None, now: datetime
) -> tuple[list[str], str | None]:
    """(waitlist reasons, error) for adding one topic from the manage page.
    Same rules as signup, at most five topics, then the cap check. Commits."""
    from app.waitlist import apply_waitlist

    if len(sub.subscriptions) >= MAX_TOPIC_ROWS:
        return [], f"You can follow at most {MAX_TOPIC_ROWS} topics; remove one first."
    try:
        query = parse_topic((raw_topic or "").strip())
    except ValueError as exc:
        return [], str(exc)
    label, error = parse_label(raw_label)
    papers, papers_error = parse_papers(raw_papers)
    if error or papers_error:
        return [], error or papers_error
    topic = topic_for(db, query)
    if subscription_for(sub, topic.id) is not None:
        db.rollback()
        return [], "You already follow this topic."
    new = models.Subscription(
        topic_id=topic.id, subscriber_id=sub.id, max_papers=papers, label=label, active=True, last_sent_at=now
    )
    db.add(new)
    db.flush()
    reasons = apply_waitlist(db, sub, [new], now)
    db.commit()
    return reasons, None


def update_topic(db: Session, sub: models.Subscriber, topic_id: str, raw_label: str | None, raw_papers: str | None) -> str | None:
    """Edit one topic's label and papers per email; an error message or None. Commits."""
    s = subscription_for(sub, topic_id)
    if s is None:
        return "That topic isn't in your list."
    label, error = parse_label(raw_label)
    papers, papers_error = parse_papers(raw_papers)
    if error or papers_error:
        return error or papers_error
    s.label, s.max_papers = label, papers
    db.commit()
    return None


def set_paused(db: Session, sub: models.Subscriber, paused: bool, now: datetime) -> list[str]:
    """Pause or resume every topic (Subscription.active only). Resuming goes
    through the cap check. Commits."""
    from app.waitlist import apply_waitlist

    for s in sub.subscriptions:
        s.active = not paused
    reasons = [] if paused else apply_waitlist(db, sub, list(sub.subscriptions), now)
    db.commit()
    return reasons


def unsubscribe(db: Session, payload: dict, now: datetime) -> None:
    """One-click unsubscribe: a topic link deletes that subscription, an
    "all" link sets unsubscribed_at. A link whose target is gone is a no-op.
    Commits."""
    sub = db.get(models.Subscriber, payload.get("s")) if isinstance(payload.get("s"), str) else None
    if sub is None:
        return
    if payload.get("t"):
        s = subscription_for(sub, payload["t"])
        if s is not None:
            db.delete(s)
    elif sub.unsubscribed_at is None:
        sub.unsubscribed_at = now
    db.commit()


def resubscribe(db: Session, sub: models.Subscriber, topic_id: str | None, now: datetime) -> tuple[list[str], str | None]:
    """Undo an unsubscribe link: recreate the topic's subscription with the
    defaults, or clear unsubscribed_at; then the cap check. Commits."""
    from app.waitlist import apply_waitlist

    if topic_id:
        if subscription_for(sub, topic_id) is None:
            if db.get(models.Topic, topic_id) is None:
                return [], "That topic no longer exists."
            if len(sub.subscriptions) >= MAX_TOPIC_ROWS:
                return [], f"You can follow at most {MAX_TOPIC_ROWS} topics; remove one first."
            db.add(models.Subscription(
                topic_id=topic_id, subscriber_id=sub.id,
                max_papers=get_settings().public_papers_per_topic_default, active=True, last_sent_at=now,
            ))
            db.flush()
            db.refresh(sub)
    else:
        sub.unsubscribed_at = None
    reasons = apply_waitlist(db, sub, list(sub.subscriptions), now)
    db.commit()
    return reasons, None
