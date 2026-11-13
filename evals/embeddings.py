"""Calibrate retrieval thresholds for an embedding model on the live corpus.

    python -m evals.embeddings scale --model M [--similar T]
    python -m evals.embeddings map --from embeddinggemma --to text-embedding-3-small
    python -m evals.embeddings review --model M --at T [--n 20]
    python -m evals.embeddings topics --model M

Cosine scales differ a lot between embedding models (embeddinggemma's
nearest neighbours sit around 0.66; other models' elsewhere), so each
threshold in app.config.Calibration is carried over by *quantile*: a
threshold that sat at the old model's p90 of nearest-neighbour similarity
goes to the new model's p90. `review` then spot-checks the carried-over
"similar earlier paper" floor by eye, and `topics` sets the similar-topic
suggestion floor from labeled pairs (evals/topic_pairs.csv).

Read-only on the database, except that `topics` embeds text through the spend
ledger (llm_usage). Every similarity here is exact: index scans are off, so
the HNSW index can't silently drop neighbours when filtering by model.
Reports go to evals/results/. See docs/rag-plan.md §5.2 and §5.9.
"""
from __future__ import annotations

import argparse
import csv
import json
import math
import random
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.config import Calibration, get_settings
from app.database import SessionLocal
from app.services.embed import Embedder
from app.spend_ledger import DbLedger
from app.topic_query import normalize_query
from evals import metrics

RESULTS = Path(__file__).parent / "results"
TOPIC_PAIRS = Path(__file__).parent / "topic_pairs.csv"
RANDOM_PAIRS = 3000
SEED = 42
PERCENTILES = (0.1, 0.5, 0.9, 0.99)

_IDS_SQL = """
SELECT e.paper_id FROM paper_embeddings e
WHERE e.model = :model AND e.kind = 'abstract' AND e.chunk_ix = 0
ORDER BY e.paper_id
"""

_PAIR_SIMS_SQL = """
SELECT 1 - (a.embedding <=> b.embedding)
FROM unnest(CAST(:xs AS uuid[]), CAST(:ys AS uuid[])) AS p(x, y)
JOIN paper_embeddings a ON a.paper_id = p.x AND a.model = :model AND a.kind = 'abstract' AND a.chunk_ix = 0
JOIN paper_embeddings b ON b.paper_id = p.y AND b.model = :model AND b.kind = 'abstract' AND b.chunk_ix = 0
"""

# Each paper's nearest other paper; with :earlier_linked, only earlier,
# topic-linked candidates (what "similar earlier paper" uses).
_NN_SQL = """
SELECT e.paper_id AS paper_id, n.paper_id AS neighbour_id, n.similarity
FROM paper_embeddings e
JOIN papers ep ON ep.id = e.paper_id
CROSS JOIN LATERAL (
    SELECT o.paper_id, 1 - (o.embedding <=> e.embedding) AS similarity
    FROM paper_embeddings o
    JOIN papers op ON op.id = o.paper_id
    WHERE o.model = e.model AND o.kind = 'abstract' AND o.chunk_ix = 0
      AND o.paper_id <> e.paper_id
      AND (NOT :earlier_linked OR (
            op.published_at < ep.published_at
            AND EXISTS (SELECT 1 FROM topic_paper l WHERE l.paper_id = o.paper_id)))
    ORDER BY o.embedding <=> e.embedding
    LIMIT 1
) n
WHERE e.model = :model AND e.kind = 'abstract' AND e.chunk_ix = 0
  AND (NOT :earlier_linked OR EXISTS (SELECT 1 FROM topic_paper l WHERE l.paper_id = e.paper_id))
"""

_SHARE_TOPIC_SQL = """
SELECT EXISTS (
    SELECT 1 FROM topic_paper a JOIN topic_paper b ON a.topic_id = b.topic_id
    WHERE a.paper_id = CAST(:x AS uuid) AND b.paper_id = CAST(:y AS uuid)
)
"""


def _exact(db: Session) -> None:
    db.execute(text("SET LOCAL enable_indexscan = off"))
    db.execute(text("SET LOCAL enable_bitmapscan = off"))


