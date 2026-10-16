"""Eval harness for the LLM summarization phase (phase 0 of docs/rag-plan.md).

Measures today's prompts so each RAG phase can be compared against them on
the same papers. Run from the repo root with the docker-compose DB and
Ollama up:

    venv/bin/python -m evals.rag sample              # write evals/labels.csv
    venv/bin/python -m evals.rag label               # hand-label it (resumable)
    venv/bin/python -m evals.rag run --variant baseline

`run` is read-only against the database: it calls the model and writes a
JSON report to evals/results/, nothing else. It is never run in CI - the test
suite only covers the pure metrics (evals/metrics.py).

A variant is how the eval asks for a rating or a summary. `baseline` is the
production prompt; RAG phases register theirs in VARIANTS and are compared
against the baseline's report.
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import random
import sys
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from sqlalchemy.orm import Session

from app import models
from app.config import get_settings
from app.database import SessionLocal
from app.services.summarize import _RELEVANCE_RUBRIC, RatedSummary, Summarizer
from evals import metrics

EVALS_DIR = Path(__file__).resolve().parent
LABELS_PATH = EVALS_DIR / "labels.csv"
RESULTS_DIR = EVALS_DIR / "results"
LABEL_FIELDS = ["topic", "arxiv_id", "title", "abstract", "human_label", "notes"]
PER_TOPIC = 20
SEED = 0


@dataclass
class EvalItem:
    topic_id: str
    topic_name: str
    topic_query: str
    paper_id: str
    arxiv_id: str
    title: str
    abstract: str
    stored_relevance: int | None
    human_label: int | None


@dataclass(frozen=True)
class Variant:
    rate: Callable[[EvalItem], int | None]
    summarize: Callable[[EvalItem], RatedSummary]


def _baseline(db: Session, summarizer: Summarizer) -> Variant:
    return Variant(
        rate=lambda i: summarizer.rate_relevance(i.title, i.abstract, i.topic_name, i.topic_query),
        summarize=lambda i: summarizer.summarize_and_rate(
            i.title, i.abstract, i.topic_name, i.topic_query
        ),
    )


# name -> factory(db, summarizer). RAG phases add theirs here.
VARIANTS: dict[str, Callable[[Session, Summarizer], Variant]] = {"baseline": _baseline}


# ---------- data access (read-only) ----------
def _topic_rows(db: Session) -> list[tuple[models.Topic, models.Paper, int | None]]:
    tp = models.topic_paper_association
    return (
        db.query(models.Topic, models.Paper, tp.c.relevance)
        .join(tp, tp.c.topic_id == models.Topic.id)
        .join(models.Paper, models.Paper.id == tp.c.paper_id)
        .filter(models.Paper.abstract.isnot(None))
        .all()
    )


def _bucket(relevance: int | None) -> str:
    if relevance is None:
        return "unrated"
    return "low" if relevance <= 5 else "mid" if relevance <= 7 else "high"


def stratified_sample(rows: list[tuple[str, str, int | None]], n: int, seed: int) -> list[str]:
    """Pick n keys, round-robin across stored-relevance buckets, so the
    labels cover the rare low ratings and not only the 8s. `rows` are
    (key, sort_key, stored_relevance); deterministic for a given seed."""
    rng = random.Random(seed)
    buckets: dict[str, list[str]] = {}
    for key, _, rel in sorted(rows, key=lambda r: r[1]):
        buckets.setdefault(_bucket(rel), []).append(key)
    for keys in buckets.values():
        rng.shuffle(keys)
    order = [buckets[b] for b in ("low", "mid", "high", "unrated") if b in buckets]
    picked: list[str] = []
    while len(picked) < n and any(order):
        for keys in order:
            if keys and len(picked) < n:
                picked.append(keys.pop())
    return picked


def read_labels(path: Path = LABELS_PATH) -> list[dict]:
    if not path.exists():
        raise SystemExit(f"{path} not found - run `python -m evals.rag sample` first.")
    with path.open(newline="") as f:
        return list(csv.DictReader(f))


def write_labels(rows: list[dict], path: Path = LABELS_PATH) -> None:
    tmp = path.with_suffix(".tmp")
    with tmp.open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=LABEL_FIELDS)
        writer.writeheader()
        writer.writerows(rows)
    os.replace(tmp, path)


def parse_label(raw: str) -> int | None:
    raw = (raw or "").strip()
    if raw.isdigit() and 1 <= int(raw) <= 10:
        return int(raw)
    return None


def load_items(db: Session, labels: list[dict]) -> list[EvalItem]:
    by_key = {(t.name, p.arxiv_id): (t, p, rel) for t, p, rel in _topic_rows(db)}
    items = []
    for row in labels:
        hit = by_key.get((row["topic"], row["arxiv_id"]))
        if hit is None:
            print(f"skipping {row['topic']}/{row['arxiv_id']}: not in the database", file=sys.stderr)
            continue
        topic, paper, rel = hit
        items.append(
            EvalItem(
                topic_id=topic.id,
                topic_name=topic.name,
                topic_query=topic.query,
                paper_id=paper.id,
                arxiv_id=paper.arxiv_id,
                title=paper.title,
                abstract=paper.abstract or "",
                stored_relevance=rel,
                human_label=parse_label(row.get("human_label", "")),
            )
        )
    return items


# ---------- commands ----------
def cmd_sample(args: argparse.Namespace) -> None:
    if LABELS_PATH.exists() and not args.force:
        raise SystemExit(f"{LABELS_PATH} exists (and may hold labels) - pass --force to overwrite.")
    db = SessionLocal()
    try:
        rows = _topic_rows(db)
    finally:
        db.close()
    by_topic: dict[str, list] = {}
    for topic, paper, rel in rows:
        by_topic.setdefault(topic.name, []).append((topic, paper, rel))
    out = []
    for name in sorted(by_topic):
        papers = {p.arxiv_id: p for _, p, _ in by_topic[name]}
        keys = stratified_sample(
            [(p.arxiv_id, p.arxiv_id, rel) for _, p, rel in by_topic[name]], args.per_topic, SEED
        )
        for arxiv_id in sorted(keys):
            p = papers[arxiv_id]
            out.append({"topic": name, "arxiv_id": arxiv_id, "title": p.title,
                        "abstract": p.abstract, "human_label": "", "notes": ""})
    write_labels(out)
    print(f"Wrote {len(out)} rows to {LABELS_PATH.relative_to(Path.cwd())}")


def cmd_label(args: argparse.Namespace) -> None:
    """Walk the unlabeled rows in the terminal. The stored LLM rating is not
    shown, so it can't anchor the human one. Saves after every answer."""
    rows = read_labels()
    todo = [r for r in rows if parse_label(r["human_label"]) is None]
    print(f"{len(rows) - len(todo)}/{len(rows)} labeled.\n\n{_RELEVANCE_RUBRIC}\n")
    print("Enter 1-10, optionally followed by a note (`7 method is only an example`), "
          "`s` to skip, `q` to quit.\n")
    for n, row in enumerate(todo, 1):
        print("=" * 78)
        print(f"[{n}/{len(todo)}] Topic: {row['topic']}   arXiv: {row['arxiv_id']}\n")
        print(row["title"], "\n")
        print(row["abstract"], "\n")
        while True:
            raw = input("label> ").strip()
            if raw in ("q", "quit"):
                return
            if raw in ("s", "skip", ""):
                break
            head, _, note = raw.partition(" ")
            label = parse_label(head)
            if label is None:
                print("  1-10, s or q")
                continue
            row["human_label"], row["notes"] = str(label), note.strip()
            write_labels(rows)
            break
    print("All rows labeled." if all(parse_label(r["human_label"]) for r in rows) else "")


