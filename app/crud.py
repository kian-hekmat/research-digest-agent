from sqlalchemy.orm import Session
from sqlalchemy.exc import IntegrityError

from app import models, schemas


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


def get_or_create_paper(db: Session, arxiv_id: str, title: str, abstract: str, published_at) -> models.Paper:
    existing = db.query(models.Paper).filter(models.Paper.arxiv_id == arxiv_id).first()
    if existing:
        return existing
    paper = models.Paper(
        arxiv_id=arxiv_id, title=title, abstract=abstract, published_at=published_at
    )
    db.add(paper)
    db.commit()
    db.refresh(paper)
    return paper


def list_papers_for_topic(db: Session, topic_id: str) -> list[models.Paper]:
    topic = get_topic(db, topic_id)
    if topic is None:
        return []
    return topic.papers


def create_digest(db: Session, topic_id: str) -> models.Digest:
    digest = models.Digest(topic_id=topic_id, status="pending")
    db.add(digest)
    db.commit()
    db.refresh(digest)
    return digest


def get_digest(db: Session, digest_id: str) -> models.Digest | None:
    return db.query(models.Digest).filter(models.Digest.id == digest_id).first()


def list_digests(db: Session, skip: int = 0, limit: int = 50) -> list[models.Digest]:
    return db.query(models.Digest).offset(skip).limit(limit).all()
