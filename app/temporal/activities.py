"""Temporal Activities: the only place in the digest pipeline allowed to touch
the DB, arXiv, Semantic Scholar, or the LLM. Each activity opens and closes its own DB session
- activities are stateless invocations, potentially retried or run on a
different worker process entirely.

These are Phase 2's `run_digest` steps (that function is retired), split at
I/O boundaries so `DigestWorkflow` can apply a retry policy per boundary and
stay itself free of I/O.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from temporalio import activity
from temporalio.exceptions import ApplicationError

from app import crud, models
from app.config import get_settings
from app.database import SessionLocal
from app.services.arxiv import search_arxiv
from app.services.ranking import venue_score
from app.services.retrieval import compute_signals, embed_missing, rating_anchors, related_context
from app.services.scholar import fetch_max_author_h_index
from app.services.embed import Embedder
from app.services.email import (
    DigestForEmail,
    EmailSender,
    PaperForEmail,
    TopicSection,
    build_topic_section,
    render_digest_email,
)
from app.services.summarize import (
    OverviewHistory,
    RatingAnchor,
    RatingContext,
    RelatedPaper,
    Summarizer,
)
from app.temporal.types import (
    AdvanceWatermarkInput,
    DigestContext,
    DueSubscription,
    EmbedPapersInput,
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


logger = logging.getLogger(__name__)


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
            comment=r.comment,
            journal_ref=r.journal_ref,
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
            paper = crud.get_or_create_paper(
                db,
                arxiv_id=r.arxiv_id,
                title=r.title,
                abstract=r.abstract,
                published_at=r.published_at,
                topic=topic,
                comment=r.comment,
                journal_ref=r.journal_ref,
            )
            if r.arxiv_id in already_ingested:
                continue  # comment/journal_ref refreshed above; nothing else to do
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
def embed_papers(input: EmbedPapersInput) -> int:
    """Embed this run's papers for retrieval, then compute their retrieval
    signals (novelty, near duplicates). Returns how many were newly embedded:
    0 when embeddings are off, or every paper already had one."""
    settings = get_settings()
    db = SessionLocal()
    try:
        embedder = Embedder()
        written = embed_missing(db, embedder, input.paper_ids, batch_size=settings.embedding_batch_size)
        if embedder.enabled:
            compute_signals(
                db,
                input.paper_ids,
                model=settings.embedding_model,
                near_duplicate_similarity=settings.rag_near_duplicate_similarity,
                novelty_window_days=settings.rag_novelty_window_days,
                similar_paper_similarity=settings.rag_similar_paper_similarity,
            )
        return written
    finally:
        db.close()


def _related_papers(paper_id: str) -> tuple[list[RelatedPaper], dict[str, tuple[str, float]]]:
    """Earlier papers to offer the summarizer, and arxiv_id -> (paper_id,
    similarity) for recording which ones its note cites. Retrieval is an
    enhancement: any failure here means summarizing without it."""
    settings = get_settings()
    if not (settings.embeddings_enabled and settings.rag_related_notes):
        return [], {}
    db = SessionLocal()
    try:
        found = related_context(
            db,
            paper_id,
            model=settings.embedding_model,
            k=settings.rag_top_k,
            min_similarity=settings.rag_min_similarity,
        )
    except Exception:
        logger.exception("Related-work retrieval failed for paper %s", paper_id)
        return [], {}
    finally:
        db.close()
    offered = [RelatedPaper(n.arxiv_id, n.title, n.summary or n.abstract or "") for n in found]
    return offered, {n.arxiv_id: (n.paper_id, n.similarity) for n in found}


def _rating_context(topic_id: str, paper_id: str) -> RatingContext | None:
    """The topic's description and reference-rated anchors for rating this
    paper. An enhancement like retrieval: a failure means rating without it."""
    if not topic_id:
        return None
    settings = get_settings()
    db = SessionLocal()
    try:
        topic = db.get(models.Topic, topic_id)
        anchors = []
        if settings.rag_rating_anchors:
            anchors = rating_anchors(
                db, topic_id, paper_id,
                model=settings.embedding_model, per_band=settings.rag_anchors_per_band,
            )
        return RatingContext(
            description=topic.description if topic else None,
            anchors=tuple(RatingAnchor(a.title, a.abstract, a.rating) for a in anchors),
        )
    except Exception:
        logger.exception("Rating context failed for paper %s", paper_id)
        return None
    finally:
        db.close()


@activity.defn
def summarize_paper(input: SummarizePaperInput) -> str | None:
    """Summarize one paper and rate its relevance to the topic (one LLM call),
    and persist both immediately, so a later retry of a *different* paper in
    the same run never redoes this one. Returns the summary: None (persisted
    as no summary) when Summarizer is in no-key mode - not a failure, so it's
    never counted as one in digest.error.

    With RAG_RELATED_NOTES on, earlier, similar papers are offered as related
    work, and a related-work note is stored only when its citations checked
    out (see Summarizer); its citations then replace the "similar earlier
    paper" link that embed_papers recorded."""
    related, by_arxiv_id = _related_papers(input.paper_id)
    rated = Summarizer().summarize_and_rate(
        input.title,
        input.abstract or "",
        input.topic_name,
        input.topic_query,
        related=related,
        rating_context=_rating_context(input.topic_id, input.paper_id),
    )
    if rated.related_rejected:
        logger.info("Dropped a related-work note with unchecked citations for %s", input.paper_id)
    db = SessionLocal()
    try:
        paper = db.get(models.Paper, input.paper_id)
        if paper is not None:
            paper.summary = rated.summary
            paper.related_note = rated.related_note
            if rated.related_ids:
                crud.set_paper_related(db, paper.id, [by_arxiv_id[a] for a in rated.related_ids])
            if input.topic_id:
                crud.set_topic_paper_relevance(db, input.topic_id, paper.id, rated.relevance)
            db.commit()
        return rated.summary
    finally:
        db.close()


