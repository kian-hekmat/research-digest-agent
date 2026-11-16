"""Temporal Activities: the only place in the digest pipeline allowed to touch
the DB, arXiv, Semantic Scholar, or the LLM. Each activity opens and closes its own DB session
- activities are stateless invocations, potentially retried or run on a
different worker process entirely.

These are Phase 2's `run_digest` steps (that function is retired), split at
I/O boundaries so `DigestWorkflow` can apply a retry policy per boundary and
stay itself free of I/O.

`fetch_arxiv` runs on its own task queue
(`workflows.arxiv_queue`), served by a worker that runs one activity at a
time, so there is never more than one arXiv request in flight.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from temporalio import activity
from temporalio.exceptions import ApplicationError

from app import crud, models
from app.alerts import send_alert
from app.config import get_settings
from app.database import SessionLocal
from app.services.arxiv import search_arxiv
from app.services.ranking import venue_score
from app.services.retrieval import (
    compute_signals,
    embed_missing,
)
from app.services.scholar import fetch_max_author_h_index
from app.services.spend import SpendLimitReached
from app.spend_ledger import DbLedger
from app.services.embed import Embedder
from app.services.email import (
    DigestForEmail,
    EmailLinks,
    EmailSender,
    PaperForEmail,
    TopicSection,
    build_topic_section,
    rank_papers,
    render_digest_email,
)
from app.services.summarize import (
    RatingContext,
    Summarizer,
)
from app.temporal.types import (
    AdvanceWatermarkInput,
    DigestContext,
    DueSubscription,
    EditionKey,
    EditionPlan,
    EditionRanking,
    EmbedPapersInput,
    FinalizeDigestInput,
    GatherContentInput,
    GatheredContent,
    IngestedPaper,
    IngestPapersInput,
    MarkSentInput,
    PaperResult,
    PipelineAlertInput,
    PipelineLimits,
    PlanEditionInput,
    AccountEmailInput,
    SendEmailInput,
    StoreEditionInput,
    SummarizePaperInput,
    TopPaper,
    WriteOverviewInput,
)


logger = logging.getLogger(__name__)


@activity.defn
def list_active_topic_ids() -> list[str]:
    """Topics with at least one confirmed, active subscriber right now. A topic
    nobody eligible subscribes to is not fetched (each fetch costs money)."""
    db = SessionLocal()
    try:
        return crud.list_active_topic_ids(db)
    finally:
        db.close()


@activity.defn
def get_pipeline_limits() -> PipelineLimits:
    settings = get_settings()
    return PipelineLimits(
        llm_max_concurrency=settings.llm_max_concurrency,
        fanout_batch_size=settings.fanout_batch_size,
    )


@activity.defn
def send_pipeline_alert(input: PipelineAlertInput) -> bool:
    """Email the owner an operational alert, once per key. Never raises: a
    failed alert is logged and must not fail or retry the run behind it.
    Returns whether this call sent it."""
    db = SessionLocal()
    try:
        return send_alert(db, input.key, input.subject, input.body)
    except Exception:
        logger.exception("Alert %r could not be sent", input.key)
        return False
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
            llm_max_concurrency=get_settings().llm_max_concurrency,
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
    # Before any write: a model without calibration must not leave half-
    # computed signals behind (raises MissingCalibration).
    calibration = settings.calibration
    db = SessionLocal()
    try:
        embedder = Embedder(ledger=DbLedger())
        try:
            written = embed_missing(db, embedder, input.paper_ids, batch_size=settings.embedding_batch_size)
        except SpendLimitReached as exc:
            # Retrying can't help until the limit resets or credits are added;
            # the workflow carries on without retrieval this run.
            raise ApplicationError(str(exc), non_retryable=True) from exc
        if embedder.enabled:
            compute_signals(
                db,
                input.paper_ids,
                model=settings.embedding_model,
                near_duplicate_similarity=calibration.near_duplicate,
                novelty_window_days=settings.rag_novelty_window_days,
                similar_paper_similarity=calibration.similar_paper,
            )
        return written
    finally:
        db.close()


def _rating_context(topic_id: str, paper_id: str) -> RatingContext | None:
    """The topic's description, for rating this paper. A failure means
    rating without it."""
    if not topic_id:
        return None
    db = SessionLocal()
    try:
        topic = db.get(models.Topic, topic_id)
        return RatingContext(description=topic.description if topic else None)
    except Exception:
        logger.exception("Rating context failed for paper %s", paper_id)
        return None
    finally:
        db.close()


@activity.defn
def summarize_for_edition(input: SummarizePaperInput) -> str | None:
    """Summarize one of an edition's top papers and persist the summary
    immediately, so a retry of a *different* paper never redoes this one.
    Uses the evaluated combined prompt (summarize_and_rate) but stores only
    the summary: the ingest rating stands (the combined prompt's own rating
    runs higher - docs/rag-plan.md §5.10). Returns the summary: None when
    the Summarizer is in no-key mode or the budget skipped it - not a failure."""
    rated = Summarizer(ledger=DbLedger(), topic_id=input.topic_id or None).summarize_and_rate(
        input.title,
        input.abstract or "",
        input.topic_name,
        input.topic_query,
        rating_context=_rating_context(input.topic_id, input.paper_id),
    )
    db = SessionLocal()
    try:
        paper = db.get(models.Paper, input.paper_id)
        if paper is not None:
            paper.summary = rated.summary
            db.commit()
        return rated.summary
    finally:
        db.close()


@activity.defn
def rate_relevance(input: SummarizePaperInput) -> int | None:
    """Rate a paper's relevance to *this* topic (relevance is per topic) and
    store it. The daily ingest rates every new paper this way; summaries come
    later, in send prep. None means no rating (the budget skipped the call,
    or the reply didn't parse) - stored as unrated, and not a failure."""
    relevance = Summarizer(ledger=DbLedger(), topic_id=input.topic_id or None).rate_relevance(
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


def _previous_overviews(input: WriteOverviewInput) -> list[str]:
    """The topic's recent overviews, oldest first, shown to the writer only so
    it doesn't repeat them. Any failure means writing without them."""
    settings = get_settings()
    if not (settings.rag_overview_history and input.topic_id):
        return []
    db = SessionLocal()
    try:
        previous = crud.recent_overviews(
            db, input.topic_id, exclude_digest_id=input.digest_id or None,
            limit=settings.rag_overview_history_count,
        )
        return [text for _, text in previous]
    except Exception:
        logger.exception("Overview history failed for digest %s", input.digest_id)
        return []
    finally:
        db.close()


@activity.defn
def write_overview(input: WriteOverviewInput) -> str | None:
    return Summarizer(ledger=DbLedger(), topic_id=input.topic_id or None).write_overview(
        input.topic_name, input.summaries, previous=_previous_overviews(input)
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


def _email_links(db, subscriber_id: str | None, topic_ids: list[str]) -> EmailLinks | None:
    """The recipient's manage and unsubscribe links; None (no links, no
    List-Unsubscribe) until SECRET_KEY is set."""
    from app import tokens

    sub = db.get(models.Subscriber, subscriber_id) if subscriber_id else None
    if sub is None or not get_settings().secret_key:
        return None
    return EmailLinks(
        manage_url=tokens.link("/manage", tokens.manage_token(sub)),
        unsubscribe_all_url=tokens.link("/unsubscribe", tokens.unsubscribe_token(sub.id, None)),
        topic_unsubscribe_urls={
            t: tokens.link("/unsubscribe", tokens.unsubscribe_token(sub.id, t)) for t in topic_ids
        },
    )


@activity.defn
def gather_digest_content(input: GatherContentInput) -> GatheredContent | None:
    """Render one email from each topic's stored edition, cut to the
    subscription's cap and kept in the edition's order. No LLM, arXiv or
    h-index call: send prep (or `ensure_edition`'s fallback) did that work.
    A topic whose edition has no papers is left out of the email (and out of
    `subscription_ids`, so its watermark stays put); None means every topic
    was empty and the send is skipped.

    `as_of` (the contributing subscriptions' new `last_sent_at`) is when the
    email was assembled."""
    as_of = datetime.now(timezone.utc)
    db = SessionLocal()
    try:
        sections: list[TopicSection] = []
        included: list[str] = []
        subscriber_id: str | None = None
        for window in input.topics:
            # Re-checked at send time: an unsubscribe, pause or waitlist
            # between listing and delivery is honoured (phase 3a).
            current = (
                crud.active_subscriptions_query(db)
                .filter(models.Subscription.id == window.subscription_id)
                .one_or_none()
            )
            if current is None:
                continue
            subscriber_id = current.subscriber_id
            edition = db.get(models.TopicEdition, window.edition_id)
            if edition is None or not edition.papers:
                continue
            relevance = crud.relevance_by_paper(db, edition.topic_id, [p.id for p in edition.papers])
            batch = DigestForEmail(
                generated_at=edition.window_end,
                overview=edition.overview,
                papers=[_paper_for_email(p, relevance.get(p.id)) for p in edition.papers],
            )
            # An uncapped subscription gets the top K: the papers prep summarized.
            section = build_topic_section(
                window.topic_name, [batch], window.max_papers or edition.k,
                novelty=_novelty(), order=[p.arxiv_id for p in edition.papers],
            )
            if section is not None:
                section.topic_id = edition.topic_id
                sections.append(section)
                included.append(window.subscription_id)
        if not sections:
            return None
        content = render_digest_email(
            sections,
            summaries_enabled=get_settings().summaries_enabled,
            links=_email_links(db, subscriber_id, [s.topic_id for s in sections]),
        )
        return GatheredContent(content=content, as_of=as_of, subscription_ids=included)
    finally:
        db.close()

def _paper_for_email(p: models.Paper, relevance: int | None) -> PaperForEmail:
    return PaperForEmail(
        title=p.title,
        summary=p.summary,
        arxiv_id=p.arxiv_id,
        published_at=p.published_at,
        relevance=relevance,
        max_author_h_index=p.max_author_h_index,
        venue_score=venue_score(p.comment, p.journal_ref),
        prior_similarity=p.prior_similarity,
        similar=[(r.arxiv_id, r.title) for r in p.related_papers],
        near_duplicate_of=p.near_duplicate_of.arxiv_id if p.near_duplicate_of else None,
    )


def _novelty() -> tuple[float, float]:
    cal = get_settings().calibration
    return (cal.novelty_repeat, cal.novelty_unrelated)


# ---------- send prep (editions) ----------
_MIN_K = 5
_UNCAPPED_K = 10  # an uncapped subscription (max_papers NULL) counts as 10


@activity.defn
def plan_edition(input: PlanEditionInput) -> EditionPlan:
    """What `input.topic_id`'s edition for `input.slot` covers: its window
    (chained from the previous edition), K (the largest max_papers among its
    active subscriptions, at least 5), the window's papers and the ones still
    unrated for this topic. `exists` when the slot already has an edition."""
    settings = get_settings()
    db = SessionLocal()
    try:
        topic = db.get(models.Topic, input.topic_id)
        if topic is None:
            raise ApplicationError(f"topic {input.topic_id} not found", non_retryable=True)
        plan = EditionPlan(
            topic_id=topic.id,
            topic_name=topic.name,
            topic_query=topic.query,
            slot=input.slot,
            window_start=input.window_end,
            window_end=input.window_end,
            k=_edition_k(db, topic.id),
            llm_max_concurrency=settings.llm_max_concurrency,
        )
        if crud.get_edition(db, topic.id, input.slot) is not None:
            plan.exists = True
            return plan
        plan.window_start = crud.edition_window_start(db, topic.id, input.window_end)
        papers = crud.window_papers(db, topic.id, plan.window_start, input.window_end)
        rated = crud.relevance_by_paper(db, topic.id, [p.id for p in papers])
        plan.paper_ids = [p.id for p in papers]
        plan.unrated = [
            SummarizePaperInput(
                paper_id=p.id, title=p.title, abstract=p.abstract or "",
                topic_id=topic.id, topic_name=topic.name, topic_query=topic.query,
            )
            for p in papers
            if rated.get(p.id) is None
        ]
        return plan
    finally:
        db.close()


@activity.defn
def enrich_h_index_for_papers(paper_ids: list[str]) -> int:
    """Fill in `max_author_h_index` for those of `paper_ids` without one
    (Semantic Scholar or OpenAlex, app.services.scholar). Run at prep rather
    than ingest: both lag arXiv by a day or more. Returns how many were filled."""
    db = SessionLocal()
    try:
        missing = {
            p.arxiv_id: p
            for p in db.query(models.Paper).filter(
                models.Paper.id.in_(paper_ids), models.Paper.max_author_h_index.is_(None)
            )
        }
        if not missing:
            return 0
        found = fetch_max_author_h_index(list(missing))
        for arxiv_id, h_index in found.items():
            missing[arxiv_id].max_author_h_index = h_index
        db.commit()
        return len(found)
    finally:
        db.close()


def _edition_k(db, topic_id: str) -> int:
    """The largest max_papers among the topic's active subscriptions (an
    uncapped one counts as 10), and at least 5."""
    caps = [
        cap if cap is not None else _UNCAPPED_K
        for (cap,) in crud.active_subscriptions_query(db)
        .with_entities(models.Subscription.max_papers)
        .filter(models.Subscription.topic_id == topic_id)
    ]
    return max([_MIN_K, *caps])


def _rank_window(
    db, topic_id: str, papers: list[models.Paper], as_of: datetime
) -> tuple[list[str], list[models.Paper]]:
    """(every paper's id best first, the ranked papers without their near
    duplicates), scored as the email scores them. A near duplicate ranks
    right after the paper it folds under, so it never takes a top-K slot."""
    if not papers:
        return [], []
    relevance = crud.relevance_by_paper(db, topic_id, [p.id for p in papers])
    by_arxiv_id = {p.arxiv_id: p for p in papers}
    batch = DigestForEmail(
        generated_at=as_of,
        overview=None,
        papers=[_paper_for_email(p, relevance.get(p.id)) for p in papers],
    )
    ranked = rank_papers([batch], novelty=_novelty())
    ordered: list[str] = []
    for r in ranked:
        ordered.append(by_arxiv_id[r.arxiv_id].id)
        ordered.extend(by_arxiv_id[d.arxiv_id].id for d in r.see_also)
    return ordered, [by_arxiv_id[r.arxiv_id] for r in ranked]


@activity.defn
def rank_edition(plan: EditionPlan) -> EditionRanking:
    """The window's papers best first (relevance, h-index, venue, novelty)
    and the top K with any summary they already have."""
    db = SessionLocal()
    try:
        papers = db.query(models.Paper).filter(models.Paper.id.in_(plan.paper_ids)).all()
        ordered, ranked = _rank_window(db, plan.topic_id, papers, plan.window_end)
        return EditionRanking(
            paper_ids=ordered,
            top=[
                TopPaper(paper_id=p.id, title=p.title, abstract=p.abstract or "", summary=p.summary)
                for p in ranked[: plan.k]
            ],
        )
    finally:
        db.close()


@activity.defn
def get_edition_id(key: EditionKey) -> str | None:
    db = SessionLocal()
    try:
        edition = crud.get_edition(db, key.topic_id, key.slot)
        return edition.id if edition is not None else None
    finally:
        db.close()


@activity.defn
def ensure_edition(key: EditionKey) -> str:
    """The id of the topic's edition for `key.slot`. When prep never wrote
    one (the worker was down, or the topic is newer than prep), builds a
    "fallback" edition from the topic's window now - ranked from what's
    stored, with no LLM, arXiv or h-index call and no overview - so the
    topic is never held back."""
    db = SessionLocal()
    try:
        existing = crud.get_edition(db, key.topic_id, key.slot)
        if existing is not None:
            return existing.id
        window_end = datetime.now(timezone.utc)
        window_start = crud.edition_window_start(db, key.topic_id, window_end)
        papers = crud.window_papers(db, key.topic_id, window_start, window_end)
        ordered, _ = _rank_window(db, key.topic_id, papers, window_end)
        edition = crud.save_edition(
            db,
            topic_id=key.topic_id,
            slot=key.slot,
            window_start=window_start,
            window_end=window_end,
            status="fallback",
            k=_edition_k(db, key.topic_id),
            overview=None,
            ranked_paper_ids=ordered,
        )
        return edition.id
    finally:
        db.close()


@activity.defn
def store_edition(input: StoreEditionInput) -> str:
    """Save the edition (idempotent per topic and slot); returns its id."""
    db = SessionLocal()
    try:
        edition = crud.save_edition(
            db,
            topic_id=input.topic_id,
            slot=input.slot,
            window_start=input.window_start,
            window_end=input.window_end,
            status="ready",
            k=input.k,
            overview=input.overview,
            ranked_paper_ids=input.ranked_paper_ids,
        )
        return edition.id
    finally:
        db.close()


@activity.defn
def send_account_email(input: AccountEmailInput) -> None:
    from app.account_emails import render_account_email

    EmailSender().send(input.to, render_account_email(input.kind, input.url))


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
