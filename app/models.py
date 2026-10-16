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
    Float,
    ForeignKey,
    Integer,
    SmallInteger,
    Table,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import relationship
from sqlalchemy.types import UserDefinedType

from app.database import Base


def gen_uuid() -> str:
    return str(uuid.uuid4())


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Vector(UserDefinedType):
    """pgvector's `vector(N)` column. Values cross the wire in pgvector's
    text form, "[0.1,0.2,...]", so this needs no driver adapter or extra
    dependency. Similarity queries are written in SQL (app.services.retrieval)."""

    cache_ok = True

    def __init__(self, dim: int) -> None:
        self.dim = dim

    def get_col_spec(self, **kw) -> str:
        return f"vector({self.dim})"

    def bind_processor(self, dialect):
        def process(value):
            if value is None:
                return None
            return "[" + ",".join(repr(float(x)) for x in value) + "]"

        return process

    def result_processor(self, dialect, coltype):
        def process(value):
            if value is None:
                return None
            return [float(x) for x in value.strip("[]").split(",")]

        return process


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


# When each cadence is due for an email: see app.cadence.


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

# Which earlier papers a paper's related-work note cites (see
# app.services.summarize). Only citations that checked out are stored.
paper_related_association = Table(
    "paper_related",
    Base.metadata,
    Column(
        "paper_id",
        UUID(as_uuid=False),
        ForeignKey("papers.id", ondelete="CASCADE"),
        primary_key=True,
    ),
    Column(
        "related_paper_id",
        UUID(as_uuid=False),
        ForeignKey("papers.id", ondelete="CASCADE"),
        primary_key=True,
    ),
    Column("similarity", Float, nullable=False),
    CheckConstraint("paper_id <> related_paper_id", name="ck_paper_related_not_self"),
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
    # --- retrieval (app.services.retrieval) ---
    # One LLM-written sentence on how this paper relates to earlier ones the
    # reader has seen, citing them; NULL when none was close enough or the
    # model's citations didn't check out.
    related_note = Column(Text, nullable=True)
    # Highest cosine similarity to an earlier-published, topic-linked paper
    # within the novelty window - low = novel. NULL = not computed, or nothing
    # earlier to compare with.
    prior_similarity = Column(Float, nullable=True)
    # An earlier paper so similar this one is effectively the same work
    # (companion paper, resubmission under a new id); emails fold it in.
    near_duplicate_of_id = Column(
        UUID(as_uuid=False), ForeignKey("papers.id", ondelete="SET NULL"), nullable=True
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

    topics = relationship(
        "Topic", secondary=topic_paper_association, back_populates="papers"
    )
    digests = relationship(
        "Digest", secondary=digest_paper_association, back_populates="papers"
    )
    near_duplicate_of = relationship("Paper", remote_side=[id], foreign_keys=[near_duplicate_of_id])
    related_papers = relationship(
        "Paper",
        secondary=paper_related_association,
        primaryjoin=lambda: Paper.id == paper_related_association.c.paper_id,
        secondaryjoin=lambda: Paper.id == paper_related_association.c.related_paper_id,
        viewonly=True,
    )


EMBEDDING_DIM = 768  # vector(N) width; app.config.Settings.embedding_dim must match


class PaperEmbedding(Base):
    """One embedded piece of a paper, for retrieval (see app.services.retrieval).

    `model` is part of the key so a new embedding model can be backfilled side
    by side and cut over to, instead of overwriting. `kind`/`chunk_ix` leave
    room for full-text chunks; for now every row is kind "abstract", chunk 0.
    """

    __tablename__ = "paper_embeddings"

    paper_id = Column(
        UUID(as_uuid=False),
        ForeignKey("papers.id", ondelete="CASCADE"),
        primary_key=True,
    )
    model = Column(String, primary_key=True)
    kind = Column(String, primary_key=True, default="abstract")
    chunk_ix = Column(Integer, primary_key=True, default=0)
    content = Column(Text, nullable=False)  # exactly the text that was embedded
    embedding = Column(Vector(EMBEDDING_DIM), nullable=False)
    created_at = Column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
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
