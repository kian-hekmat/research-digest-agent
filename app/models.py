import enum
import uuid
from datetime import datetime, timezone

from sqlalchemy import (
    Column,
    String,
    Text,
    DateTime,
    Enum,
    ForeignKey,
    Table,
    func,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import relationship

from app.database import Base


def gen_uuid() -> str:
    return str(uuid.uuid4())


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class DigestStatus(str, enum.Enum):
    """Lifecycle of a digest run. Enforced at the DB level as a native enum."""

    pending = "pending"
    completed = "completed"
    failed = "failed"


# Many-to-many: a paper can match several topics, a topic can match many papers.
topic_paper_association = Table(
    "topic_paper",
    Base.metadata,
    Column(
        "topic_id",
        UUID(as_uuid=False),
        ForeignKey("topics.id", ondelete="CASCADE"),
        primary_key=True,
    ),
    Column(
        "paper_id",
        UUID(as_uuid=False),
        ForeignKey("papers.id", ondelete="CASCADE"),
        primary_key=True,
    ),
)

# Many-to-many: a digest bundles many papers, and a paper can appear in the
# digests of every topic it matched (and in re-runs).
digest_paper_association = Table(
    "digest_paper",
    Base.metadata,
    Column(
        "digest_id",
        UUID(as_uuid=False),
        ForeignKey("digests.id", ondelete="CASCADE"),
        primary_key=True,
    ),
    Column(
        "paper_id",
        UUID(as_uuid=False),
        ForeignKey("papers.id", ondelete="CASCADE"),
        primary_key=True,
    ),
)


class Topic(Base):
    __tablename__ = "topics"

    id = Column(UUID(as_uuid=False), primary_key=True, default=gen_uuid)
    name = Column(String(100), nullable=False, unique=True)
    query = Column(String, nullable=False)  # arXiv search query for this topic
    created_at = Column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at = Column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=utcnow,
    )

    papers = relationship(
        "Paper", secondary=topic_paper_association, back_populates="topics"
    )
    digests = relationship(
        "Digest",
        back_populates="topic",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )


class Paper(Base):
    __tablename__ = "papers"

    id = Column(UUID(as_uuid=False), primary_key=True, default=gen_uuid)
    arxiv_id = Column(String, nullable=False, unique=True)
    title = Column(String, nullable=False)
    abstract = Column(Text, nullable=True)
    summary = Column(Text, nullable=True)  # LLM-generated summary, filled in Phase 2
    published_at = Column(DateTime(timezone=True), nullable=True, index=True)
    created_at = Column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at = Column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=utcnow,
    )

    topics = relationship(
        "Topic", secondary=topic_paper_association, back_populates="papers"
    )
    digests = relationship(
        "Digest", secondary=digest_paper_association, back_populates="papers"
    )


class Digest(Base):
    __tablename__ = "digests"

    id = Column(UUID(as_uuid=False), primary_key=True, default=gen_uuid)
    topic_id = Column(
        UUID(as_uuid=False),
        ForeignKey("topics.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    generated_at = Column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at = Column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=utcnow,
    )
    status = Column(
        Enum(DigestStatus, name="digest_status"),
        nullable=False,
        server_default=DigestStatus.pending.value,
        index=True,
    )
    error = Column(Text, nullable=True)  # failure detail when status == failed

    topic = relationship("Topic", back_populates="digests")
    papers = relationship(
        "Paper", secondary=digest_paper_association, back_populates="digests"
    )
