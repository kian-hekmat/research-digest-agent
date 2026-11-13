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
    # LLM_MAX_CONCURRENCY, read by the activity so the run's history records
    # it: a replay after the setting changes keeps the cap the run started with.
    llm_max_concurrency: int = 8


@dataclass
class PipelineLimits:
    """Concurrency settings a workflow needs, read through an activity so
    they're recorded in its history (see DigestContext.llm_max_concurrency)."""

    llm_max_concurrency: int
    fanout_batch_size: int


@dataclass
class PipelineAlertInput:
    """An operational alert to the owner, sent at most once per `key`
    (app.alerts.send_alert); the key carries its own period."""

    key: str
    subject: str
    body: str


@dataclass
class PaperResult:
    """One arXiv search hit, decoupled from `app.services.arxiv.ArxivResult`."""

    arxiv_id: str
    title: str
    abstract: str
    published_at: datetime | None
    comment: str | None = None
    journal_ref: str | None = None


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
class EmbedPapersInput:
    """Papers from this run to embed for retrieval (ones that already have
    an embedding are skipped by the activity)."""

    paper_ids: list[str] = field(default_factory=list)
    # The digest's topic, embedded too if it has no embedding yet (for
    # similar-topic suggestions). Empty for older callers.
    topic_id: str = ""


@dataclass
class FetchFulltextInput:
    """Papers from this run whose full text to fetch and index."""

    paper_ids: list[str] = field(default_factory=list)


@dataclass
class SummarizePaperInput:
    """Summarize a paper and rate its relevance to the topic in one LLM call.
    Also the input to `rate_relevance`, for a paper that already has a
    summary (from another topic) and only needs this topic's rating."""

    paper_id: str
    title: str
    abstract: str
    topic_id: str = ""
    topic_name: str = ""
    topic_query: str = ""


@dataclass
class WriteOverviewInput:
    topic_name: str
    summaries: list[str] = field(default_factory=list)
    # For a history-aware overview; empty (as in workflows started before it
    # existed) means no history.
    topic_id: str = ""
    digest_id: str = ""
    paper_ids: list[str] = field(default_factory=list)


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


# ---------- send prep (editions) ----------
@dataclass
class PrepTopicInput:
    """Prepare `topic_id`'s edition for the send at `slot`."""

    topic_id: str
    slot: datetime


@dataclass
class PlanEditionInput:
    topic_id: str
    slot: datetime
    window_end: datetime  # workflow.now() at plan time


@dataclass
class EditionPlan:
    """What an edition will cover. Settings are read here, by the activity,
    so the prep run's history records them (see DigestContext)."""

    topic_id: str
    topic_name: str
    topic_query: str
    slot: datetime
    window_start: datetime
    window_end: datetime
    k: int
    exists: bool = False
    paper_ids: list[str] = field(default_factory=list)
    # Window papers with no rating for this topic yet, ready to re-rate.
    unrated: list[SummarizePaperInput] = field(default_factory=list)
    llm_max_concurrency: int = 8
    fulltext: bool = False


@dataclass
class TopPaper:
    """One of an edition's top K, with its summary if it has one already."""

    paper_id: str
    title: str
    abstract: str
    summary: str | None = None


@dataclass
class EditionRanking:
    paper_ids: list[str] = field(default_factory=list)  # every window paper, best first
    top: list[TopPaper] = field(default_factory=list)  # the first K of them


@dataclass
class StoreEditionInput:
    topic_id: str
    slot: datetime
    window_start: datetime
    window_end: datetime
    k: int
    overview: str | None
    ranked_paper_ids: list[str] = field(default_factory=list)


# ---------- email delivery ----------
@dataclass
class DueSubscription:
    """One subscription whose cadence window has elapsed."""

    subscription_id: str
    topic_id: str
    topic_name: str
    email: str
    last_sent_at: datetime
    max_papers: int | None = None


@dataclass
class EditionKey:
    """A topic's edition for one send slot."""

    topic_id: str
    slot: datetime


@dataclass
class TopicWindow:
    """One subscription's slice of a recipient's email: its topic's edition
    for this slot, capped at `max_papers`."""

    subscription_id: str
    topic_id: str
    topic_name: str
    edition_id: str
    max_papers: int | None = None


@dataclass
class GatherContentInput:
    topics: list[TopicWindow] = field(default_factory=list)


@dataclass
class DigestEmailContent:
    subject: str
    text_body: str
    html_body: str


@dataclass
class GatheredContent:
    """A rendered email plus which subscriptions actually contributed papers
    to it - only those get their watermark advanced after the send."""

    content: DigestEmailContent
    as_of: datetime  # snapshot time - becomes the contributing subscriptions' watermark
    subscription_ids: list[str] = field(default_factory=list)


@dataclass
class SendEmailInput:
    email: str
    content: DigestEmailContent


@dataclass
class MarkSentInput:
    subscription_id: str
    sent_at: datetime
