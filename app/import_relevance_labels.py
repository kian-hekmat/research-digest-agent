"""One-off: load reference relevance labels into topic_paper.

`python -m app.import_relevance_labels [evals/labels.csv]`

Reads a CSV with `topic`, `arxiv_id`, `label` (1-10) and `labeler` columns -
the format `python -m evals.rag sample/label` writes - and stores each label
as that topic-paper pair's reference_relevance. These are the few-shot
anchors new relevance ratings are calibrated against (see
app.services.retrieval.rating_anchors). Rows whose paper isn't linked to the
topic, or with no valid label, are skipped and counted. Safe to re-run: it
overwrites the labels it finds.
"""
from __future__ import annotations

import csv
import logging
import sys
from pathlib import Path

from sqlalchemy import update

from app import models
from app.database import SessionLocal

logger = logging.getLogger(__name__)
DEFAULT_PATH = Path("evals/labels.csv")


def import_labels(db, rows: list[dict]) -> tuple[int, int]:
    """Store each valid row's label; returns (stored, skipped). Commits."""
    topics = {t.name: t.id for t in db.query(models.Topic)}
    papers = {p.arxiv_id: p.id for p in db.query(models.Paper.arxiv_id, models.Paper.id)}
    table = models.topic_paper_association
    stored = skipped = 0
    for row in rows:
        raw = (row.get("label") or "").strip()
        topic_id, paper_id = topics.get(row.get("topic", "")), papers.get(row.get("arxiv_id", ""))
        if not (raw.isdigit() and 1 <= int(raw) <= 10 and topic_id and paper_id):
            skipped += 1
            continue
        result = db.execute(
            update(table)
            .where(table.c.topic_id == topic_id, table.c.paper_id == paper_id)
            .values(reference_relevance=int(raw), reference_labeler=row.get("labeler") or "unknown")
        )
        if result.rowcount:
            stored += 1
        else:
            skipped += 1
    db.commit()
    return stored, skipped


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    path = Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_PATH
    with path.open(newline="") as f:
        rows = list(csv.DictReader(f))
    db = SessionLocal()
    try:
        stored, skipped = import_labels(db, rows)
    finally:
        db.close()
    logger.info("Stored %d reference labels from %s, skipped %d", stored, path, skipped)


if __name__ == "__main__":
    main()
