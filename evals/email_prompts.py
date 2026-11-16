"""Eval of the phase 2d email prompts (docs/plans/
2026-11-16-phase-2d-picks-first-email-design.md, Round 1).

    venv/bin/python -m evals.email_prompts [--pairs 20] [--seed 1]

Rebuilds Monday-to-Monday weekly windows (boundaries at prep time, Monday
01:00 Eastern) from the completed digests, ranks each with production's
ranking, and writes each window's overview and picks with the new prompt,
oldest first, each seeing the topic's last two new overviews as
avoid-repeats history. Then blind pairs for a sample of the windows' top-5
papers: today's prep summary (summarize_and_rate, re-run now) against the
new blurb. Reads the database; writes only llm_usage rows (spend recorded,
no topic).

Writes to evals/results/:
- `<stamp>_email_prompts.json`: automatic checks, every overview with its
  inputs, both texts of every pair, and the blind key.
- `<stamp>_email_prompts_overviews_review.json`: per window, the offered
  papers (id, title, abstract), the pick pool, the overview and the picks -
  for checking each citation against its abstract.
- `<stamp>_email_prompts_blurbs_blind.json`: the pairs as A/B.
"""
from __future__ import annotations

import argparse
import json
import random
import statistics
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from app import cadence, crud, models
from app.database import SessionLocal
from app.services.summarize import OverviewAndPicks, OverviewPaper, Summarizer
from app.spend_ledger import DbLedger
from app.temporal.activities import _rank_window
from evals import metrics

RESULTS_DIR = Path(__file__).resolve().parent / "results"
MIN_PAPERS = 5
HISTORY = 2  # previous overviews shown as avoid-repeats history


@dataclass
class EvalWindow:
    topic_id: str
    topic_name: str
    topic_query: str
    start: datetime
    end: datetime
    paper_ids: list[str]  # ranked best first, near duplicates left out


def _boundary_at_or_before(t: datetime) -> datetime:
    """The latest prep-time boundary (Monday 08:00 ET - PREP_LEAD) at or before t."""
    return cadence.latest_slot(t + cadence.PREP_LEAD) - cadence.PREP_LEAD


def _next_boundary(b: datetime) -> datetime:
    return cadence.next_slot(b + cadence.PREP_LEAD) - cadence.PREP_LEAD


def weekly_windows(db, min_papers: int = MIN_PAPERS) -> list[EvalWindow]:
    """Every ended weekly window with at least `min_papers` papers, per topic,
    oldest first, ranked as prep ranks an edition."""
    now = datetime.now(timezone.utc)
    windows: list[EvalWindow] = []
    for topic in db.query(models.Topic).order_by(models.Topic.created_at):
        first = (
            db.query(models.Digest.completed_at)
            .filter(models.Digest.topic_id == topic.id, models.Digest.status == models.DigestStatus.completed)
            .order_by(models.Digest.completed_at)
            .limit(1)
            .scalar()
        )
        if first is None:
            continue
        # Strictly before the first digest: windows are (start, end], so a
        # digest exactly on a boundary belongs to the window ending there.
        start = _boundary_at_or_before(first - timedelta(microseconds=1))
        while (end := _next_boundary(start)) <= now:
            papers = crud.window_papers(db, topic.id, start, end)
            if len(papers) >= min_papers:
                # The second value, as prep's rank_edition takes its top K:
                # near duplicates folded away, never in the top 5.
                _, ranked = _rank_window(db, topic.id, papers, end)
                windows.append(EvalWindow(topic.id, topic.name, topic.query, start, end, [p.id for p in ranked]))
            start = end
    return windows


def overview_checks(result: OverviewAndPicks, papers: list[OverviewPaper]) -> dict:
    source = " ".join(f"{p.arxiv_id} {p.title} {p.abstract}" for p in papers)
    return {
        "words": len(result.overview.split()),
        "dropped_citations": len(result.dropped_citations),
        "dropped_picks": len(result.dropped_picks),
        "unsupported_numbers": sorted(metrics.unsupported_numbers(result.overview, source)),
    }


def blind_pairs(items: list[dict], seed: int) -> tuple[list[dict], dict[str, str]]:
    """Each item's `old` and `new` as A/B in a seeded order; the key says
    which side is new."""
    rng = random.Random(seed)
    blind, key = [], {}
    for i, item in enumerate(items):
        new_first = rng.random() < 0.5
        a, b = (item["new"], item["old"]) if new_first else (item["old"], item["new"])
        key[str(i)] = "A=new" if new_first else "A=old"
        rest = {k: v for k, v in item.items() if k not in ("old", "new")}
        blind.append({"item": i, **rest, "A": a, "B": b})
    return blind, key


