"""One-off: summarize papers that are waiting to be emailed but have no summary.

`python -m app.backfill_summaries` (inside the worker container:
`docker-compose exec worker python -m app.backfill_summaries`).

Digest runs only summarize papers as they're ingested, and a paper is never
re-ingested for the same topic - so papers fetched while summarization was off
keep no summary forever. This fills in exactly the ones that still matter: in
completed digests generated after an active subscription's last send (i.e.
not yet emailed), plus a missing overview for each such digest. Already-
emailed history is left alone. Safe to re-run; it skips anything already done.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

from sqlalchemy.orm import Session

from app import crud, models
from app.config import get_settings
from app.database import SessionLocal
from app.services.summarize import Summarizer

logger = logging.getLogger(__name__)


@dataclass
class BackfillResult:
    summaries_written: int = 0
    overviews_written: int = 0
    failures: int = 0


def _pending_digests(db: Session) -> list[models.Digest]:
    subs = db.query(models.Subscription).filter(models.Subscription.active.is_(True)).all()
    seen: dict[str, models.Digest] = {}
    for sub in subs:
        for digest in crud.list_completed_digests_since(db, sub.topic_id, sub.last_sent_at):
            seen.setdefault(digest.id, digest)
    return sorted(seen.values(), key=lambda d: d.generated_at)


def backfill(db: Session, summarizer: Summarizer) -> BackfillResult:
    """One paper's failure (server hiccup, bad output) is logged and skipped,
    not fatal - a re-run picks it up."""
    result = BackfillResult()
    for digest in _pending_digests(db):
        for paper in digest.papers:
            if paper.summary:
                continue
            try:
                paper.summary = summarizer.summarize_paper(paper.title, paper.abstract or "")
            except Exception:
                logger.exception("Summary failed for %s", paper.arxiv_id)
                result.failures += 1
                continue
            db.commit()
            result.summaries_written += 1
            logger.info("Summarized %s", paper.arxiv_id)

        summaries = [p.summary for p in digest.papers if p.summary]
        if digest.overview is None and summaries:
            try:
                digest.overview = summarizer.write_overview(digest.topic.name, summaries)
            except Exception:
                logger.exception("Overview failed for digest %s", digest.id)
                result.failures += 1
                continue
            db.commit()
            result.overviews_written += 1
    return result


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    if not get_settings().summaries_enabled:
        raise SystemExit("Summarization is off - set SUMMARY_BACKEND=ollama first.")
    db = SessionLocal()
    try:
        result = backfill(db, Summarizer())
    finally:
        db.close()
    logger.info(
        "Done: %d summaries, %d overviews written, %d failures%s",
        result.summaries_written,
        result.overviews_written,
        result.failures,
        " (re-run to retry them)" if result.failures else "",
    )


if __name__ == "__main__":
    main()
