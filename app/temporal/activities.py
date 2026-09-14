"""Temporal Activities: the only place in the digest pipeline allowed to touch
the DB, arXiv, or Anthropic. Each activity opens and closes its own DB session
- activities are stateless invocations, potentially retried or run on a
different worker process entirely.

These are Phase 2's `run_digest` steps (that function is retired), split at
I/O boundaries so `DigestWorkflow` can apply a retry policy per boundary and
stay itself free of I/O.
"""
from __future__ import annotations

from temporalio import activity
from temporalio.exceptions import ApplicationError

from app import crud, models
from app.database import SessionLocal
from app.services.arxiv import search_arxiv
from app.services.summarize import Summarizer
from app.temporal.types import (
    AdvanceWatermarkInput,
    DigestContext,
    FinalizeDigestInput,
    IngestedPaper,
    IngestPapersInput,
    PaperResult,
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
    do in practice (redirects, 429s, slow responses)."""
    results = search_arxiv(ctx.query, since=ctx.since)
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
    """Upsert + link fetched papers to the topic and this digest."""
    db = SessionLocal()
    try:
        digest = crud.get_digest(db, input.digest_id)
        if digest is None:
            raise ApplicationError(f"Digest {input.digest_id} not found", non_retryable=True)
        topic = digest.topic

        ingested = []
        for r in input.results:
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
def summarize_paper(input: SummarizePaperInput) -> str:
    """Summarize one paper and persist it immediately, so a later retry of a
    *different* paper in the same run never redoes this one."""
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
def write_overview(input: WriteOverviewInput) -> str:
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
