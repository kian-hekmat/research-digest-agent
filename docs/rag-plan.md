# RAG plan for the summarization phase

Status: proposal, nothing implemented. Written 2026-10-16 against `main` @ `81da41e`.

## 1. Where the LLM phase stands today

Every LLM call in `app/services/summarize.py` sees exactly one thing: the text
it is handed in the prompt.

| Call | Input | Output | Where |
|---|---|---|---|
| `summarize_and_rate` | topic name/query + title + abstract | `Relevance: N` + 3–4 sentence summary | `activities.summarize_paper` |
| `rate_relevance` | same, for a paper another topic already summarized | integer | `activities.rate_relevance` |
| `write_overview` | topic name + this batch's summaries | one paragraph | `activities.write_overview` |

What the data in the local database says about the outputs (398 papers, 330 summarized):

- **Summaries are paraphrases.** Average abstract is ~1,290 chars, average
  summary ~640. The model has no information beyond the abstract, so the best
  it can do is compress it. It cannot say how a paper relates to anything else.
- **Relevance ratings bunch at 7–9 among on-topic papers.** For Numerical
  Analysis, 143 of 150 stored ratings are 7, 8 or 9. Ratings carry 60% of the
  ranking weight (`app/services/ranking.py`), and the cap on papers per email
  mostly cuts among on-topic papers, so this 7-vs-9 call decides who gets in.
  The rubric has no anchor: the model never sees what a "7" or a "9" looked
  like for this topic. *Phase 0 confirmed this; see §5.1. Gemma separates
  off-topic from on-topic papers well but barely orders on-topic papers
  (ρ = 0.31 against the reference labels).*
- **Overviews have no memory.** `write_overview` sees only the current batch,
  so it can't say "third paper this month on X" or "contradicts last week's Y".
  Each digest is written as if it were the first.
- **Category topics have no stated interest.** `cat:math.NA` matches a whole
  arXiv category. "Relevance to Numerical Analysis" is close to 10 for every
  paper by construction, so the rating says nothing about what the reader
  actually cares about.

## 2. What RAG can and can't fix

RAG adds context the model doesn't currently have. For this project there are
three useful sources to retrieve from:

1. **The project's own paper corpus** (Postgres: titles, abstracts, summaries,
   ratings, past overviews). Cheap, already local, and it grows every day.
   This is the biggest win.
2. **The paper's own full text** (arXiv HTML/PDF). The only way to make
   summaries more *accurate* than the abstract (concrete numbers, datasets,
   limitations), but it's the most expensive to fetch and process.
3. **A per-topic interest profile** (a short description the user writes, plus
   rated examples). Small, but it's what makes relevance mean something for
   category topics.

What RAG won't do: make an abstract-only summary more faithful. The abstract
is already the whole source. Retrieval from the corpus adds *context*
(relations, novelty, calibration), not accuracy. Accuracy comes from full text
(phase 5).

## 3. Improvements, ranked by value per unit of effort

### A. Related-work context in each summary (corpus RAG)
Before summarizing a paper, retrieve the k most similar papers already in the
database (cosine similarity on embeddings) and include their titles, ids and
summaries in the prompt. The output gains one optional sentence:

```
Relevance: 8

<3–4 sentence summary>

Related: Builds on 2609.31244's adaptive collocation scheme, but replaces the
residual-based sampler with a learned one.
```

- Only papers **published before** this one are eligible, so "builds on" is at
  least chronologically possible. The paper itself and its other arXiv versions
  are excluded.
- A similarity floor (start at ~0.75 and tune it) so weak neighbours never reach the
  prompt. With no neighbour above the floor, the prompt has no related-work
  section and the output has no `Related:` line.
- **Citations are checked.** A 12B model will invent relationships if allowed
  to. The `Related:` line has to cite arXiv ids. The parser (a sibling of
  `parse_rated_summary`) drops the line if any cited id isn't in the retrieved
  set. The email renders the cited ids as links.

### B. Novelty and near-duplicate signal (corpus retrieval, no LLM)
The same nearest-neighbour query gives `max_similarity_to_prior` for free:

- **Near duplicates** (sim ≥ ~0.95: v2s, companion papers, resubmissions under
  new ids) get collapsed in the email or marked "see also".