def _timed(fn: Callable, *a):
    start = time.perf_counter()
    try:
        return fn(*a), time.perf_counter() - start, None
    except Exception as exc:  # noqa: BLE001 - an eval records failures, never stops on one
        return None, time.perf_counter() - start, f"{type(exc).__name__}: {exc}"


def corpus_stats(db: Session) -> dict:
    """Label-free numbers over everything stored: rating spread per topic
    and how many stored summaries state numbers absent from their abstract."""
    by_topic: dict[str, list[int | None]] = {}
    for topic, _, rel in _topic_rows(db):
        by_topic.setdefault(topic.name, []).append(rel)
    summarized = (
        db.query(models.Paper)
        .filter(models.Paper.summary.isnot(None), models.Paper.abstract.isnot(None))
        .all()
    )
    flagged = {p.arxiv_id: sorted(metrics.unsupported_numbers(p.summary, p.abstract))
               for p in summarized}
    flagged = {k: v for k, v in flagged.items() if v}
    return {
        "stored_rating_spread": {t: metrics.spread(r) for t, r in sorted(by_topic.items())},
        "stored_summaries": {
            "n": len(summarized),
            "with_unsupported_numbers": len(flagged),
            "examples": dict(list(flagged.items())[:10]),
        },
    }


def cmd_run(args: argparse.Namespace) -> None:
    settings = get_settings()
    if not settings.summaries_enabled:
        raise SystemExit("Summarization is off - set SUMMARY_BACKEND=ollama first.")
    db = SessionLocal()
    try:
        items = load_items(db, read_labels())
        variant = VARIANTS[args.variant](db, Summarizer(settings))
        report: dict = {
            "variant": args.variant,
            "model": settings.ollama_model,
            "started_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "items": len(items),
            "corpus": corpus_stats(db),
        }

        # Ratings: every item, `--repeats` times, for spread, run-to-run
        # stability, and (where labeled) agreement with the human.
        runs: list[list[int | None]] = []
        rate_times: list[float] = []
        errors: list[str] = []
        for r in range(args.repeats):
            ratings = []
            for n, item in enumerate(items, 1):
                rating, secs, err = _timed(variant.rate, item)
                ratings.append(rating)
                rate_times.append(secs)
                if err:
                    errors.append(f"rate {item.arxiv_id}: {err}")
                print(f"\rrating pass {r + 1}/{args.repeats}: {n}/{len(items)}", end="", flush=True)
            runs.append(ratings)
        print()

        by_topic: dict[str, list[int]] = {}
        for i, item in enumerate(items):
            by_topic.setdefault(item.topic_name, []).append(i)
        human = [i.human_label for i in items]
        report["ratings"] = {
            "spread_overall": metrics.spread(runs[0]),
            "spread_by_topic": {
                t: metrics.spread([runs[0][i] for i in idx]) for t, idx in sorted(by_topic.items())
            },
            "stability": metrics.agreement(runs[0], runs[1]) if len(runs) > 1 else None,
            "vs_human_overall": metrics.agreement(runs[0], human),
            "vs_human_by_topic": {
                t: metrics.agreement([runs[0][i] for i in idx], [human[i] for i in idx])
                for t, idx in sorted(by_topic.items())
            },
            "stored_vs_human": metrics.agreement([i.stored_relevance for i in items], human),
            "latency": metrics.latency(rate_times),
        }

        # Summaries: an evenly spread subset (they're ~10x slower than ratings).
        step = max(1, len(items) // args.summaries) if args.summaries else 0
        subset = items[::step][: args.summaries] if step else []
        summaries, sum_times = [], []
        for n, item in enumerate(subset, 1):
            rated, secs, err = _timed(variant.summarize, item)
            sum_times.append(secs)
            if err:
                errors.append(f"summarize {item.arxiv_id}: {err}")
            text = rated.summary if rated else None
            summaries.append({
                "topic": item.topic_name,
                "arxiv_id": item.arxiv_id,
                "relevance": rated.relevance if rated else None,
                "summary": text,
                "unsupported_numbers": sorted(metrics.unsupported_numbers(text, item.abstract))
                if text else [],
                "chars": len(text) if text else 0,
                "abstract_chars": len(item.abstract),
            })
            print(f"\rsummaries: {n}/{len(subset)}", end="", flush=True)
        print()
        ok = [s for s in summaries if s["summary"]]
        report["summaries"] = {
            "n": len(summaries),
            "failed": len(summaries) - len(ok),
            "with_unsupported_numbers": sum(bool(s["unsupported_numbers"]) for s in ok),
            "mean_compression": round(
                sum(s["chars"] / s["abstract_chars"] for s in ok) / len(ok), 3
            ) if ok else None,
            "relevance_parsed": sum(s["relevance"] is not None for s in ok),
            "latency": metrics.latency(sum_times),
            "outputs": summaries,
        }
        report["per_item"] = [
            {**{k: v for k, v in asdict(item).items() if k not in ("abstract", "topic_id", "paper_id")},
             "ratings": [run[i] for run in runs]}
            for i, item in enumerate(items)
        ]
        report["errors"] = errors
    finally:
        db.close()

    RESULTS_DIR.mkdir(exist_ok=True)
    stamp = datetime.now().strftime("%Y-%m-%d_%H%M")
    out = RESULTS_DIR / f"{stamp}_{args.variant}.json"
    out.write_text(json.dumps(report, indent=2) + "\n")
    print_report(report)
    print(f"\nFull report: {out.relative_to(Path.cwd())}")


def print_report(r: dict) -> None:
    rt = r["ratings"]
    print(f"\n== {r['variant']} ({r['model']}), {r['items']} items ==")
    print("\nRating spread (fresh, pass 1):")
    for topic, s in rt["spread_by_topic"].items():
        if s["n"]:
            print(f"  {topic:<20} n={s['n']:<3} mean={s['mean']:<5} std={s['std']:<5} "
                  f"7-9={s['share_7_to_9']:.0%}  hist={s['histogram']}")
    if rt["stability"]:
        s = rt["stability"]
        print(f"\nRun-to-run stability: exact={s['exact']:.0%} within1={s['within_1']:.0%} "
              f"mae={s['mae']} rho={s['spearman']}")
    vh = rt["vs_human_overall"]
    if vh["n"]:
        print(f"\nVs human labels (n={vh['n']}): rho={vh['spearman']} mae={vh['mae']} "
              f"within1={vh['within_1']:.0%}")
        for topic, a in rt["vs_human_by_topic"].items():
            if a["n"]:
                print(f"  {topic:<20} n={a['n']:<3} rho={a['spearman']} mae={a['mae']}")
        sh = rt["stored_vs_human"]
        if sh["n"]:
            print(f"  (stored DB ratings vs human: n={sh['n']} rho={sh['spearman']} mae={sh['mae']})")
    else:
        print("\nVs human labels: none yet - run `python -m evals.rag label`.")
    print(f"\nRating latency: {rt['latency']}")
    s = r["summaries"]
    if s["n"]:
        print(f"\nSummaries: n={s['n']} failed={s['failed']} "
              f"unsupported-number={s['with_unsupported_numbers']} "
              f"compression={s['mean_compression']} relevance-parsed={s['relevance_parsed']}")
        print(f"Summary latency: {s['latency']}")
    c = r["corpus"]["stored_summaries"]
    print(f"\nStored summaries with numbers absent from the abstract: "
          f"{c['with_unsupported_numbers']}/{c['n']}")
    if r["errors"]:
        print(f"\n{len(r['errors'])} errors, first: {r['errors'][0]}")


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="python -m evals.rag", description=__doc__.split("\n")[0])
    sub = parser.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("sample", help="write a stratified labeling set to evals/labels.csv")
    p.add_argument("--per-topic", type=int, default=PER_TOPIC)
    p.add_argument("--force", action="store_true", help="overwrite an existing labels.csv")
    p.set_defaults(fn=cmd_sample)
    p = sub.add_parser("label", help="hand-label evals/labels.csv in the terminal")
    p.set_defaults(fn=cmd_label)
    p = sub.add_parser("run", help="rate and summarize the labeled set, write a report")
    p.add_argument("--variant", choices=sorted(VARIANTS), default="baseline")
    p.add_argument("--repeats", type=int, default=2, help="rating passes (2+ measures stability)")
    p.add_argument("--summaries", type=int, default=15, help="papers to re-summarize (0 = none)")
    p.set_defaults(fn=cmd_run)
    args = parser.parse_args(argv)
    args.fn(args)


if __name__ == "__main__":
    main()
