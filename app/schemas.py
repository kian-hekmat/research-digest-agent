from datetime import datetime
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field

from app.models import DigestStatus


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