- **Novelty** = `1 − max similarity to papers from the last 90 days` becomes a
  fourth ranking signal in `ranking.py`. It follows the existing pattern: scale
  to [0, 1], impute the median when missing, and use a small weight (start at
  0.1, taken from h-index).

### C. Relevance ratings calibrated with retrieved examples
Few-shot retrieval: give the rater the 3–5 most similar papers that **already
have a rating for this topic**, each with its rating, as anchors. Pick them so
they span the scale (one high, one mid, one low when available), not just the
nearest ones, or every example will be a 8.

Pair this with a new nullable `topics.description` column ("I care about
structure-preserving integrators and low-rank tensor methods, not general
FEM"). `_topic_line` includes it when set. For `cat:` topics, this is the
single biggest improvement to ranking.

Later, once there's a feedback signal (clicks on links in the email, or a
thumbs-up/down link), user-confirmed ratings replace LLM ratings as the
few-shot anchors. That turns the rater into a lightweight personalized model.
Out of scope here, but the schema should leave room for it (see §4).

### D. History-aware overviews
`write_overview` additionally receives:

- the topic's last 2–3 overviews (plain SQL, no vectors needed), and
- for each paper in the batch, its top earlier neighbour (from A).

The prompt asks what is *new* relative to recent digests. The cost is one call
per digest, with a somewhat longer prompt.

### E. Full-text retrieval (optional, highest cost)
Fetch arXiv's HTML rendering (`https://arxiv.org/html/<id>`, available for most
LaTeX submissions; skip a paper when it's missing), split it into section-aware
chunks, embed them, and retrieve the 3–4 chunks that best match fixed queries
("main quantitative result", "method", "limitations"). Feed those into the
summary prompt alongside the abstract.

Gemma 3 has a 128k context, so stuffing the whole paper is *possible*, but on a
local 12B at Q4 it would take minutes per paper and run into the
`_LLM_TIMEOUT` of 180s with 4 concurrent calls. Retrieval keeps prompts around
3k tokens. This is the only phase that makes summaries more accurate rather
than better contextualized.

## 4. Architecture

Stay inside the current stack. No new service.

**Vector store: pgvector in the existing Postgres.**
- `docker-compose.yml`: `postgres:16` → `pgvector/pgvector:pg16` (a drop-in
  image with the same data volume format).
- An Alembic migration that runs `CREATE EXTENSION IF NOT EXISTS vector` and
  creates:

```
paper_embeddings
  paper_id   uuid  FK papers.id ON DELETE CASCADE
  model      text                       -- e.g. "embeddinggemma"
  kind       text                       -- "abstract" | "chunk" (phase E)
  chunk_ix   int   default 0
  content    text                       -- the text that was embedded
  embedding  vector(768)
  PK (paper_id, model, kind, chunk_ix)
  HNSW index on embedding vector_cosine_ops
```

  `model` is part of the key, so switching embedding models means re-embedding
  side by side and then cutting over, not a destructive migration.
- `papers.related_ids` (or a `paper_related` join table): which neighbours
  were cited, kept for the email and for auditing hallucinations.
- `topics.description text NULL` (C).
- Leave room for C's feedback loop: `topic_paper.relevance_source`
  (`llm` | `user`) can come later.

**Embedding model: `embeddinggemma` via Ollama** (`/api/embed`, 768-dim,
~300M params). It keeps the "Gemma only" constraint from `c784285`, runs
alongside `gemma3:12b` without trouble, and `/api/embed` takes a batch of inputs in
one call. Fallback: `nomic-embed-text`. Whichever is used needs its
task prefixes for queries vs documents, applied in one place.

**Code:**
- `app/services/embed.py`: an `Embedder` with the same shape as `Summarizer`
  (injectable `http_client`, `Settings`-driven, and a no-op when
  `rag_enabled=False`).
- `app/services/retrieval.py`: pure query helpers (`neighbours(db, paper_id,
  k, min_sim, before=…, topic_id=…)`, `rated_examples(...)`). This is the only
  place that writes pgvector SQL.
- `Summarizer.summarize_and_rate` / `rate_relevance` / `write_overview` gain an
  optional `context` argument. With no context, the prompts are byte-for-byte
  what they are today, which keeps the existing tests valid and keeps
  `rag_enabled=false` a true off switch.
- `app/config.py`: `rag_enabled`, `embedding_model`, `rag_top_k=4`,
  `rag_min_similarity=0.75`, `rag_novelty_window_days=90`.

