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
