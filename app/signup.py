"""Public signup and self-service state changes (phase 3a). The routes in
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
