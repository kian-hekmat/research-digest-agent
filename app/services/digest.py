"""Digest orchestration: fetch -> upsert & link -> summarize -> finalize.

Each step is a plain function call so Phase 3 can lift them into Temporal
Activities (each with its own retry policy) and `run_digest` becomes the
Workflow. Nothing here imports FastAPI.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from app import crud, models
from app.services.arxiv import search_arxiv
from app.services.summarize import Summarizer

logger = logging.getLogger(__name__)


def run_digest(
    db: Session,
    digest_id: str,
    *,
    fetcher=search_arxiv,
    summarizer: Summarizer | None = None,
) -> models.Digest:
    """Populate a pending digest and flip it to `completed` / `failed`.

    - A failed arXiv fetch -> `failed`, watermark untouched.
    - No new papers -> `completed`, empty, no overview.
    - Some per-paper summaries fail -> still `completed`; those papers are
      attached without a summary and the count is recorded in `error`.
    """
    digest = crud.get_digest(db, digest_id)
    if digest is None:
        raise ValueError(f"Digest {digest_id} not found")

    topic = digest.topic
    summarizer = summarizer or Summarizer()
    run_started = datetime.now(timezone.utc)

    # --- 1. fetch ---------------------------------------------------------
    try:
        results = fetcher(topic.query, since=topic.last_checked_at)
    except Exception as exc:
        logger.exception("arXiv fetch failed for topic %s", topic.id)
        return crud.finalize_digest(
            db,
            digest,
            status=models.DigestStatus.failed,
            error=f"arXiv fetch failed: {exc}",
        )

    # --- 2. upsert papers, link to topic and to this digest --------------
    papers = [
        crud.get_or_create_paper(
            db,
            arxiv_id=r.arxiv_id,
            title=r.title,
            abstract=r.abstract,
            published_at=r.published_at,
            topic=topic,
        )
        for r in results
    ]
    for paper in papers:
        crud.link_paper_to_digest(digest, paper)
    db.commit()  # persist ingestion before the slow summarization phase

    # --- 3. summarize papers that don't have a summary yet ---------------
    summary_failures = 0
    for paper in papers:
        if paper.summary:
            continue
        try:
            paper.summary = summarizer.summarize_paper(
                paper.title, paper.abstract or ""
            )
        except Exception:
            summary_failures += 1
            logger.exception("summary failed for paper %s", paper.arxiv_id)
    db.commit()

    # --- 4. synthesized overview across the batch -----------------------
    overview = None
    summarized = [p.summary for p in papers if p.summary]
    if summarized:
        try:
            overview = summarizer.write_overview(topic.name, summarized)
        except Exception:
            logger.exception("overview failed for digest %s", digest.id)

    # --- 5. finalize --------------------------------------------------
    error = (
        f"{summary_failures} of {len(papers)} paper summaries failed"
        if summary_failures
        else None
    )
    crud.finalize_digest(
        db,
        digest,
        status=models.DigestStatus.completed,
        overview=overview,
        error=error,
    )
    crud.advance_topic_watermark(db, topic, run_started)
    return digest
