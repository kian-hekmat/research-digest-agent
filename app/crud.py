import uuid
from datetime import datetime

from sqlalchemy.orm import Session
from sqlalchemy.exc import IntegrityError

from app import models, schemas


def _is_valid_uuid(value: str) -> bool:
    """IDs are stored as UUID strings; reject anything that isn't one so
    lookups return a clean 404 instead of a DB DataError."""
    try:
        uuid.UUID(str(value))
        return True
    except (ValueError, TypeError):
        return False


def create_topic(db: Session, topic: schemas.TopicCreate) -> models.Topic:
    db_topic = models.Topic(name=topic.name, query=topic.query)
    db.add(db_topic)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise ValueError(f"Topic '{topic.name}' already exists")
    db.refresh(db_topic)
    return db_topic


def get_topic(db: Session, topic_id: str) -> models.Topic | None:
    if not _is_valid_uuid(topic_id):
        return None
    return db.query(models.Topic).filter(models.Topic.id == topic_id).first()


def list_topics(db: Session, skip: int = 0, limit: int = 50) -> list[models.Topic]:
    return db.query(models.Topic).offset(skip).limit(limit).all()


def list_topic_ids(db: Session) -> list[str]:
    """Every topic id, unpaginated - for the scheduled fan-out, which needs
    all of them, not a page."""
    return [row.id for row in db.query(models.Topic.id).all()]


def delete_topic(db: Session, topic_id: str) -> bool:
    db_topic = get_topic(db, topic_id)
    if db_topic is None:
        return False
    db.delete(db_topic)
    db.commit()
    return True


def get_or_create_paper(
    db: Session,
    *,
    arxiv_id: str,
    title: str,
    abstract: str | None,
    published_at,
    topic: models.Topic | None = None,
) -> models.Paper:
    """Upsert a paper by arXiv id; when `topic` is given, ensure it's linked.

    Does not commit — the caller owns the transaction boundary.
    """
    paper = (
        db.query(models.Paper)
        .filter(models.Paper.arxiv_id == arxiv_id)
        .first()
    )
    if paper is None:
        paper = models.Paper(
            arxiv_id=arxiv_id,
            title=title,
            abstract=abstract,
            published_at=published_at,
        )
        db.add(paper)
        db.flush()  # assign paper.id so association rows can be written

    if topic is not None and topic not in paper.topics:
        paper.topics.append(topic)

    return paper


def link_paper_to_digest(digest: models.Digest, paper: models.Paper) -> None:
    if paper not in digest.papers:
        digest.papers.append(paper)


def finalize_digest(
    db: Session,
    digest: models.Digest,
    *,
    status: models.DigestStatus,
    overview: str | None = None,
    error: str | None = None,
) -> models.Digest:
    digest.status = status
    digest.overview = overview
    digest.error = error
    db.commit()
    db.refresh(digest)
    return digest


def advance_topic_watermark(
    db: Session, topic: models.Topic, checked_at: datetime
) -> None:
    """Move the topic's ingestion high-water mark forward. Called only after a
    successful run, so a failed fetch is retried against the same window."""
    topic.last_checked_at = checked_at
    db.commit()


def list_papers_for_topic(db: Session, topic_id: str) -> list[models.Paper]:
    topic = get_topic(db, topic_id)
    if topic is None:
        return []
    return topic.papers


def create_digest(db: Session, topic_id: str) -> models.Digest:
    digest = models.Digest(topic_id=topic_id, status=models.DigestStatus.pending)
    db.add(digest)
    db.commit()
    db.refresh(digest)
    return digest


def get_digest(db: Session, digest_id: str) -> models.Digest | None:
    if not _is_valid_uuid(digest_id):
        return None
    return db.query(models.Digest).filter(models.Digest.id == digest_id).first()


def list_digests(db: Session, skip: int = 0, limit: int = 50) -> list[models.Digest]:
    return db.query(models.Digest).offset(skip).limit(limit).all()


def list_completed_digests_since(
    db: Session, topic_id: str, since: datetime
) -> list[models.Digest]:
    """Completed digests for a topic generated after `since`, oldest first -
    the window an email delivery covers."""
    return (
        db.query(models.Digest)
        .filter(
            models.Digest.topic_id == topic_id,
            models.Digest.status == models.DigestStatus.completed,
            models.Digest.generated_at > since,
        )
        .order_by(models.Digest.generated_at)
        .all()
    )


# ---------- Subscriptions ----------
def create_subscription(
    db: Session, topic_id: str, email: str, cadence: models.SubscriptionCadence
) -> models.Subscription:
    sub = models.Subscription(topic_id=topic_id, email=email, cadence=cadence)
    db.add(sub)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise ValueError(f"{email} is already subscribed to this topic")
    db.refresh(sub)
    return sub


def list_subscriptions_for_topic(db: Session, topic_id: str) -> list[models.Subscription]:
    topic = get_topic(db, topic_id)
    if topic is None:
        return []
    return topic.subscriptions


def get_subscription(db: Session, subscription_id: str) -> models.Subscription | None:
    if not _is_valid_uuid(subscription_id):
        return None
    return (
        db.query(models.Subscription)
        .filter(models.Subscription.id == subscription_id)
        .first()
    )


def delete_subscription(db: Session, subscription_id: str) -> bool:
    sub = get_subscription(db, subscription_id)
    if sub is None:
        return False
    db.delete(sub)
    db.commit()
    return True


def list_due_subscriptions(db: Session, now: datetime) -> list[models.Subscription]:
    """Active subscriptions whose cadence window has elapsed since their last
    send. Filtered in Python, not SQL - the per-row interval depends on each
    subscription's own cadence, and the table is small."""
    subs = db.query(models.Subscription).filter(models.Subscription.active.is_(True)).all()
    return [
        s for s in subs if now - s.last_sent_at >= models.CADENCE_INTERVALS[s.cadence]
    ]


def mark_subscription_sent(
    db: Session, subscription: models.Subscription, sent_at: datetime
) -> None:
    """Only call this after a confirmed send - mirrors advance_topic_watermark's
    only-on-success rule, so a failed send is retried on the next run rather
    than silently skipped."""
    subscription.last_sent_at = sent_at
    db.commit()
