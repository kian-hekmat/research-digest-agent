"""Paper embeddings in pgvector: writing them and finding nearest neighbours.

The only module that writes pgvector SQL. Callers own the session; functions
here commit only where noted.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import text
from sqlalchemy.orm import Session

from app import crud, models
from app.services.embed import Embedder, document_text

ABSTRACT = "abstract"


@dataclass(frozen=True)
class Neighbour:
    paper_id: str
    arxiv_id: str
    title: str
    abstract: str | None
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
SELECT p.id, p.arxiv_id, p.title, p.abstract, p.summary, p.published_at,
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
  {window_clause}
  {linked_clause}
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
    within_days: int | None = None,
    topic_id: str | None = None,
    linked_only: bool = False,
) -> list[Neighbour]:
    """The `k` papers most similar to `paper_id` by abstract embedding, most
    similar first, never the paper itself.

    - `min_similarity`: drop anything less similar (cosine).
    - `published_before`: only papers submitted strictly before this one, so
      "builds on" is at least chronologically possible. A paper with no
      published_at matches nothing under this filter.
    - `within_days`: only papers submitted at most this many days before it.
    - `topic_id`: only papers linked to that topic.
    - `linked_only`: only papers linked to at least one topic - leaves out
      papers that were unlinked as off-topic but kept for digest history.

    Empty when the paper has no embedding for `model`."""
    sql = _NEIGHBOURS_SQL.format(
        topic_join=(
            "JOIN topic_paper tp ON tp.paper_id = p.id AND tp.topic_id = :topic_id"
            if topic_id
            else ""
        ),
        before_clause="AND p.published_at < qp.published_at" if published_before else "",
        window_clause=(
            "AND p.published_at >= qp.published_at - make_interval(days => :within_days)"
            if within_days is not None
            else ""
        ),
        linked_clause=(
            "AND EXISTS (SELECT 1 FROM topic_paper l WHERE l.paper_id = p.id)" if linked_only else ""
        ),
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
    if within_days is not None:
        params["within_days"] = within_days
    rows = db.execute(text(sql), params).all()
    return [
        Neighbour(
            paper_id=r.id,
            arxiv_id=r.arxiv_id,
            title=r.title,
            abstract=r.abstract,
            summary=r.summary,
            published_at=r.published_at,
            similarity=float(r.similarity),
        )
        for r in rows
    ]


def related_context(
    db: Session, paper_id: str, *, model: str, k: int, min_similarity: float
) -> list[Neighbour]:
    """Earlier, topic-linked papers close enough to offer the summarizer as
    related work."""
    return neighbours(
        db,
        paper_id,
        model=model,
        k=k,
        min_similarity=min_similarity,
        published_before=True,
        linked_only=True,
    )


_FAR_FUTURE = datetime.max.replace(tzinfo=timezone.utc)


def _order_key(published_at: datetime | None, arxiv_id: str) -> tuple[datetime, str]:
    return (published_at or _FAR_FUTURE, arxiv_id)


def compute_signals(
    db: Session,
    paper_ids: list[str] | None,
    *,
    model: str,
    near_duplicate_similarity: float,
    novelty_window_days: int,
    similar_paper_similarity: float = 0.75,
) -> int:
    """Set `prior_similarity` and `near_duplicate_of_id` for the given papers
    (every embedded paper when None) from their nearest topic-linked
    neighbours, and commit. Recomputed from scratch each time, so safe to
    re-run. Returns how many papers were updated.

    - prior_similarity: similarity to the closest paper published before this
      one within the window; NULL when there is none.
    - near_duplicate_of: the closest paper at or above the near-duplicate
      threshold that comes *first* (by submission date, then arXiv id), so of
      two near-identical papers only the later one points at the other.
    - similar paper: the closest earlier, topic-linked paper at or above
      `similar_paper_similarity`, recorded in paper_related - unless the paper
      has an LLM related-work note, whose checked citations take its place."""
    q = db.query(models.Paper).join(
        models.PaperEmbedding,
        (models.PaperEmbedding.paper_id == models.Paper.id)
        & (models.PaperEmbedding.model == model)
        & (models.PaperEmbedding.kind == ABSTRACT),
    )
    if paper_ids is not None:
        if not paper_ids:
            return 0
        q = q.filter(models.Paper.id.in_(paper_ids))
    papers = q.all()
    for paper in papers:
        prior = neighbours(
            db, paper.id, model=model, k=1, published_before=True,
            within_days=novelty_window_days, linked_only=True,
        )
        paper.prior_similarity = prior[0].similarity if prior else None
        close = neighbours(
            db, paper.id, model=model, k=3, min_similarity=near_duplicate_similarity,
            linked_only=True,
        )
        mine = _order_key(paper.published_at, paper.arxiv_id)
        earlier = [n for n in close if _order_key(n.published_at, n.arxiv_id) < mine]
        paper.near_duplicate_of_id = earlier[0].paper_id if earlier else None
        if paper.related_note is None:
            similar = related_context(
                db, paper.id, model=model, k=1, min_similarity=similar_paper_similarity
            )
            crud.set_paper_related(db, paper.id, [(n.paper_id, n.similarity) for n in similar])
    db.commit()
    return len(papers)
