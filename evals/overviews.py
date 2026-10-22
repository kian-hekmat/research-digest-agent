"""Eval of history-aware digest overviews (phase 4 of docs/rag-plan.md).

    venv/bin/python -m evals.overviews [--limit N]

Replays completed digests that have at least `MIN_PAPERS` summarized papers
and at least `MIN_PREVIOUS` earlier overviews in their topic. Each one is
written twice from the same summaries: without history (the old prompt), and
with the history the activity would have given it at the time (the topic's
previous stored overviews, completed before this digest, plus its papers'
similar earlier papers). Read-only against the database.

Writes two files to evals/results/:
- `<stamp>_overviews.json`: both overviews per digest and automatic
  metrics. Repetition is the highest embedding similarity to the previous
  overviews (lower = says something new). Unsupported numbers are numbers
  absent from the material the writer was given.
- `<stamp>_overviews_blind.json`: the pairs as A/B in a seeded random order,
  without saying which is which, for side-by-side review. The key is in the
  first file.
"""
from __future__ import annotations

import argparse
import json
import math
import random
import re
import statistics
from datetime import datetime
from pathlib import Path

from app import crud, models
from app.config import get_settings
from app.database import SessionLocal
from app.services.embed import Embedder
from app.services.summarize import OverviewHistory, Summarizer
from evals import metrics

RESULTS_DIR = Path(__file__).resolve().parent / "results"
MIN_PAPERS = 3
MIN_PREVIOUS = 2
# Phrases that show the overview is talking about history at all.
_HISTORY_WORDS = re.compile(
    r"\b(previous|earlier|prior|recent digests?|last (?:digest|week|batch)|continu\w*|again|"
    r"once more|new(?:ly)? in this|unlike (?:before|earlier)|this time)\b",
    re.IGNORECASE,
)


