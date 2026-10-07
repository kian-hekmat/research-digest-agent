import enum
import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Column,
    String,
    Text,
    DateTime,
    Enum,
    ForeignKey,
    Integer,
    SmallInteger,
    Table,
    UniqueConstraint,
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


class SubscriptionCadence(str, enum.Enum):
    """How often a subscriber gets an emailed roundup of a topic's digests."""

    twice_weekly = "twice_weekly"
    weekly = "weekly"
    biweekly = "biweekly"


# How far apart two emails to the same subscription should be. The send
# schedule fires twice a week (Mon + Thu) and each subscription's own cadence
# decides whether it's actually due - see app.crud.list_due_subscriptions.
# twice_weekly's 3 days is the shorter Mon->Thu gap; Thu->Mon (4 days) clears
# it trivially.
CADENCE_INTERVALS: dict[SubscriptionCadence, timedelta] = {
    SubscriptionCadence.twice_weekly: timedelta(days=3),
    SubscriptionCadence.weekly: timedelta(days=7),
    SubscriptionCadence.biweekly: timedelta(days=14),
}

# Slack subtracted from a cadence interval when judging due-ness. A send's
# watermark is stamped a few seconds *after* the schedule fires, so the next
# fire lands a few seconds *short* of a full interval - without slack, an exact
# `>= 7 days` check would skip every other fire. It also absorbs a catch-up
# fire at an odd hour (e.g. the laptop waking at 13:00 on a Monday).
CADENCE_DUE_TOLERANCE = timedelta(hours=12)


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
    # LLM-rated 1-10: how central the paper is to *this* topic (see
    # app.services.ranking). NULL = not rated (no LLM, or the call failed).
    Column("relevance", SmallInteger, nullable=True),
    CheckConstraint("relevance BETWEEN 1 AND 10", name="ck_topic_paper_relevance_range"),
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
    # High-water mark for arXiv ingestion: the next digest run only pulls papers
    # published after this. NULL means "never run" -> pull everything.
    last_checked_at = Column(DateTime(timezone=True), nullable=True)

    papers = relationship(
        "Paper", secondary=topic_paper_association, back_populates="topics"
    )
    digests = relationship(
        "Digest",
        back_populates="topic",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )
    subscriptions = relationship(
        "Subscription",
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
    # arXiv's free-text author comment and journal reference - where an
    # "Accepted at ..." note lives. Refreshed whenever arXiv returns the paper.
    comment = Column(Text, nullable=True)
    journal_ref = Column(Text, nullable=True)
    # Highest h-index among the authors, from Semantic Scholar. NULL = not
    # looked up yet, or not indexed yet (fresh papers lag a day or so).
    max_author_h_index = Column(Integer, nullable=True)
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
    # When the run finished as completed. Email delivery keys off this, not
    # generated_at: a digest still running when an email goes out must land
    # in the *next* email - keyed on generated_at it would fall behind the
    # new watermark and never be sent at all.
    completed_at = Column(DateTime(timezone=True), nullable=True, index=True)
    overview = Column(Text, nullable=True)  # LLM-synthesized paragraph across the batch

    topic = relationship("Topic", back_populates="digests")
    papers = relationship(
        "Paper", secondary=digest_paper_association, back_populates="digests"
    )


class Subscription(Base):
    __tablename__ = "subscriptions"
    __table_args__ = (
        UniqueConstraint("topic_id", "email", name="uq_subscription_topic_email"),
    )

    id = Column(UUID(as_uuid=False), primary_key=True, default=gen_uuid)
    topic_id = Column(
        UUID(as_uuid=False),
        ForeignKey("topics.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    email = Column(String, nullable=False)
    cadence = Column(
        Enum(SubscriptionCadence, name="subscription_cadence"),
        nullable=False,
        server_default=SubscriptionCadence.twice_weekly.value,
    )
    active = Column(Boolean, nullable=False, server_default="true")
    # Cap on papers per topic in one email (highest-ranked kept, see
    # app.services.ranking); NULL = no cap.
    max_papers = Column(Integer, nullable=True)
    # High-water mark for delivery (mirrors Topic.last_checked_at): set to
    # "now" at creation, so a new subscriber's first email lands on their
    # first cadence boundary rather than dumping a historical backlog.
    last_sent_at = Column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    created_at = Column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at = Column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=utcnow,
    )

    topic = relationship("Topic", back_populates="subscriptions")
