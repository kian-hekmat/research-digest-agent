"""Paper embeddings in pgvector: writing them and finding nearest neighbours.

The only module that writes pgvector SQL. Callers own the session; functions
here commit only where noted.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import text
from sqlalchemy.orm import Session

from app import crud, models
from app.services.embed import Embedder, document_text, query_text
from app.services.fulltext import Chunk, chunk_sections, fetch_html, parse_sections

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


@dataclass(frozen=True)
class Anchor:
    paper_id: str
    arxiv_id: str
    title: str
    abstract: str
    rating: int  # reference_relevance for the topic
    similarity: float | None  # to the paper being rated; None without embeddings


# Rating bands anchors are drawn from, so examples span the scale instead of
# all being the nearest (usually highly rated) papers.
_BANDS = ((8, 10), (4, 7), (1, 3))

_ANCHORS_SQL = """
SELECT p.id, p.arxiv_id, p.title, p.abstract, tp.reference_relevance AS rating,
       {similarity} AS similarity
FROM topic_paper tp
JOIN papers p ON p.id = tp.paper_id
{embedding_join}
WHERE tp.topic_id = :topic_id AND tp.reference_relevance IS NOT NULL
  AND p.id <> ALL(CAST(:exclude AS uuid[])) AND p.abstract IS NOT NULL
ORDER BY {order}
"""


def rating_anchors(
    db: Session,
    topic_id: str,
    paper_id: str,
    *,
    model: str,
    per_band: int = 2,
    exclude_paper_ids: tuple[str, ...] = (),
) -> list[Anchor]:
    """Reference-rated papers of this topic to show as calibration examples
    when rating `paper_id`: up to `per_band` from each rating band (8-10,
    4-7, 1-3), the most similar to the paper first. Highest rating first in
    the result. Never the paper itself or anything in `exclude_paper_ids`.
    Without an embedding for the paper, falls back to the newest labeled
    papers per band."""
    has_embedding = db.execute(
        text(
            "SELECT 1 FROM paper_embeddings WHERE paper_id = :pid AND model = :model "
            "AND kind = :kind AND chunk_ix = 0"
        ),
        {"pid": paper_id, "model": model, "kind": ABSTRACT},
    ).first()
    if has_embedding:
        sql = _ANCHORS_SQL.format(
            similarity="1 - (e.embedding <=> q.embedding)",
            embedding_join=(
                "JOIN paper_embeddings e ON e.paper_id = p.id AND e.model = :model "
                "AND e.kind = :kind AND e.chunk_ix = 0 "
                "JOIN paper_embeddings q ON q.paper_id = :paper_id AND q.model = :model "
                "AND q.kind = :kind AND q.chunk_ix = 0"
            ),
            order="e.embedding <=> q.embedding",
        )
    else:
        sql = _ANCHORS_SQL.format(
            similarity="NULL", embedding_join="", order="p.published_at DESC NULLS LAST, p.arxiv_id"
        )
    rows = db.execute(
        text(sql),
        {
            "topic_id": topic_id,
            "paper_id": paper_id,
            "model": model,
            "kind": ABSTRACT,
            # str(): callers may hold uuid.UUID or str ids; the cast takes text.
            "exclude": [str(x) for x in (paper_id, *exclude_paper_ids)],
        },
    ).all()
    picked: list[Anchor] = []
    for low, high in _BANDS:
        band = [r for r in rows if low <= r.rating <= high][:per_band]
        picked += [
            Anchor(
                paper_id=r.id, arxiv_id=r.arxiv_id, title=r.title, abstract=r.abstract,
                rating=r.rating, similarity=None if r.similarity is None else float(r.similarity),
            )
            for r in band
        ]
    return sorted(picked, key=lambda a: -a.rating)


# ---------- full text (phase E) ----------
CHUNK = "chunk"

# What the summarizer most often lacks from an abstract. Each query pulls
# the paper's best-matching chunk. (An "experimental setup" query was tried
# and dropped: it mostly surfaced hardware and hyperparameter trivia.)
FULLTEXT_QUERIES = (
    "main quantitative results: accuracy, error, speedup, and comparison with baselines",
    "the proposed method, model or algorithm and how it works",
    "limitations, assumptions and failure cases",
)


def store_chunks(db: Session, embedder: Embedder, paper: models.Paper, chunks: list[Chunk]) -> int:
    """Embed and store a paper's full-text chunks, replacing any it had for
    this model. Does not commit. Returns how many were stored."""
    db.query(models.PaperEmbedding).filter(
        models.PaperEmbedding.paper_id == paper.id,
        models.PaperEmbedding.model == embedder.model,
        models.PaperEmbedding.kind == CHUNK,
    ).delete(synchronize_session=False)
    if not chunks:
        return 0
    texts = [document_text(f"{paper.title} - {c.section}", c.text) for c in chunks]
    vectors = embedder.embed(texts)
    for i, (chunk, vector) in enumerate(zip(chunks, vectors)):
        db.add(
            models.PaperEmbedding(
                paper_id=paper.id,
                model=embedder.model,
                kind=CHUNK,
                chunk_ix=i,
                content=f"[{chunk.section}] {chunk.text}" if chunk.section else chunk.text,
                embedding=vector,
            )
        )
    return len(chunks)


_query_vectors: dict[str, list[list[float]]] = {}


def _fulltext_query_vectors(embedder: Embedder) -> list[list[float]]:
    """The fixed queries' embeddings, computed once per process and model."""
    if embedder.model not in _query_vectors:
        _query_vectors[embedder.model] = embedder.embed([query_text(q) for q in FULLTEXT_QUERIES])
    return _query_vectors[embedder.model]


