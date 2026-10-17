"""Offline eval of the ranking (app.services.ranking) against reference labels.

    venv/bin/python -m evals.ranking [--report evals/results/<run>.json]

For each topic, scores its reference-labeled papers the way an email would
(one batch, signals imputed from the batch median) under several relevance
schemes. It then measures how well each ordering puts the papers the
reference labels call central first. No model calls: relevance comes from a
saved `evals.rag run` report (by default the combined summarize+rate call,
which is what production uses for new papers), and the other signals
(h-index, novelty, venue) come from the database.

Caveat: the reference labels measure topical centrality only. h-index and
novelty are meant to add something those labels don't capture, so a scheme
that leans on them can only lose on these metrics. The question this answers
is how much topical precision each scheme gives up, not which one makes a
better email overall.
"""
from __future__ import annotations

import argparse
import glob
import json
import random
import statistics
from dataclasses import replace
from datetime import datetime
from pathlib import Path

from sqlalchemy import text

from app.database import SessionLocal
from app.services.email import PaperForEmail
from app.services.ranking import GATED, GRADED, score_papers, venue_score
from evals import metrics

RESULTS_DIR = Path(__file__).resolve().parent / "results"
OFF_TOPIC_MAX = 3  # reference label <= this is off topic
KS = (5, 10)
SCHEMES = ("graded", "gated", "signals only", "relevance only")


def _latest_report() -> Path:
    candidates = [p for p in sorted(glob.glob(str(RESULTS_DIR / "*_baseline.json")))
                  if json.load(open(p)).get("summaries", {}).get("n", 0) >= 40]
    if not candidates:
        raise SystemExit("No baseline report with summaries for every item - run `evals.rag run --summaries 47`.")
    return Path(candidates[-1])


def load(report: Path, source: str) -> dict[str, list[tuple[PaperForEmail, int]]]:
    """topic -> [(paper with its signals, reference label)]."""
    r = json.load(open(report))
    if source == "combined":
        llm = {(s["topic"], s["arxiv_id"]): s["relevance"] for s in r["summaries"]["outputs"]}
    else:
        llm = {(i["topic_name"], i["arxiv_id"]): i["ratings"][0] for i in r["per_item"]}
    db = SessionLocal()
    try:
        rows = db.execute(text("""
            SELECT t.name, p.arxiv_id, p.title, p.published_at, p.max_author_h_index,
                   p.prior_similarity, p.comment, p.journal_ref, tp.reference_relevance
            FROM topic_paper tp JOIN topics t ON t.id = tp.topic_id JOIN papers p ON p.id = tp.paper_id
            WHERE tp.reference_relevance IS NOT NULL""")).all()
    finally:
        db.close()
    out: dict[str, list] = {}
    for row in rows:
        paper = PaperForEmail(
            title=row.title, summary=None, arxiv_id=row.arxiv_id, published_at=row.published_at,
            relevance=llm.get((row.name, row.arxiv_id)), max_author_h_index=row.max_author_h_index,
            venue_score=venue_score(row.comment, row.journal_ref), prior_similarity=row.prior_similarity,
        )
        out.setdefault(row.name, []).append((paper, row.reference_relevance))
    return out


def order(items: list[tuple[PaperForEmail, int]], scheme: str) -> list[tuple[PaperForEmail, int]]:
    papers = [p for p, _ in items]
    if scheme == "graded":
        scores = score_papers(papers, GRADED)
    elif scheme == "gated":
        scores = score_papers(papers, GATED)
    elif scheme == "signals only":  # no relevance at all
        scores = score_papers([replace(p, relevance=None) for p in papers], GRADED)
    elif scheme == "relevance only":
        scores = [float(p.relevance) if p.relevance is not None else 0.0 for p in papers]
    else:
        raise ValueError(scheme)
    newest = datetime.min
    ranked = sorted(zip(items, scores),
                    key=lambda x: (x[1], x[0][0].published_at.replace(tzinfo=None) if x[0][0].published_at else newest),
                    reverse=True)
    return [item for item, _ in ranked]


