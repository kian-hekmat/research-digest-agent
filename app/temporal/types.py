"""Plain data-transfer types passed between the Workflow and its Activities.

Deliberately self-contained (stdlib only: dataclasses + datetime) so this
module is safe to import directly into `workflows.py` without tripping
Temporal's workflow sandbox - unlike `app.services.arxiv.ArxivResult` (pulls in
httpx) or the ORM models (pull in SQLAlchemy), which only `activities.py` may
touch.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

# Mirrors app.models.DigestStatus values. Duplicated here (rather than imported)
# so this module - and workflows.py, which needs it - never has to import the
# SQLAlchemy-backed models package.
DIGEST_STATUS_COMPLETED = "completed"
DIGEST_STATUS_FAILED = "failed"


@dataclass
class DigestContext:
    """What the workflow needs to know about a digest's topic to run it."""

    topic_id: str
    topic_name: str
    query: str
    since: datetime | None


@dataclass
class PaperResult:
    """One arXiv search hit, decoupled from `app.services.arxiv.ArxivResult`."""

    arxiv_id: str
    title: str
    abstract: str
    published_at: datetime | None


@dataclass
class IngestPapersInput:
    topic_id: str
    digest_id: str
    results: list[PaperResult] = field(default_factory=list)


@dataclass
class IngestedPaper:
    paper_id: str
    title: str
    abstract: str | None
    existing_summary: str | None


@dataclass
class SummarizePaperInput:
    paper_id: str
    title: str
    abstract: str


@dataclass
class WriteOverviewInput:
    topic_name: str
    summaries: list[str] = field(default_factory=list)


@dataclass
class FinalizeDigestInput:
    digest_id: str
    status: str  # models.DigestStatus value ("completed" | "failed")
    overview: str | None = None
    error: str | None = None


@dataclass
class AdvanceWatermarkInput:
    topic_id: str
    checked_at: datetime