_BEST_CHUNK_SQL = """
SELECT chunk_ix, content FROM paper_embeddings
WHERE paper_id = :paper_id AND model = :model AND kind = :kind
ORDER BY embedding <=> CAST(:query AS vector)
LIMIT 1
"""


def fulltext_excerpts(
    db: Session, embedder: Embedder, paper_id: str, *, max_excerpts: int = 4
) -> list[str]:
    """The paper's chunks that best answer FULLTEXT_QUERIES (one per query,
    de-duplicated), in the paper's own order. Empty when it has none."""
    found: dict[int, str] = {}
    for vector in _fulltext_query_vectors(embedder):
        row = db.execute(
            text(_BEST_CHUNK_SQL),
            {
                "paper_id": paper_id,
                "model": embedder.model,
                "kind": CHUNK,
                "query": "[" + ",".join(repr(float(x)) for x in vector) + "]",
            },
        ).first()
        if row is not None:
            found.setdefault(row.chunk_ix, row.content)
        if len(found) >= max_excerpts:
            break
    return [_simplify_math(found[i]) for i in sorted(found)]


_INLINE_MATH = re.compile(r"\$[^$]*\$")
_LONG_FORMULA_CHARS = 25


def _simplify_math(text_: str) -> str:
    """Long inline formulas become "[formula]" so they don't leak into
    summaries; short ones (`$n=50000$`) stay readable. Formulas are paired
    left to right ($...$), so prose between two formulas is never touched."""
    return _INLINE_MATH.sub(
        lambda m: "[formula]" if len(m.group(0)) - 2 >= _LONG_FORMULA_CHARS else m.group(0), text_
    )


def index_fulltext(
    db: Session, embedder: Embedder, paper: models.Paper, fetch=fetch_html
) -> str:
    """Fetch, parse, chunk and store one paper's full text, and record the
    outcome in paper.fulltext_status ("ok" / "unavailable" / "empty" /
    "error"). Commits. A fetch error is recorded, logged by the caller, and
    never retried automatically - the summary just goes without full text."""
    status = "error"
    try:
        html = fetch(paper.arxiv_id)
        if html is None:
            status = "unavailable"
        else:
            chunks = chunk_sections(parse_sections(html))
            status = "ok" if store_chunks(db, embedder, paper, chunks) else "empty"
    finally:
        paper.fulltext_status = status
        paper.fulltext_checked_at = datetime.now(timezone.utc)
        db.commit()
    return status