**Workflow (`DigestWorkflow`):**

```
fetch → ingest → [NEW] embed_papers (one batched activity, LLM retry policy)
      → summarize/rate (retrieval happens *inside* the activity)
      → overview (with history) → finalize → watermark
```

- Retrieval goes inside `summarize_paper` / `rate_relevance` rather than in a
  separate activity. It's a millisecond DB query, and returning neighbour
  texts through the workflow would bloat Temporal history for no benefit.
- `embed_papers` failing must **degrade, not block**, the same way a failed
  overview does today: summaries fall back to the no-context prompt.
- Papers in the same batch are embedded before any summary runs, so a batch's
  papers can see each other (useful for D; for A they're excluded unless
  published earlier).
- `app/backfill_embeddings.py`, modelled on `app/backfill_summaries.py`,
  embeds the existing ~400 papers once (seconds of work).

## 5. Evaluation: do this first

Without a baseline, there's no way to tell whether RAG helped. That matters
most for A, where the failure mode (made-up relationships) reads fluently.

- **Relevance:** hand-label ~60 topic–paper pairs (20 per topic) on the 1–10
  rubric. Report Spearman correlation and the spread of scores (std dev),
  with and without C. Success means the 7–9 pile spreads out *and* agrees
  better with the hand labels.
- **Related-work sentences:** for ~30 papers, have a human mark each `Related:`
  claim as correct / vague / wrong. Also report automatically how often the
  validator drops a line for citing an id outside the retrieved set. Ship A
  only if "wrong" stays under ~10%.
- **Summaries (phase E):** check that each number in a summary appears in the
  abstract or a retrieved chunk (a regex check). Do a blind A/B read of 20
  pairs.
- **Cost:** wall-clock time per digest run before and after. The 180s activity
  timeout and 4-way concurrency are the hard limits.

This lives in `evals/` (`python -m evals.rag`). It runs on demand against the
local DB and Ollama and stays out of CI, which keeps using fakes as it does now.
Only the pure metrics are unit-tested (`tests/test_eval_metrics.py`).

### 5.1 Baseline results (2026-10-16, `gemma3:12b`)

Full report: `evals/results/2026-10-16_1304_baseline.json`. The earlier
`_1242` run predates the labels, and `_1256` predates the RLHF cleanup. The
set is 47 topic–paper pairs: 20 each for Numerical Analysis and PINNs,
stratified by stored rating so that rare low ratings are represented, and all
7 papers RLHF has. The stratification means the set has more off-topic papers
than the real corpus does.

**About the reference labels.** The labels in `evals/labels.csv` were written
by Claude (Opus 5.5), not by the project owner. The `labeler` column says so
on every row. Treat them as a strong-model reference, not as ground truth
for what this reader wants. `python -m evals.rag label` overwrites any row
with `labeler=human`, and the report prints how many labels came from each
labeler.

**RLHF cleanup (2026-10-16).** 25 consecutive arXiv ids
(2609.17076–2609.17127) were all ingested into RLHF on 2026-09-16, one day
after `3d7df43 fixed searching`. That's the signature of the old unquoted
query, which returned the newest submissions regardless of topic. Their
`topic_paper` links were deleted, so RLHF went from 32 papers to 7. Their
`digest_paper` rows were kept as the record of what past digests contained.
The RLHF sample was then redrawn with `python -m evals.rag sample --topic
RLHF`. With only 7 papers, RLHF's per-topic numbers are indicative at best.

| Measure | Result |
|---|---|
| Rating spread on the set (rate-only prompt) | std 2.5; values used: 1, 3, 4 and 7–10, with **nothing at 5–6** |
| Run-to-run stability (2 passes, temp 0.2) | 98–100% exact across three runs |
| Combined summarize+rate call vs rate-only call (n=15) | 53% exact, 87% within 1, ρ = 0.875 |
| Fresh summaries stating a number absent from the abstract | 0 / 15 |
| Stored summaries stating a number absent from the abstract | 1 / 330 |
| Summary length / abstract length | 0.45 |
| Latency per call | rating 0.8s mean (p90 1.2s); summary 3.6s mean (p90 3.9s) |
| Fresh ratings vs reference labels, all 47 | ρ = 0.72, MAE 1.4, 66% within 1 |
| …among on-topic papers only (reference ≥ 6, n=32) | **ρ = 0.31**, MAE 1.1, 72% within 1 |
| Per topic | NA ρ 0.74 · PINNs ρ 0.80 · RLHF ρ 0.95 but MAE 2.1 (n=7) |
| Same 31 items: stored DB ratings vs fresh rate-only ratings, each against the reference | stored ρ 0.67, MAE 1.7 · fresh ρ 0.75, MAE 1.6 |