def _summary(values: list[float]) -> dict:
    out = {f"p{int(q * 100)}": metrics.percentile(values, q) for q in PERCENTILES}
    out["max"] = max(values) if values else None
    out["n"] = len(values)
    return out


def random_pair_sims(db: Session, model: str) -> list[float]:
    ids = [r[0] for r in db.execute(text(_IDS_SQL), {"model": model})]
    if len(ids) < 2:
        return []
    rng = random.Random(SEED)
    pairs = [tuple(rng.sample(ids, 2)) for _ in range(RANDOM_PAIRS)]
    xs, ys = [p[0] for p in pairs], [p[1] for p in pairs]
    return [float(r[0]) for r in db.execute(text(_PAIR_SIMS_SQL), {"xs": xs, "ys": ys, "model": model})]


def nearest_neighbours(db: Session, model: str, *, earlier_linked: bool = False) -> list[tuple[str, str, float]]:
    rows = db.execute(text(_NN_SQL), {"model": model, "earlier_linked": earlier_linked})
    return [(r.paper_id, r.neighbour_id, float(r.similarity)) for r in rows]


def scale(db: Session, model: str, similar: float | None = None) -> dict:
    _exact(db)
    rand = random_pair_sims(db, model)
    nn = nearest_neighbours(db, model)
    linked_ids = {r[0] for r in db.execute(text("SELECT DISTINCT paper_id FROM topic_paper"))}
    linked_nn = [(p, n) for p, n, _ in nn if p in linked_ids and n in linked_ids]
    shared = sum(bool(db.execute(text(_SHARE_TOPIC_SQL), {"x": p, "y": n}).scalar()) for p, n in linked_nn)
    report = {
        "model": model,
        "papers": len(nn),
        "random_pairs": _summary(rand),
        "nearest_neighbour": _summary([s for _, _, s in nn]),
        "neighbours_share_a_topic": {
            "shared": shared, "of": len(linked_nn),
            "rate": round(shared / len(linked_nn), 3) if linked_nn else None,
        },
    }
    if similar is not None:
        earlier = nearest_neighbours(db, model, earlier_linked=True)
        covered = sum(s >= similar for _, _, s in earlier)
        report["similar_paper_coverage"] = {
            "threshold": similar, "covered": covered, "of_linked_papers": len(linked_ids),
            "rate": round(covered / len(linked_ids), 3) if linked_ids else None,
        }
    return report


def propose(old: Calibration, old_rand, old_nn, new_rand, new_nn) -> dict:
    """The new model's thresholds by quantile mapping (see module docstring).
    near_duplicate sits above every observed pair on the old model (no real
    duplicates in the corpus), so it stays above every observed pair on the
    new one: max(new NN max + 0.01, its quantile mapping)."""
    nn_map = lambda v: round(metrics.quantile_map(v, old_nn, new_nn), 3)  # noqa: E731
    return {
        "similar_paper": nn_map(old.similar_paper),
        "related_min": nn_map(old.related_min),
        "novelty_repeat": nn_map(old.novelty_repeat),
        "novelty_unrelated": round(metrics.quantile_map(old.novelty_unrelated, old_rand, new_rand), 3),
        "near_duplicate": round(max(max(new_nn) + 0.01, metrics.quantile_map(old.near_duplicate, old_nn, new_nn)), 3),
    }


def review_pairs(db: Session, model: str, at: float, n: int) -> list[dict]:
    """The n earlier, linked nearest neighbours just at or above `at`."""
    _exact(db)
    pairs = sorted((s, p, q) for p, q, s in nearest_neighbours(db, model, earlier_linked=True) if s >= at)[:n]
    out = []
    for sim, paper_id, neighbour_id in pairs:
        rows = {
            r.id: r for r in db.execute(
                text("SELECT id, arxiv_id, title, abstract FROM papers WHERE id IN (CAST(:a AS uuid), CAST(:b AS uuid))"),
                {"a": paper_id, "b": neighbour_id},
            )
        }
        a, b = rows[paper_id], rows[neighbour_id]
        out.append({
            "similarity": round(sim, 4),
            "paper": {"arxiv_id": a.arxiv_id, "title": a.title, "abstract": (a.abstract or "")[:300]},
            "earlier": {"arxiv_id": b.arxiv_id, "title": b.title, "abstract": (b.abstract or "")[:300]},
        })
    return out


