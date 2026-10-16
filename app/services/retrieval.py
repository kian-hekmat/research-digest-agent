"""Paper embeddings in pgvector: writing them and finding nearest neighbours.

The only module that writes pgvector SQL. Callers own the session; functions
here commit only where noted.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import text
from sqlalchemy.orm import Session

from app import models
from app.services.embed import Embedder, document_text

ABSTRACT = "abstract"


@dataclass(frozen=True)
class Neighbour:
    paper_id: str
    arxiv_id: str
    title: str
    summary: str | None
    published_at: datetime | None
    similarity: float  # cosine similarity, 1.0 = same direction


def papers_missing_embedding(
    db: Session, model: str, paper_ids: list[str] | None = None
) -> list[models.Paper]:
    """Papers with an abstract but no `model` abstract embedding yet -
    restricted to `paper_ids` when given."""
    has_embedding = (
        db.query(models.PaperEmbedding.paper_id)
        .filter(
            models.PaperEmbedding.model == model,
            models.PaperEmbedding.kind == ABSTRACT,
        )
    )
    q = db.query(models.Paper).filter(
        models.Paper.abstract.isnot(None),
        models.Paper.abstract != "",
        models.Paper.id.notin_(has_embedding),
    )
    if paper_ids is not None:
        if not paper_ids:
            return []
        q = q.filter(models.Paper.id.in_(paper_ids))
    return q.order_by(models.Paper.created_at, models.Paper.id).all()


def embed_missing(
    db: Session, embedder: Embedder, paper_ids: list[str] | None = None, batch_size: int = 32
) -> int:
    """Embed every paper that lacks an embedding (or just those in
    `paper_ids`), committing after each batch so a failure part-way keeps the
    work already done. Idempotent. Returns how many were written; 0 when
    embeddings are off."""
    if not embedder.enabled:
        return 0
    todo = papers_missing_embedding(db, embedder.model, paper_ids)
    written = 0
    for start in range(0, len(todo), batch_size):
        batch = todo[start : start + batch_size]
        texts = [document_text(p.title, p.abstract) for p in batch]
        vectors = embedder.embed(texts)
        for paper, content, vector in zip(batch, texts, vectors):
            db.add(
                models.PaperEmbedding(
                    paper_id=paper.id,
                    model=embedder.model,
                    kind=ABSTRACT,
                    chunk_ix=0,
                    content=content,
                    embedding=vector,
                )
            )
        db.commit()
        written += len(batch)
    return written


_NEIGHBOURS_SQL = """
SELECT p.id, p.arxiv_id, p.title, p.summary, p.published_at,
       1 - (e.embedding <=> q.embedding) AS similarity
FROM paper_embeddings q
JOIN papers qp ON qp.id = q.paper_id
JOIN paper_embeddings e
  ON e.model = q.model AND e.kind = q.kind AND e.chunk_ix = 0 AND e.paper_id <> q.paper_id
JOIN papers p ON p.id = e.paper_id
{topic_join}
WHERE q.paper_id = :paper_id AND q.model = :model AND q.kind = :kind AND q.chunk_ix = 0
  AND 1 - (e.embedding <=> q.embedding) >= :min_similarity
  {before_clause}
ORDER BY e.embedding <=> q.embedding
LIMIT :k
"""


def neighbours(
    db: Session,
    paper_id: str,
    *,
    model: str,
    k: int = 4,
    min_similarity: float = 0.0,
    published_before: bool = False,
    topic_id: str | None = None,
) -> list[Neighbour]:
    """The `k` papers most similar to `paper_id` by abstract embedding, most
    similar first, never the paper itself.

    - `min_similarity`: drop anything less similar (cosine).
    - `published_before`: only papers submitted strictly before this one, so
      "builds on" is at least chronologically possible. A paper with no
      published_at matches nothing under this filter.
    - `topic_id`: only papers linked to that topic.

    Empty when the paper has no embedding for `model`."""
    sql = _NEIGHBOURS_SQL.format(
        topic_join=(
            "JOIN topic_paper tp ON tp.paper_id = p.id AND tp.topic_id = :topic_id"
            if topic_id
            else ""
        ),
        before_clause="AND p.published_at < qp.published_at" if published_before else "",
    )
    params = {
        "paper_id": paper_id,
        "model": model,
        "kind": ABSTRACT,
        "min_similarity": min_similarity,
        "k": k,
    }
    if topic_id:
        params["topic_id"] = topic_id
    rows = db.execute(text(sql), params).all()
    return [
        Neighbour(
            paper_id=r.id,
            arxiv_id=r.arxiv_id,
            title=r.title,
            summary=r.summary,
            published_at=r.published_at,
            similarity=float(r.similarity),
        )
        for r in rows
    ]
