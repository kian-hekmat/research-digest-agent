"""Plain data-transfer types passed between the Workflow and its Activities.

Deliberately self-contained (stdlib only: dataclasses + datetime) so this
module is safe to import directly into `workflows.py` without tripping
Temporal's workflow sandbox - unlike `app.services.arxiv.ArxivResult` (pulls in
httpx) or the ORM models (pull in SQLAlchemy), which only `activities.py` may
touch.

Deliberately does NOT use `from __future__ import annotations`: Temporal's
payload converter resolves dataclass field types via `dataclasses.fields()`,
which returns raw (unresolved) string annotations under postponed evaluation.
That silently breaks datetime fields specifically (datetime needs concrete
type-object recognition) when the dataclass is nested inside a generic like
`list[...]` - discovered because it doesn't raise, it fails the workflow task
and Temporal retries it forever, which just looks like a hang. Python 3.10+'s
`X | None` syntax works fine at runtime without the future import, so there's
no downside to leaving it out here.
"""

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


# ---------- email delivery ----------
@dataclass
class DueSubscription:
    """One subscription whose cadence window has elapsed."""

    subscription_id: str
    topic_id: str
    topic_name: str
    email: str
    last_sent_at: datetime


@dataclass
class GatherContentInput:
    topic_id: str
    topic_name: str
    since: datetime


@dataclass
class DigestEmailContent:
    subject: str
    text_body: str
    html_body: str


@dataclass
class SendEmailInput:
    email: str
    content: DigestEmailContent


@dataclass
class MarkSentInput:
    subscription_id: str
    sent_at: datetime
