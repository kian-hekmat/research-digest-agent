import enum
import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Column,
    String,
    Text,
    DateTime,
    Enum,
    Float,
    ForeignKey,
    Identity,
    Index,
    Integer,
    Numeric,
    SmallInteger,
    Table,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import relationship
from sqlalchemy.types import UserDefinedType

from app.database import Base
from app.topic_query import normalize_query


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
    # A rating from outside the pipeline - a person, or a stronger model doing
    # reference labeling - and who gave it. These, not the LLM's own
    # ratings, are the few-shot anchors that calibrate new ratings (see
    # app.services.retrieval.rating_anchors). NULL = not labeled.
    Column("reference_relevance", SmallInteger, nullable=True),
    Column("reference_labeler", String, nullable=True),
    CheckConstraint(
        "reference_relevance BETWEEN 1 AND 10", name="ck_topic_paper_reference_relevance_range"
    ),
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


def _normalized_query_default(context) -> str:
    """Fills Topic.normalized_query from the row's query when the caller didn't
    set it, so a Topic built anywhere still gets the one canonical form."""
    return normalize_query(context.get_current_parameters()["query"])


class Topic(Base):
    __tablename__ = "topics"
    __table_args__ = (
        UniqueConstraint("normalized_query", name="uq_topics_normalized_query"),
    )

    id = Column(UUID(as_uuid=False), primary_key=True, default=gen_uuid)
    # Not unique: topics are shared by query, and two subscribers may well
    # give different topics the same name.
    name = Column(String(200), nullable=False)
    query = Column(String, nullable=False)  # arXiv search query for this topic
    # app.topic_query.normalize_query(query): what makes two topics the same
    # topic, so subscribers who type it differently share one arXiv fetch.
    normalized_query = Column(String(200), nullable=False, default=_normalized_query_default)
    # What the reader does and doesn't want from this topic, in their words.
    # Shown to the relevance rater; most useful for whole-category queries
    # like `cat:math.NA`, where "relevant to the topic" says little.
    description = Column(Text, nullable=True)
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
    # Full text (app.services.fulltext), stored as kind="chunk" rows in
    # paper_embeddings: "ok", "unavailable" (arXiv has no HTML for it),
    # "empty" (HTML with no body text), "error" (fetch failed); NULL = never
    # tried. Checked once, at ingestion.
    fulltext_status = Column(String(16), nullable=True)
    fulltext_checked_at = Column(DateTime(timezone=True), nullable=True)
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


class TopicEmbedding(Base):
    """A topic's normalized query, embedded, for "join the existing topic?"
    suggestions (app.services.retrieval.similar_topics). Keyed by model like
    paper_embeddings, so a new model can be backfilled side by side."""

    __tablename__ = "topic_embeddings"

    topic_id = Column(
        UUID(as_uuid=False),
        ForeignKey("topics.id", ondelete="CASCADE"),
        primary_key=True,
    )
    model = Column(String, primary_key=True)
    content = Column(Text, nullable=False)  # exactly the text that was embedded
    embedding = Column(Vector(EMBEDDING_DIM), nullable=False)
    created_at = Column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


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


# The papers of an edition, in rank order (1 = best).
edition_papers = Table(
    "edition_papers",
    Base.metadata,
    Column(
        "edition_id",
        UUID(as_uuid=False),
        ForeignKey("topic_editions.id", ondelete="CASCADE"),
        primary_key=True,
    ),
    Column(
        "paper_id",
        UUID(as_uuid=False),
        ForeignKey("papers.id", ondelete="CASCADE"),
        primary_key=True,
    ),
    Column("rank", Integer, nullable=False),
)


class TopicEdition(Base):
    """One stored send per topic per send slot. Its paper window is
    (window_start, window_end], chained from the previous edition's end."""

    __tablename__ = "topic_editions"
    __table_args__ = (
        UniqueConstraint("topic_id", "slot", name="uq_topic_editions_topic_slot"),
        CheckConstraint("status IN ('ready', 'fallback')", name="ck_topic_editions_status"),
    )

    id = Column(UUID(as_uuid=False), primary_key=True, default=gen_uuid)
    topic_id = Column(
        UUID(as_uuid=False),
        ForeignKey("topics.id", ondelete="CASCADE"),
        nullable=False,
    )
    slot = Column(DateTime(timezone=True), nullable=False)
    window_start = Column(DateTime(timezone=True), nullable=False)
    window_end = Column(DateTime(timezone=True), nullable=False)
    status = Column(String(16), nullable=False)
    overview = Column(Text, nullable=True)
    k = Column(Integer, nullable=False)
    prepared_at = Column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    papers = relationship(
        "Paper",
        secondary=edition_papers,
        order_by=edition_papers.c.rank,
        viewonly=True,
    )