@activity.defn
def rate_relevance(input: SummarizePaperInput) -> int | None:
    """Rate a paper that already has a summary (another topic ingested it
    first) for *this* topic - relevance is per topic, the summary isn't."""
    relevance = Summarizer().rate_relevance(
        input.title,
        input.abstract or "",
        input.topic_name,
        input.topic_query,
        rating_context=_rating_context(input.topic_id, input.paper_id),
    )
    db = SessionLocal()
    try:
        crud.set_topic_paper_relevance(db, input.topic_id, input.paper_id, relevance)
        db.commit()
        return relevance
    finally:
        db.close()


def _overview_history(input: WriteOverviewInput) -> OverviewHistory | None:
    """The topic's recent overviews and the batch's similar earlier papers.
    An enhancement: any failure means writing the overview without them."""
    settings = get_settings()
    if not (settings.rag_overview_history and input.topic_id):
        return None
    db = SessionLocal()
    try:
        previous = crud.recent_overviews(
            db, input.topic_id, exclude_digest_id=input.digest_id or None,
            limit=settings.rag_overview_history_count,
        )
        style = settings.rag_overview_history_style
        return OverviewHistory(
            previous=tuple(
                (f"{d.completed_at:%b} {d.completed_at.day}", d.overview) for d in previous
            ),
            earlier_papers=(
                tuple(crud.similar_earlier_papers(db, input.paper_ids)) if style == "compare" else ()
            ),
            style=style,
        )
    except Exception:
        logger.exception("Overview history failed for digest %s", input.digest_id)
        return None
    finally:
        db.close()


@activity.defn
def write_overview(input: WriteOverviewInput) -> str | None:
    return Summarizer().write_overview(
        input.topic_name, input.summaries, history=_overview_history(input)
    )


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
def enrich_author_h_index(input: GatherContentInput) -> int:
    """Fill in `max_author_h_index` for every paper the coming emails could
    include that doesn't have one yet (Semantic Scholar or OpenAlex, see
    app.services.scholar). Run at send time rather than ingest time on
    purpose: both lag arXiv by a day or more, so by the send more of a
    window's papers are indexed. Ones that still aren't stay NULL and are retried on
    the next send they're due in. Returns how many papers were filled in."""
    db = SessionLocal()
    try:
        missing: dict[str, models.Paper] = {}
        for window in input.topics:
            for digest in crud.list_completed_digests_since(db, window.topic_id, window.since):
                for paper in digest.papers:
                    if paper.max_author_h_index is None:
                        missing[paper.arxiv_id] = paper
        if not missing:
            return 0
        found = fetch_max_author_h_index(list(missing))
        for arxiv_id, h_index in found.items():
            missing[arxiv_id].max_author_h_index = h_index
        db.commit()
        return len(found)
    finally:
        db.close()


@activity.defn
def gather_digest_content(input: GatherContentInput) -> GatheredContent | None:
    """Render one email covering every topic in `input.topics` that has at
    least one new paper. None means none of them do - the workflow skips the
    send and leaves every watermark untouched. A topic with nothing new is
    left out of the email (and out of `subscription_ids`), so its watermark
    stays put too.

    `as_of` (returned for use as the new watermark) is taken *before* reading:
    a digest completing mid-gather has completed_at > as_of, so it's picked
    up by the next email instead of slipping between the read and the stamp."""
    as_of = datetime.now(timezone.utc)
    db = SessionLocal()
    try:
        sections: list[TopicSection] = []
        included: list[str] = []
        for window in input.topics:
            digests = crud.list_completed_digests_since(db, window.topic_id, window.since)
            relevance = crud.relevance_by_paper(
                db, window.topic_id, [p.id for d in digests for p in d.papers]
            )
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
                            relevance=relevance.get(p.id),
                            max_author_h_index=p.max_author_h_index,
                            venue_score=venue_score(p.comment, p.journal_ref),
                            prior_similarity=p.prior_similarity,
                            related_note=p.related_note,
                            similar=[(r.arxiv_id, r.title) for r in p.related_papers],
                            near_duplicate_of=(
                                p.near_duplicate_of.arxiv_id if p.near_duplicate_of else None
                            ),
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
        return GatheredContent(content=content, as_of=as_of, subscription_ids=included)
    finally:
        db.close()


@activity.defn
def count_pending_digests() -> int:
    db = SessionLocal()
    try:
        return crud.count_pending_digests(db)
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
