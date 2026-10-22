"""Eval of full-text excerpts in summaries (phase E of docs/rag-plan.md).

    venv/bin/python -m evals.fulltext [--review N]

Runs on the reference-labeled papers. Any of them never checked for full
text are fetched first: one request per `arxiv_page_delay` seconds through
the shared gate, the same path the pipeline uses. This writes their chunks
and fulltext_status to the database. Each paper with full text is then
summarized twice with the production prompt: once from the abstract alone,
and once with the retrieved excerpts.

Measures, per variant:
- numbers in the summary, split into those in the abstract, those only in
  the excerpts (concrete detail full text added), and those in neither
  (unsupported - a faithfulness check);
- the same call's relevance rating against the reference labels;
- latency and prompt size.
Writes `<stamp>_fulltext.json`, plus `<stamp>_fulltext_blind.json`: a seeded
random `--review` papers with abstract, excerpts and the two summaries as
A/B, for side-by-side review. The key is in the first file.
"""
from __future__ import annotations

import argparse
import json
import random
import statistics
import time
from datetime import datetime
from pathlib import Path

from sqlalchemy import text

from app import models
from app.config import get_settings
from app.database import SessionLocal
from app.services.embed import Embedder
from app.services.retrieval import fulltext_excerpts, index_fulltext
from app.services.summarize import Summarizer
from evals import metrics

RESULTS_DIR = Path(__file__).resolve().parent / "results"


def _numbers(summary: str | None, abstract: str, excerpts: list[str]) -> dict:
    if not summary:
        return {"total": 0, "from_abstract": 0, "from_fulltext_only": [], "unsupported": []}
    found = metrics.numbers_in(summary)
    in_abstract = metrics.numbers_in(abstract)
    in_excerpts = metrics.numbers_in(" ".join(excerpts))
    return {
        "total": len(found),
        "from_abstract": len(found & in_abstract),
        "from_fulltext_only": sorted(found - in_abstract & in_excerpts),
        "unsupported": sorted(found - in_abstract - in_excerpts),
    }


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(prog="python -m evals.fulltext", description=__doc__.split("\n")[0])
    ap.add_argument("--review", type=int, default=20)
    args = ap.parse_args(argv)
    settings = get_settings()
    if not settings.embeddings_enabled:
        raise SystemExit("Needs SUMMARY_BACKEND=ollama and RAG_ENABLED=true.")
    embedder, summarizer = Embedder(settings), Summarizer(settings)
    db = SessionLocal()
    try:
        rows = db.execute(text("""
            SELECT p.id, t.name, t.query, tp.reference_relevance AS label
            FROM topic_paper tp JOIN topics t ON t.id = tp.topic_id JOIN papers p ON p.id = tp.paper_id
            WHERE tp.reference_relevance IS NOT NULL ORDER BY p.arxiv_id""")).all()
        papers = {r.id: db.get(models.Paper, r.id) for r in rows}

        fetch_secs = []
        for n, p in enumerate(papers.values(), 1):
            if p.fulltext_status is None:
                start = time.perf_counter()
                try:
                    index_fulltext(db, embedder, p)
                except Exception as exc:  # noqa: BLE001 - recorded as "error" by index_fulltext
                    print(f"\n{p.arxiv_id}: {type(exc).__name__}: {exc}")
                fetch_secs.append(time.perf_counter() - start)
            print(f"\rindexing {n}/{len(papers)}", end="", flush=True)
        print()
        statuses = {s: sum(p.fulltext_status == s for p in papers.values())
                    for s in ("ok", "unavailable", "empty", "error")}

        out = []
        usable = [r for r in rows if papers[r.id].fulltext_status == "ok"]
        for n, r in enumerate(usable, 1):
            p = papers[r.id]
            excerpts = fulltext_excerpts(db, embedder, p.id, max_excerpts=settings.rag_fulltext_excerpts)
            t0 = time.perf_counter()
            plain = summarizer.summarize_and_rate(p.title, p.abstract or "", r.name, r.query)
            t1 = time.perf_counter()
            full = summarizer.summarize_and_rate(p.title, p.abstract or "", r.name, r.query, excerpts=excerpts)
            t2 = time.perf_counter()
            chunks = db.query(models.PaperEmbedding).filter_by(
                paper_id=p.id, model=embedder.model, kind="chunk").count()
            out.append({
                "arxiv_id": p.arxiv_id, "topic": r.name, "label": r.label, "title": p.title,
                "abstract": p.abstract, "chunks": chunks, "excerpts": excerpts,
                "excerpt_chars": sum(len(e) for e in excerpts),
                "plain": {"summary": plain.summary, "relevance": plain.relevance, "secs": round(t1 - t0, 2),
                          "chars": len(plain.summary or ""),
                          "numbers": _numbers(plain.summary, p.abstract or "", excerpts)},
                "fulltext": {"summary": full.summary, "relevance": full.relevance, "secs": round(t2 - t1, 2),
                             "chars": len(full.summary or ""),
                             "numbers": _numbers(full.summary, p.abstract or "", excerpts)},
            })
            print(f"\rsummarizing {n}/{len(usable)}", end="", flush=True)
        print()
    finally:
        db.close()

    def side(s):
        o = [x[s] for x in out]
        return {
            "mean_numbers": round(statistics.fmean(x["numbers"]["total"] for x in o), 2),
            "summaries_with_fulltext_only_numbers": sum(bool(x["numbers"]["from_fulltext_only"]) for x in o),
            "summaries_with_unsupported_numbers": sum(bool(x["numbers"]["unsupported"]) for x in o),
            "mean_chars": round(statistics.fmean(x["chars"] for x in o), 1),
            "latency": metrics.latency([x["secs"] for x in o]),
            "relevance_vs_reference": metrics.agreement([x["relevance"] for x in o], [x["label"] for x in out]),
        }

    summary = {
        "papers": len(papers),
        "fulltext_status": statuses,
        "fetch_latency": metrics.latency(fetch_secs),
        "mean_chunks": round(statistics.fmean(x["chunks"] for x in out), 1) if out else 0,
        "mean_excerpt_chars": round(statistics.fmean(x["excerpt_chars"] for x in out), 0) if out else 0,
        "plain": side("plain") if out else None,
        "fulltext": side("fulltext") if out else None,
    }
    rng = random.Random(0)
    picked = rng.sample(range(len(out)), min(args.review, len(out)))
    blind, key = [], {}
    for i in picked:
        o = out[i]
        flip = rng.random() < 0.5
        a, b = (o["fulltext"]["summary"], o["plain"]["summary"]) if flip else (o["plain"]["summary"], o["fulltext"]["summary"])
        key[o["arxiv_id"]] = "A=fulltext" if flip else "A=plain"
        blind.append({"arxiv_id": o["arxiv_id"], "title": o["title"], "abstract": o["abstract"],
                      "excerpts": o["excerpts"], "A": a, "B": b})
    RESULTS_DIR.mkdir(exist_ok=True)
    stamp = datetime.now().strftime("%Y-%m-%d_%H%M")
    report = RESULTS_DIR / f"{stamp}_fulltext.json"
    report.write_text(json.dumps({"summary": summary, "blind_key": key, "items": out}, indent=1) + "\n")
    (RESULTS_DIR / f"{stamp}_fulltext_blind.json").write_text(json.dumps(blind, indent=1) + "\n")
    print(json.dumps(summary, indent=1))
    print(f"\nReports: {report.relative_to(Path.cwd())} (+ _blind)")


if __name__ == "__main__":
    main()
