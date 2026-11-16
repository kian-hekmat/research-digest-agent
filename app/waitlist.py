"""The one cap check for public subscribers (phase 3a; owner's call: a
simple, hand-admitted waitlist). Called at confirmation, when a topic is
added, on resume and on re-subscribe, so leaving and coming back can't get
around a cap. Admitting is by hand (admin API); nothing auto-admits."""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import func
from sqlalchemy.orm import Session

from app import crud, models
from app.config import get_settings
from app.spend_ledger import spend_state


def _active_subscriber_count(db: Session, excluding: str) -> int:
    return (
        db.query(func.count(models.Subscriber.id))
        .filter(
            models.Subscriber.confirmed_at.is_not(None),
            models.Subscriber.unsubscribed_at.is_(None),
            models.Subscriber.waitlisted_at.is_(None),
            models.Subscriber.id != excluding,
        )
        .scalar()
    )


def _topic_active_elsewhere(db: Session, sub: models.Subscription) -> bool:
    return (
        crud.active_subscriptions_query(db)
        .filter(models.Subscription.topic_id == sub.topic_id, models.Subscription.id != sub.id)
        .first()
        is not None
    )


def _active_topic_count(db: Session) -> int:
    return crud.active_subscriptions_query(db).with_entities(models.Subscription.topic_id).distinct().count()


def apply_waitlist(
    db: Session, subscriber: models.Subscriber, subscriptions: list[models.Subscription], now: datetime
) -> list[str]:
    """Hold back what would break a cap; returns a plain reason per hold.
    Doesn't commit. The subscriber cap holds the whole person; otherwise a
    subscription to a topic nobody else is active on is held when active
    topics are at the cap or spend is past the alert line."""
    settings = get_settings()
    if subscriber.waitlisted_at is not None:
        return ["You're on the waitlist; we'll let you in as places open."]
    if _active_subscriber_count(db, subscriber.id) >= settings.max_subscribers:
        subscriber.waitlisted_at = now
        db.flush()
        return ["We're at our subscriber limit, so you're on the waitlist; we'll let you in as places open."]
    reasons = []
    db.flush()
    for sub in subscriptions:
        if sub.waitlisted_at is not None or not sub.active or _topic_active_elsewhere(db, sub):
            continue
        if _active_topic_count(db) - _counts_self(db, sub) >= settings.max_active_topics or spend_state(db, now) != "ok":
            sub.waitlisted_at = now
            db.flush()
            reasons.append(
                f'"{sub.topic.name}" is a new topic and we\'re at capacity for new topics, '
                "so it's waitlisted; your other topics are unaffected."
            )
    return reasons


def _counts_self(db: Session, sub: models.Subscription) -> int:
    """1 if `sub` itself already counts as an active topic in the query."""
    return (
        crud.active_subscriptions_query(db).filter(models.Subscription.id == sub.id).first() is not None
    )
