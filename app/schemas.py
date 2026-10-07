from datetime import datetime
from typing import Optional

from pydantic import BaseModel, ConfigDict, EmailStr, Field

from app.models import DigestStatus, SubscriptionCadence


# ---------- Topic ----------
class TopicCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=100)
    query: str = Field(..., min_length=1, description="arXiv search query, e.g. 'RLHF'")


class TopicOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    name: str
    query: str
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