What this changes in the plan:

- **Gemma separates papers coarsely but can't order the on-topic ones.**
  Most of the overall ρ comes from the easy split between off-topic and
  on-topic papers. Among the papers that matter for the cap, ρ is 0.31. The
  disagreements follow one pattern: **a paper that names the topic in its
  abstract gets 7–9 even when the topic is peripheral.** For example, a
  turbulence perspective that mentions PINNs once got 7 (reference 2). A
  jailbreak-safety paper that lists RLHF as prior work got 7 (reference 2). A
  debate paper motivated by RLHF got 8 (reference 4). This is why RLHF has
  ρ 0.95 but MAE 2.1: its order is right, but its three peripheral papers are
  rated as core ones. This is exactly the "mentioned in passing vs main
  subject" distinction the rubric asks for. It's the case for **C (few-shot
  anchors plus topic descriptions)**, which moves up to the first prompt
  change after phase 1.
- **The combined summarize+rate call may rate slightly worse than
  rate-only.** On the same 31 items, stored ratings get ρ 0.67 and fresh
  rate-only ratings get ρ 0.75. Some stored ratings may come from older
  prompts, and n is small, so this is a hint, not a result. It's cheap to
  test on its own: rate in a separate ~1s call.
- **Accuracy against the abstract is not a problem.** Unsupported numbers
  are about 0%, which confirms §2: corpus RAG (A, B, D) should be sold on
  *context*, and only full text (E) can raise accuracy.
- **There's a lot of latency headroom.** Calls take 1–5s against a 180s
  timeout, so prompts several times longer (4 neighbours plus anchors) are
  affordable.
- **The two prompts disagree slightly** (combined call vs rate-only, MAE
  0.8). A rerated paper and a newly summarized one aren't quite on the same
  scale. This is minor, but it's worth fixing when C changes both prompts.

## 6. Phases

| # | Scope | Ships | Size |
|---|---|---|---|
| 0 | Eval harness + hand labels + baseline numbers | `evals/rag.py`, `evals/labels.csv` | S |
| 1 | pgvector, `paper_embeddings`, `Embedder`, `embed_papers` activity, backfill | infra only, no change to outputs | M |
| 2 | **A + B**: related-work context with checked citations, near-dup collapse, novelty ranking signal | email `Related:` line, new ranking term | M |
| 3 | **C**: `topics.description` + few-shot rating anchors (API: `PATCH /topics` accepts description) | better ranking | S–M |
| 4 | **D**: history-aware overviews | better overview paragraph | S |
| 5 | **E**: full-text chunk retrieval from arXiv HTML | more concrete summaries | L |

Phases 2–4 each sit behind `rag_enabled` and keep the no-context code path
intact, so any one of them can be turned off without a deploy.

### Tests to add (following the existing suite's patterns)
- `Embedder` against a respx-mocked `/api/embed`, plus the no-op mode.
- `retrieval.py` against real Postgres with pgvector (the conftest DB image
  changes to `pgvector/pgvector:pg16`, and CI's service container too):
  ordering, the similarity floor, the `before=` filter, self-exclusion.
- Parser: a `Related:` line citing an unknown id is dropped and the summary
  is kept, matching the existing "a malformed rating must never cost the paper
  its summary" rule.
- Workflow: a failed `embed_papers` still completes the digest with summaries
  (time-skipping test server, same as `test_temporal_workflows.py`).
- Ranking: novelty imputation and the weights still sum correctly.

## 7. Open decisions
1. Embedding model: `embeddinggemma` (Gemma-only, recommended) or
   `nomic-embed-text`.
2. Whether to show `Related:` links in the email, or use retrieval only
   internally (for ranking and overviews) until phase 0's numbers come back.
3. Whether phase 5's extra arXiv traffic (one HTML fetch per new paper, ~25 per
   topic per run) fits within arXiv's usage policy at the current
   `arxiv_page_delay`, or needs its own slower rate limit.
