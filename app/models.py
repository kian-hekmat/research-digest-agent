import uuid
from datetime import datetime

from sqlalchemy import (
    Column,
    String,
    Text,
    DateTime,
    ForeignKey,
    Table,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import relationship

from app.database import Base


def gen_uuid():
    return str(uuid.uuid4())


# Many-to-many: a paper can match several topics, a topic can match many papers.
topic_paper_association = Table(
    "topic_paper",
    Base.metadata,
    Column("topic_id", UUID(as_uuid=False), ForeignKey("topics.id"), primary_key=True),
    Column("paper_id", UUID(as_uuid=False), ForeignKey("papers.id"), primary_key=True),
)


class Topic(Base):
    __tablename__ = "topics"

    id = Column(UUID(as_uuid=False), primary_key=True, default=gen_uuid)
    name = Column(String, nullable=False, unique=True)
    query = Column(String, nullable=False)  # e.g. arXiv search query for this topic
    created_at = Column(DateTime, default=datetime.utcnow)

    papers = relationship(
        "Paper", secondary=topic_paper_association, back_populates="topics"
    )


class Paper(Base):
    __tablename__ = "papers"

    id = Column(UUID(as_uuid=False), primary_key=True, default=gen_uuid)
    arxiv_id = Column(String, nullable=False, unique=True)
    title = Column(String, nullable=False)
    abstract = Column(Text, nullable=True)
    summary = Column(Text, nullable=True)  # LLM-generated summary, filled in Phase 2
    published_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)

    topics = relationship(
        "Topic", secondary=topic_paper_association, back_populates="papers"
    )
    digest_id = Column(UUID(as_uuid=False), ForeignKey("digests.id"), nullable=True)
    digest = relationship("Digest", back_populates="papers")


class Digest(Base):
    __tablename__ = "digests"

    id = Column(UUID(as_uuid=False), primary_key=True, default=gen_uuid)
    topic_id = Column(UUID(as_uuid=False), ForeignKey("topics.id"), nullable=False)
    generated_at = Column(DateTime, default=datetime.utcnow)
    status = Column(String, default="pending")  # pending, completed, failed

    topic = relationship("Topic")
    papers = relationship("Paper", back_populates="digest")
