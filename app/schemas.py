from datetime import datetime
from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator

from app.models import DigestStatus, SubscriptionCadence
from app.topic_query import normalize_query


# ---------- Topic ----------
class TopicCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=200)
    # 200 = topics.normalized_query's length; normalizing never lengthens a query.
    query: str = Field(
        ..., min_length=1, max_length=200, description="arXiv search query, e.g. 'RLHF'"
    )
    description: Optional[str] = Field(
        None,
        max_length=2000,
        description="What you do and don't want from this topic; guides the relevance rating",
    )

    @field_validator("query")
    @classmethod
    def _query_is_not_empty(cls, query: str) -> str:
        # Checked here so it is a 422, not the router's 409 for a duplicate.
        try:
            normalize_query(query)
        except ValueError:
            raise ValueError("query must contain more than quotes and whitespace")
        return query


class TopicUpdate(BaseModel):
    """Partial update. Only the description is editable: changing a topic's
    name or query would change what its existing papers and watermark mean.
    An explicit `"description": null` clears it."""

    description: Optional[str] = Field(None, max_length=2000)


class TopicOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    name: str
    query: str
    description: Optional[str] = None
    created_at: datetime
    last_checked_at: Optional[datetime] = None


# ---------- Paper ----------
class PaperOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    arxiv_id: str
    title: str
    abstract: Optional[str] = None
    summary: Optional[str] = None
    published_at: Optional[datetime] = None
    comment: Optional[str] = None
    journal_ref: Optional[str] = None
    max_author_h_index: Optional[int] = None


# ---------- Digest ----------
class DigestOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    topic_id: str
    generated_at: datetime
    status: DigestStatus
    overview: Optional[str] = None
    error: Optional[str] = None
    papers: list[PaperOut] = []


# ---------- Subscription ----------
class SubscriptionCreate(BaseModel):
    email: EmailStr
    cadence: SubscriptionCadence = SubscriptionCadence.twice_weekly
    max_papers: Optional[int] = Field(
        None, ge=1, description="Most papers from this topic per email (newest first); omit for no cap"
    )


class SubscriptionUpdate(BaseModel):
    """Partial update - only fields actually sent are changed, so an explicit
    `"max_papers": null` removes the cap while omitting it leaves it alone."""

    cadence: Optional[SubscriptionCadence] = None
    max_papers: Optional[int] = Field(None, ge=1)
    active: Optional[bool] = None


class SubscriptionOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    topic_id: str
    email: str
    cadence: SubscriptionCadence
    max_papers: Optional[int] = None
    active: bool
    last_sent_at: datetime
    created_at: datetime


# ---------- Admin: OpenAI usage ----------
class PurposeUsage(BaseModel):
    calls: int
    input_tokens: int
    cached_input_tokens: int
    output_tokens: int
    cost_usd: float


class TopicUsage(BaseModel):
    # None groups calls with no topic, or whose topic has since been deleted.
    topic_id: Optional[str] = None
    topic_name: Optional[str] = None
    calls: int
    cost_usd: float


class UsageReport(BaseModel):
    month: str  # YYYY-MM, UTC
    budget_usd: float
    spent_usd: float
    # Spend-guard state; only meaningful for the current month, else None.
    state: Optional[Literal["ok", "soft", "hard"]] = None
    by_purpose: dict[str, PurposeUsage]
    by_topic: list[TopicUsage]  # most expensive first