class Subscriber(Base):
    """One person, by email (case-insensitive). The lifecycle timestamps live
    here rather than on each subscription: confirming or unsubscribing is about
    the person, not one topic."""

    __tablename__ = "subscribers"

    id = Column(UUID(as_uuid=False), primary_key=True, default=gen_uuid)
    email = Column(String, nullable=False)
    confirmed_at = Column(DateTime(timezone=True), nullable=True)
    unsubscribed_at = Column(DateTime(timezone=True), nullable=True)
    waitlisted_at = Column(DateTime(timezone=True), nullable=True)
    last_manage_link_sent_at = Column(DateTime(timezone=True), nullable=True)
    us_resident_attested_at = Column(DateTime(timezone=True), nullable=True)
    # Bumped to invalidate every link already emailed to this subscriber.
    token_version = Column(Integer, nullable=False, server_default="0")
    created_at = Column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at = Column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=utcnow,
    )

    subscriptions = relationship(
        "Subscription",
        back_populates="subscriber",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )


# "Me@Example.com" and "me@example.com" are one subscriber.
Index("uq_subscribers_email_lower", func.lower(Subscriber.email), unique=True)


class Subscription(Base):
    __tablename__ = "subscriptions"
    __table_args__ = (
        UniqueConstraint("topic_id", "subscriber_id", name="uq_subscription_topic_subscriber"),
    )

    id = Column(UUID(as_uuid=False), primary_key=True, default=gen_uuid)
    topic_id = Column(
        UUID(as_uuid=False),
        ForeignKey("topics.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    subscriber_id = Column(
        UUID(as_uuid=False),
        ForeignKey("subscribers.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    # The subscriber's own display name for this shared topic. Display only:
    # never sent to the LLM, which sees the normalized query.
    label = Column(String(200), nullable=True)
    # Set while this subscription's topic is held back by the topic cap.
    waitlisted_at = Column(DateTime(timezone=True), nullable=True)
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
    subscriber = relationship("Subscriber", back_populates="subscriptions")

    @property
    def email(self) -> str:
        """The subscriber's email, so code that only needs a recipient
        (SubscriptionOut, the send workflow) doesn't care where it lives."""
        return self.subscriber.email


# Purposes an OpenAI call can be recorded under (app.services.spend.PURPOSES;
# duplicated here so the check constraint doesn't import the service layer).
LLM_USAGE_PURPOSES = ("rating", "summary", "overview", "embedding", "preview")


class LlmUsage(Base):
    """One OpenAI call: what it was for, which topic, its tokens and cost.
    Written by app.spend_ledger.DbLedger as soon as the call is paid for; the
    spend guard sums this table for the month. Topic deletion keeps the row
    (the money was still spent)."""

    __tablename__ = "llm_usage"
    __table_args__ = (
        CheckConstraint(
            "purpose IN (" + ", ".join(f"'{p}'" for p in LLM_USAGE_PURPOSES) + ")",
            name="ck_llm_usage_purpose",
        ),
    )

    id = Column(BigInteger, Identity(), primary_key=True)
    created_at = Column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), index=True
    )
    model = Column(String, nullable=False)
    purpose = Column(String(16), nullable=False)
    topic_id = Column(
        UUID(as_uuid=False),
        ForeignKey("topics.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    input_tokens = Column(Integer, nullable=False)
    cached_input_tokens = Column(Integer, nullable=False)
    output_tokens = Column(Integer, nullable=False)
    cost_usd = Column(Numeric(12, 6), nullable=False)
    # True when OpenAI's response carried no usage and tokens were estimated.
    estimated = Column(Boolean, nullable=False, server_default="false", default=False)


class SentAlert(Base):
    """An operational alert that has been emailed, by key (e.g.
    "spend-80:2026-11"), so each one goes out at most once."""

    __tablename__ = "sent_alerts"

    key = Column(String, primary_key=True)
    sent_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())
