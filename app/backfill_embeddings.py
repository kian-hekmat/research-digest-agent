"""One-off: embed every paper that has no embedding for the configured model,
then recompute every embedded paper's retrieval signals (novelty, near
duplicates, similar earlier paper).

`python -m app.backfill_embeddings` (inside the worker container:
`docker-compose exec worker python -m app.backfill_embeddings`).

Digest runs embed papers as they're ingested; this covers everything ingested
before retrieval existed, and is how a new EMBEDDING_MODEL gets backfilled
side by side before switching to it. Safe to re-run: it skips papers that
already have an embedding, and commits per batch, so an interrupted run keeps
what it finished.
"""
from __future__ import annotations

import logging

from app.config import get_settings
from app.database import SessionLocal
from app.services.embed import Embedder
from app.services.retrieval import compute_signals, embed_missing

logger = logging.getLogger(__name__)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    settings = get_settings()
    if not settings.embeddings_enabled:
        raise SystemExit(
            "Embeddings are off - they need SUMMARY_BACKEND=ollama and RAG_ENABLED=true."
        )
    db = SessionLocal()
    try:
        written = embed_missing(db, Embedder(settings), batch_size=settings.embedding_batch_size)
        updated = compute_signals(
            db,
            None,
            model=settings.embedding_model,
            near_duplicate_similarity=settings.rag_near_duplicate_similarity,
            novelty_window_days=settings.rag_novelty_window_days,
            similar_paper_similarity=settings.rag_similar_paper_similarity,
        )
    finally:
        db.close()
    logger.info(
        "Done: embedded %d papers with %s, recomputed signals for %d",
        written, settings.embedding_model, updated,
    )


if __name__ == "__main__":
    main()