def _cos(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    return dot / (math.sqrt(sum(x * x for x in a)) * math.sqrt(sum(y * y for y in b)))


def _history_for(db, digest: models.Digest, count: int, style: str = "compare") -> OverviewHistory:
    earlier = (
        db.query(models.Digest)
        .filter(
            models.Digest.topic_id == digest.topic_id,
            models.Digest.status == models.DigestStatus.completed,
            models.Digest.overview.isnot(None),
            models.Digest.completed_at < digest.completed_at,
        )
        .order_by(models.Digest.completed_at.desc())
        .limit(count)
        .all()
    )
    return OverviewHistory(
        previous=tuple((f"{d.completed_at:%b} {d.completed_at.day}", d.overview) for d in reversed(earlier)),
        earlier_papers=(
            tuple(crud.similar_earlier_papers(db, [p.id for p in digest.papers])) if style == "compare" else ()
        ),
        style=style,
    )


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(prog="python -m evals.overviews", description=__doc__.split("\n")[0])
    ap.add_argument("--limit", type=int, default=30)
    ap.add_argument("--style", choices=("compare", "avoid_repeats"), default="compare")
    ap.add_argument("--reuse-plain", type=Path, default=None,
                    help="earlier report whose plain overviews to reuse (same digests, no regeneration)")
    args = ap.parse_args(argv)
    settings = get_settings()
    if not settings.summaries_enabled:
        raise SystemExit("Summarization is off - set SUMMARY_BACKEND=ollama first.")
    summarizer, embedder = Summarizer(settings), Embedder(settings)
    db = SessionLocal()
    rows = []
    try:
        digests = (
            db.query(models.Digest)
            .filter(models.Digest.status == models.DigestStatus.completed, models.Digest.completed_at.isnot(None))
            .order_by(models.Digest.completed_at.desc())
            .all()
        )
        for d in digests:
            summaries = [p.summary for p in d.papers if p.summary]
            if len(summaries) < MIN_PAPERS:
                continue
            history = _history_for(db, d, settings.rag_overview_history_count, args.style)
            if len(history.previous) < MIN_PREVIOUS:
                continue
            rows.append((d, summaries, history))
            if len(rows) >= args.limit:
                break

        reused = {}
        if args.reuse_plain:
            reused = {o["digest_id"]: o["plain"]["text"] for o in json.load(open(args.reuse_plain))["items"]}
        out = []
        for n, (d, summaries, history) in enumerate(rows, 1):
            plain = reused.get(d.id) or summarizer.write_overview(d.topic.name, summaries)
            aware = summarizer.write_overview(d.topic.name, summaries, history=history)
            prev_texts = [t for _, t in history.previous]
            vecs = embedder.embed([plain, aware, *prev_texts])
            p_vec, a_vec, prev_vecs = vecs[0], vecs[1], vecs[2:]
            source = " ".join(summaries)
            history_text = source + " " + " ".join(prev_texts) + " " + " ".join(
                f"{a} {b}" for a, b in history.earlier_papers
            )
            out.append({
                "digest_id": d.id,
                "topic": d.topic.name,
                "completed_at": d.completed_at.isoformat(),
                "papers": len(summaries),
                "earlier_paper_links": len(history.earlier_papers),
                "previous": [{"label": l, "text": t} for l, t in history.previous],
                "earlier_papers": [list(x) for x in history.earlier_papers],
                "summaries": summaries,
                "plain": {
                    "text": plain,
                    "max_sim_to_previous": round(max(_cos(p_vec, v) for v in prev_vecs), 3),
                    "unsupported_numbers": sorted(metrics.unsupported_numbers(plain, source)),
                    "history_phrases": len(_HISTORY_WORDS.findall(plain)),
                    "chars": len(plain),
                },
                "history": {
                    "text": aware,
                    "max_sim_to_previous": round(max(_cos(a_vec, v) for v in prev_vecs), 3),
                    "unsupported_numbers": sorted(metrics.unsupported_numbers(aware, history_text)),
                    "history_phrases": len(_HISTORY_WORDS.findall(aware)),
                    "chars": len(aware),
                },
            })
            print(f"\r{n}/{len(rows)}", end="", flush=True)
        print()
    finally:
        db.close()

    def agg(side, key):
        return round(statistics.fmean(o[side][key] for o in out), 3)

    summary = {
        "n": len(out),
        "by_topic": {t: sum(o["topic"] == t for o in out) for t in sorted({o["topic"] for o in out})},
        "mean_max_sim_to_previous": {"plain": agg("plain", "max_sim_to_previous"), "history": agg("history", "max_sim_to_previous")},
        "history_less_repetitive": sum(o["history"]["max_sim_to_previous"] < o["plain"]["max_sim_to_previous"] for o in out),
        "with_unsupported_numbers": {
            s: sum(bool(o[s]["unsupported_numbers"]) for o in out) for s in ("plain", "history")
        },
        "mean_history_phrases": {"plain": agg("plain", "history_phrases"), "history": agg("history", "history_phrases")},
        "mean_chars": {"plain": agg("plain", "chars"), "history": agg("history", "chars")},
    }
    rng = random.Random(1)  # a different A/B order from the first review
    blind, key = [], {}
    for i, o in enumerate(out):
        first_is_history = rng.random() < 0.5
        a, b = (o["history"]["text"], o["plain"]["text"]) if first_is_history else (o["plain"]["text"], o["history"]["text"])
        key[str(i)] = "A=history" if first_is_history else "A=plain"
        blind.append({"item": i, "topic": o["topic"], "previous": o["previous"],
                      "earlier_papers": o["earlier_papers"], "summaries": o["summaries"], "A": a, "B": b})

    RESULTS_DIR.mkdir(exist_ok=True)
    stamp = datetime.now().strftime("%Y-%m-%d_%H%M")
    report = RESULTS_DIR / f"{stamp}_overviews_{args.style}.json"
    report.write_text(json.dumps({"style": args.style, "summary": summary, "blind_key": key, "items": out}, indent=1) + "\n")
    (RESULTS_DIR / f"{stamp}_overviews_{args.style}_blind.json").write_text(json.dumps(blind, indent=1) + "\n")
    print(json.dumps(summary, indent=1))
    print(f"\nReports: {report.relative_to(Path.cwd())} (+ _blind)")


if __name__ == "__main__":
    main()