def evaluate(items, scheme) -> dict:
    ranked = order(items, scheme)
    labels = [ref for _, ref in ranked]
    papers = [p for p, _ in items]
    scores_rank = {p.arxiv_id: len(ranked) - i for i, (p, _) in enumerate(ranked)}
    out = {"spearman": metrics.spearman([scores_rank[p.arxiv_id] for p in papers], [r for _, r in items])}
    for k in KS:
        out[f"ndcg@{k}"] = metrics.ndcg_at_k([r - 1 for r in labels], k)
        out[f"off_topic_in_top{k}"] = sum(r <= OFF_TOPIC_MAX for r in labels[:k])
    return out


def bootstrap_diff(items, a: str, b: str, metric: str, n: int = 2000, seed: int = 0):
    """95% interval of metric(b) - metric(a), resampling papers within the topic."""
    rng = random.Random(seed)
    diffs = []
    for _ in range(n):
        sample = [items[rng.randrange(len(items))] for _ in items]
        # Keep resampled duplicates distinct so ranks are well defined.
        sample = [(replace(p, arxiv_id=f"{p.arxiv_id}#{i}"), r) for i, (p, r) in enumerate(sample)]
        va, vb = evaluate(sample, a)[metric], evaluate(sample, b)[metric]
        if va is not None and vb is not None:
            diffs.append(vb - va)
    diffs.sort()
    return (round(diffs[int(0.025 * len(diffs))], 3), round(diffs[int(0.975 * len(diffs)) - 1], 3)) if diffs else None


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(prog="python -m evals.ranking", description=__doc__.split("\n")[0])
    ap.add_argument("--report", type=Path, default=None)
    ap.add_argument("--relevance", choices=("combined", "rate-only"), default="combined")
    args = ap.parse_args(argv)
    report = args.report or _latest_report()
    data = load(report, args.relevance)
    result = {"report": str(report.name), "relevance_source": args.relevance, "topics": {}}
    for topic, items in sorted(data.items()):
        result["topics"][topic] = {
            "n": len(items),
            "on_topic": sum(r > OFF_TOPIC_MAX for _, r in items),
            "schemes": {s: evaluate(items, s) for s in SCHEMES},
            "gated_minus_graded_ndcg@10_95ci": bootstrap_diff(items, "graded", "gated", "ndcg@10"),
        }
    means = {}
    for s in SCHEMES:
        vals = [t["schemes"][s] for t in result["topics"].values()]
        means[s] = {m: round(statistics.fmean([v[m] for v in vals if v[m] is not None]), 3) for m in vals[0]}
    result["mean_over_topics"] = means

    print(f"Ranking vs reference labels ({report.name}, relevance from the {args.relevance} call)\n")
    header = f"{'scheme':<16}" + "".join(f"{m:>19}" for m in means["graded"])
    for topic, t in result["topics"].items():
        print(f"{topic} (n={t['n']}, {t['on_topic']} not off-topic)")
        print("  " + header)
        for s in SCHEMES:
            v = t["schemes"][s]
            print(f"  {s:<16}" + "".join(f"{(round(x, 3) if isinstance(x, float) else x):>19}" for x in v.values()))
        print(f"  gated - graded NDCG@10, 95% bootstrap interval: {t['gated_minus_graded_ndcg@10_95ci']}\n")
    print("Mean over topics")
    print("  " + header)
    for s in SCHEMES:
        print(f"  {s:<16}" + "".join(f"{x:>19}" for x in means[s].values()))
    out = RESULTS_DIR / f"{datetime.now():%Y-%m-%d_%H%M}_ranking_{args.relevance}.json"
    out.write_text(json.dumps(result, indent=2, default=str) + "\n")
    print(f"\nFull report: {out.relative_to(Path.cwd())}")


if __name__ == "__main__":
    main()