def read_topic_pairs(path: Path = TOPIC_PAIRS) -> list[dict]:
    with path.open(newline="") as f:
        return [row for row in csv.DictReader(f) if not row["a"].startswith("#")]


def _cosine(u: list[float], v: list[float]) -> float:
    dot = sum(x * y for x, y in zip(u, v))
    return dot / (math.sqrt(sum(x * x for x in u)) * math.sqrt(sum(y * y for y in v)))


def topic_pairs(embedder: Embedder, rows: list[dict]) -> dict:
    texts = sorted({normalize_query(r[k]) for r in rows for k in ("a", "b")})
    vectors = dict(zip(texts, embedder.embed([embedder.query_text(t) for t in texts])))
    scored = [
        {**r, "similarity": round(_cosine(vectors[normalize_query(r["a"])], vectors[normalize_query(r["b"])]), 4)}
        for r in rows
    ]
    same = [r["similarity"] for r in scored if r["label"] == "same"]
    different = [r["similarity"] for r in scored if r["label"] == "different"]
    threshold, overlap = metrics.separating_threshold(same, different)
    misclassified = [
        r for r in scored
        if (r["label"] == "same") != (r["similarity"] >= threshold)
    ]
    return {
        "model": embedder.model,
        "topic_suggestion": round(threshold, 3),
        "overlap": overlap,
        "misclassified": misclassified,
        "pairs": sorted(scored, key=lambda r: r["similarity"], reverse=True),
    }


def _write(name: str, report: dict) -> Path:
    RESULTS.mkdir(exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d_%H%M%S")
    path = RESULTS / f"{stamp}_{name}.json"
    path.write_text(json.dumps(report, indent=2, default=str) + "\n")
    return path


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="python -m evals.embeddings", description=__doc__.split("\n")[0])
    sub = parser.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("scale", help="similarity percentiles, topic sharing, coverage")
    p.add_argument("--model", required=True)
    p.add_argument("--similar", type=float, help="report similar-paper coverage at this floor")
    p = sub.add_parser("map", help="propose a calibration for --to by quantile mapping from --from")
    p.add_argument("--from", dest="src", required=True)
    p.add_argument("--to", dest="dst", required=True)
    p = sub.add_parser("review", help="neighbour pairs just above a floor, for a by-eye check")
    p.add_argument("--model", required=True)
    p.add_argument("--at", type=float, required=True)
    p.add_argument("--n", type=int, default=20)
    p = sub.add_parser("topics", help="similar-topic floor from evals/topic_pairs.csv (embeds; costs ~nothing)")
    p.add_argument("--model", required=True)
    args = parser.parse_args(argv)

    settings = get_settings()
    db = SessionLocal()
    try:
        if args.cmd == "scale":
            report = scale(db, args.model, args.similar)
        elif args.cmd == "map":
            old = settings.retrieval_calibration[args.src]
            _exact(db)
            report = {
                "from": args.src, "to": args.dst,
                "proposed": propose(
                    old,
                    random_pair_sims(db, args.src), [s for *_, s in nearest_neighbours(db, args.src)],
                    random_pair_sims(db, args.dst), [s for *_, s in nearest_neighbours(db, args.dst)],
                ),
                "old": old.model_dump(),
            }
        elif args.cmd == "review":
            report = {"model": args.model, "at": args.at, "pairs": review_pairs(db, args.model, args.at, args.n)}
        else:
            embedder = Embedder(settings, ledger=DbLedger())
            if embedder.model != args.model:
                raise SystemExit(f"EMBEDDING_MODEL is {embedder.model!r}; set it to {args.model!r} for this run")
            report = topic_pairs(embedder, read_topic_pairs())
        db.rollback()
    finally:
        db.close()
    path = _write(f"embeddings_{args.cmd}", report)
    print(json.dumps(report if args.cmd != "review" else {"written": str(path)}, indent=2, default=str)[:4000])
    print(f"\nWritten to {path}")


if __name__ == "__main__":
    main()
