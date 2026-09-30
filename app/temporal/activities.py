"""Temporal Activities: the only place in the digest pipeline allowed to touch
the DB, arXiv, or Anthropic. Each activity opens and closes its own DB session
- activities are stateless invocations, potentially retried or run on a
different worker process entirely.

These are Phase 2's `run_digest` steps (that function is retired), split at
I/O boundaries so `DigestWorkflow` can apply a retry policy per boundary and
stay itself free of I/O.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from temporalio import activity
from temporalio.exceptions import ApplicationError

from app import crud, models
from app.config import get_settings
from app.database import SessionLocal
from app.services.arxiv import search_arxiv
from app.services.email import (
    DigestForEmail,
    EmailSender,
    PaperForEmail,
    TopicSection,
    build_topic_section,
    render_digest_email,
)
from app.services.summarize import Summarizer
from app.temporal.types import (
    AdvanceWatermarkInput,
    DigestContext,
    DueSubscription,
    FinalizeDigestInput,
    GatherContentInput,
    GatheredContent,
    IngestedPaper,
    IngestPapersInput,
    MarkSentInput,
    PaperResult,
    SendEmailInput,
    SummarizePaperInput,
    WriteOverviewInput,
)


@activity.defn
def list_active_topic_ids() -> list[str]:
    """All topics right now. There's no per-topic enable/disable flag yet, so
    every topic is "active"."""
    db = SessionLocal()
    try:
        return crud.list_topic_ids(db)
    finally:
        db.close()


@activity.defn
def create_pending_digest(topic_id: str) -> str:
    db = SessionLocal()
    try:
        topic = crud.get_topic(db, topic_id)
        if topic is None:
            raise ApplicationError(f"Topic {topic_id} not found", non_retryable=True)
        digest = crud.create_digest(db, topic_id)
        return digest.id
    finally:
        db.close()


@activity.defn
def get_digest_context(digest_id: str) -> DigestContext:
    """Load the topic behind a digest. Not-found is not retryable - the
    digest/topic won't appear on a later attempt."""
    db = SessionLocal()
    try:
        digest = crud.get_digest(db, digest_id)
        if digest is None:
            raise ApplicationError(f"Digest {digest_id} not found", non_retryable=True)
        topic = digest.topic
        return DigestContext(
            topic_id=topic.id,
            topic_name=topic.name,
            query=topic.query,
            since=topic.last_checked_at,
        )
    finally:
        db.close()


@activity.defn
def fetch_arxiv(ctx: DigestContext) -> list[PaperResult]:
    """The one network call to arXiv. Left to raise on failure - the workflow's
    retry policy governs backoff/attempts, matching what we saw arXiv actually
    do in practice (redirects, 429s, slow responses).

    Looks back `arxiv_lookback_days` past the watermark: filtering strictly on
    `published > last_checked_at` silently and permanently dropped every paper
    announced after a run that had already moved the watermark past its
    submission time (see Settings.arxiv_lookback_days). Re-fetched papers the
    topic already has are skipped in `ingest_papers`."""
    since = ctx.since
    if since is not None:
        since = since - timedelta(days=get_settings().arxiv_lookback_days)
    results = search_arxiv(ctx.query, since=since)
    return [
        PaperResult(
            arxiv_id=r.arxiv_id,
            title=r.title,
            abstract=r.abstract,
            published_at=r.published_at,
        )
        for r in results
    ]


@activity.defn
def ingest_papers(input: IngestPapersInput) -> list[IngestedPaper]:
    """Upsert + link fetched papers to the topic and this digest. Papers the
    topic already has (from an earlier run's overlapping lookback window) are
    skipped, so each paper lands in exactly one of a topic's digests and is
    never emailed twice."""
    db = SessionLocal()
    try:
        digest = crud.get_digest(db, input.digest_id)
        if digest is None:
            raise ApplicationError(f"Digest {input.digest_id} not found", non_retryable=True)
        topic = digest.topic
        already_ingested = crud.arxiv_ids_linked_to_topic(
            db, topic.id, [r.arxiv_id for r in input.results]
        )

        ingested = []
        for r in input.results:
            if r.arxiv_id in already_ingested:
                continue
            paper = crud.get_or_create_paper(
                db,
                arxiv_id=r.arxiv_id,
                title=r.title,
                abstract=r.abstract,
                published_at=r.published_at,
                topic=topic,
            )
            crud.link_paper_to_digest(digest, paper)
            ingested.append(
                IngestedPaper(
                    paper_id=paper.id,
                    title=paper.title,
                    abstract=paper.abstract,
                    existing_summary=paper.summary,
                )
            )
        db.commit()
        return ingested
    finally:
        db.close()


