from datetime import datetime
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field


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
    status: str
    papers: list[PaperOut] = []