def _overview_paper(p: models.Paper) -> OverviewPaper:
    return OverviewPaper(p.arxiv_id, p.title, p.abstract or "")


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(prog="python -m evals.email_prompts", description=__doc__.split("\n")[0])
    ap.add_argument("--pairs", type=int, default=20)
    ap.add_argument("--seed", type=int, default=1)
    args = ap.parse_args(argv)

    summarizer = Summarizer(ledger=DbLedger())
    db = SessionLocal()
    overviews, pairs = [], []
    try:
        windows = weekly_windows(db)
        history: dict[str, list[str]] = {}
        for n, w in enumerate(windows, 1):
            by_id = {p.id: p for p in db.query(models.Paper).filter(models.Paper.id.in_(w.paper_ids))}
            papers = [_overview_paper(by_id[pid]) for pid in w.paper_ids]
            result = summarizer.write_overview_and_picks(
                w.topic_name, papers, previous=history.get(w.topic_id, [])[-HISTORY:]
            )
            offered = papers[:25]
            overviews.append({
                "topic": w.topic_name,
                "start": w.start.isoformat(),
                "end": w.end.isoformat(),
                "papers_in_window": len(papers),
                "offered": [asdict(p) for p in offered],
                "pick_pool": [p.arxiv_id for p in offered[:5]],
                "overview": result.overview if result else None,
                "picks": [asdict(p) for p in result.picks] if result else [],
                "checks": overview_checks(result, offered) if result else None,
                "dropped_citations": result.dropped_citations if result else [],
                "dropped_picks": result.dropped_picks if result else [],
            })
            if result:
                history.setdefault(w.topic_id, []).append(result.overview)
            print(f"\roverviews {n}/{len(windows)}", end="", flush=True)
        print()

        rng = random.Random(args.seed)
        candidates = {pid: w for w in windows for pid in w.paper_ids[:5]}
        sample = rng.sample(sorted(candidates), min(args.pairs, len(candidates)))
        for n, pid in enumerate(sample, 1):
            w, paper = candidates[pid], db.get(models.Paper, pid)
            old = summarizer.summarize_and_rate(paper.title, paper.abstract or "", w.topic_name, w.topic_query).summary
            blurb = summarizer.write_blurb(paper.title, paper.abstract or "")
            pairs.append({
                "paper": paper.arxiv_id, "topic": w.topic_name, "title": paper.title,
                "abstract": paper.abstract or "",
                "old": old or "", "new": f"{blurb.hook}\n{blurb.what}" if blurb else "",
            })
            print(f"\rblurb pairs {n}/{len(sample)}", end="", flush=True)
        print()
    finally:
        db.close()

    checked = [o["checks"] for o in overviews if o["checks"]]
    summary = {
        "windows": len(overviews),
        "by_topic": {t: sum(o["topic"] == t for o in overviews) for t in sorted({o["topic"] for o in overviews})},
        "overviews_failed": sum(o["overview"] is None for o in overviews),
        "words": {
            "mean": round(statistics.fmean(c["words"] for c in checked), 1) if checked else None,
            "outside_80_120": sum(not 80 <= c["words"] <= 120 for c in checked),
        },
        "dropped_citations": sum(c["dropped_citations"] for c in checked),
        "dropped_picks": sum(c["dropped_picks"] for c in checked),
        "picks_per_overview": {k: sum(len(o["picks"]) == k for o in overviews) for k in (0, 1, 2)},
        "overviews_with_unsupported_numbers": sum(bool(c["unsupported_numbers"]) for c in checked),
        "pairs": len(pairs),
        "pairs_missing_a_side": sum(not p["old"] or not p["new"] for p in pairs),
        "blurbs_with_unsupported_numbers": sum(
            bool(metrics.unsupported_numbers(p["new"], f"{p['title']} {p['abstract']}")) for p in pairs
        ),
    }
    blind, key = blind_pairs(pairs, args.seed)

    RESULTS_DIR.mkdir(exist_ok=True)
    stamp = datetime.now().strftime("%Y-%m-%d_%H%M")
    report = RESULTS_DIR / f"{stamp}_email_prompts.json"
    review = RESULTS_DIR / f"{stamp}_email_prompts_overviews_review.json"
    blurbs = RESULTS_DIR / f"{stamp}_email_prompts_blurbs_blind.json"
    report.write_text(json.dumps({"summary": summary, "blind_key": key, "overviews": overviews, "pairs": pairs}, indent=1) + "\n")
    review.write_text(json.dumps([
        {k: o[k] for k in ("topic", "start", "end", "offered", "pick_pool", "overview", "picks")} for o in overviews
    ], indent=1) + "\n")
    blurbs.write_text(json.dumps(blind, indent=1) + "\n")
    print(json.dumps(summary, indent=1))
    print(f"\nReports: {report.name}, {review.name}, {blurbs.name} (in {RESULTS_DIR})")


if __name__ == "__main__":
    main()