@activity.defn
def summarize_paper(input: SummarizePaperInput) -> str | None:
    """Summarize one paper and persist it immediately, so a later retry of a
    *different* paper in the same run never redoes this one. Returns None
    (persisted as no summary) when Summarizer is in no-key mode - not a
    failure, so it's never counted as one in digest.error."""
    summary = Summarizer().summarize_paper(input.title, input.abstract or "")
    db = SessionLocal()
    try:
        paper = db.get(models.Paper, input.paper_id)
        if paper is not None:
            paper.summary = summary
            db.commit()
        return summary
    finally:
        db.close()


@activity.defn
def write_overview(input: WriteOverviewInput) -> str | None:
    return Summarizer().write_overview(input.topic_name, input.summaries)


@activity.defn
def finalize_digest(input: FinalizeDigestInput) -> None:
    db = SessionLocal()
    try:
        digest = crud.get_digest(db, input.digest_id)
        if digest is None:
            raise ApplicationError(f"Digest {input.digest_id} not found", non_retryable=True)
        crud.finalize_digest(
            db,
            digest,
            status=models.DigestStatus(input.status),
            overview=input.overview,
            error=input.error,
        )
    finally:
        db.close()


@activity.defn
def advance_watermark(input: AdvanceWatermarkInput) -> None:
    db = SessionLocal()
    try:
        topic = db.get(models.Topic, input.topic_id)
        if topic is not None:
            crud.advance_topic_watermark(db, topic, input.checked_at)
    finally:
        db.close()


# ---------- email delivery ----------
@activity.defn
def list_due_subscriptions() -> list[DueSubscription]:
    db = SessionLocal()
    try:
        now = datetime.now(timezone.utc)
        subs = crud.list_due_subscriptions(db, now)
        return [
            DueSubscription(
                subscription_id=s.id,
                topic_id=s.topic_id,
                topic_name=s.topic.name,
                email=s.email,
                last_sent_at=s.last_sent_at,
                max_papers=s.max_papers,
            )
            for s in subs
        ]
    finally:
        db.close()


@activity.defn
def gather_digest_content(input: GatherContentInput) -> GatheredContent | None:
    """Render one email covering every topic in `input.topics` that has at
    least one new paper. None means none of them do - the workflow skips the
    send and leaves every watermark untouched. A topic with nothing new is
    left out of the email (and out of `subscription_ids`), so its watermark
    stays put too."""
    db = SessionLocal()
    try:
        sections: list[TopicSection] = []
        included: list[str] = []
        for window in input.topics:
            digests = crud.list_completed_digests_since(db, window.topic_id, window.since)
            batches = [
                DigestForEmail(
                    generated_at=d.generated_at,
                    overview=d.overview,
                    papers=[
                        PaperForEmail(
                            title=p.title,
                            summary=p.summary,
                            arxiv_id=p.arxiv_id,
                            published_at=p.published_at,
                        )
                        for p in d.papers
                    ],
                )
                for d in digests
            ]
            section = build_topic_section(window.topic_name, batches, window.max_papers)
            if section is not None:
                sections.append(section)
                included.append(window.subscription_id)
        if not sections:
            return None
        content = render_digest_email(
            sections, summaries_enabled=get_settings().summaries_enabled
        )
        return GatheredContent(content=content, subscription_ids=included)
    finally:
        db.close()


@activity.defn
def send_digest_email(input: SendEmailInput) -> None:
    EmailSender().send(input.email, input.content)


@activity.defn
def mark_subscription_sent(input: MarkSentInput) -> None:
    db = SessionLocal()
    try:
        sub = db.get(models.Subscription, input.subscription_id)
        if sub is not None:
            crud.mark_subscription_sent(db, sub, input.sent_at)
    finally:
        db.close()
