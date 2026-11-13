"""One-off: embed every paper and topic that has no embedding for the
configured model, then recompute every embedded paper's retrieval signals
(novelty, near duplicates, similar earlier paper).

`python -m app.backfill_embeddings [--embed-only | --signals-only]` (inside the
worker container: `docker-compose exec worker python -m app.backfill_embeddings`).

Digest runs embed papers as they're ingested; this covers everything ingested
before retrieval existed, and is how a new EMBEDDING_MODEL is switched to:

1. `--embed-only` with the new model (e.g. `-e EMBEDDING_BACKEND=openai
   -e EMBEDDING_MODEL=text-embedding-3-small`) writes its embeddings side by
   side and touches no signal. Signals are stored per paper, not per model,
   so recomputing them now would switch live ranking to the new model's
   similarities while it still uses the old model's thresholds.
2. At cutover, with the new model configured and calibrated,
   `--signals-only` recomputes every paper's signals.

Safe to re-run: it skips anything already embedded, and commits per batch,
so an interrupted run keeps what it finished.
"""
from __future__ import annotations

import argparse
import logging

from app.config import get_settings
from app.database import SessionLocal
from app.services.embed import Embedder
from app.services.retrieval import compute_signals, embed_missing, embed_missing_topics
from app.spend_ledger import DbLedger

logger = logging.getLogger(__name__)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        prog="python -m app.backfill_embeddings", description=__doc__.split("\n")[0]
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--embed-only", action="store_true", help="embed papers and topics; leave signals alone")
    mode.add_argument("--signals-only", action="store_true", help="recompute signals; embed nothing")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(message)s")
    settings = get_settings()
    if not settings.embeddings_enabled:
        raise SystemExit(
            "Embeddings are off - they need SUMMARY_BACKEND set (not none) and RAG_ENABLED=true."
        )
    # Before anything is written: signals need this model's thresholds.
    calibration = None if args.embed_only else settings.calibration
    db = SessionLocal()
    try:
        papers = topics = updated = 0
        if not args.signals_only:
            embedder = Embedder(settings, ledger=DbLedger())
            papers = embed_missing(db, embedder, batch_size=settings.embedding_batch_size)
            topics = embed_missing_topics(db, embedder)
        if calibration is not None:
            updated = compute_signals(
                db,
                None,
                model=settings.embedding_model,
                near_duplicate_similarity=calibration.near_duplicate,
                novelty_window_days=settings.rag_novelty_window_days,
                similar_paper_similarity=calibration.similar_paper,
            )
    finally:
        db.close()
    logger.info(
        "Done with %s: embedded %d papers and %d topics, recomputed signals for %d",
        settings.embedding_model, papers, topics, updated,
    )


if __name__ == "__main__":
    main()
