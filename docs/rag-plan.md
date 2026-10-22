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
- A similarity floor (start at ~0.65, from the phase 1 distribution in §5.2, and tune it) so weak neighbours never reach the
  prompt. With no neighbour above the floor, the prompt has no related-work
  section and the output has no `Related:` line.
- **Citations are checked.** A 12B model will invent relationships if allowed
  to. The `Related:` line has to cite arXiv ids. The parser (a sibling of
  `parse_rated_summary`) drops the line if any cited id isn't in the retrieved
  set. The email renders the cited ids as links.

### B. Novelty and near-duplicate signal (corpus retrieval, no LLM)
The same nearest-neighbour query gives `max_similarity_to_prior` for free:

- **Near duplicates** (sim ≥ ~0.9, untested: the current corpus has none above 0.89; v2s, companion papers, resubmissions under
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
  `rag_min_similarity=0.65`, `rag_novelty_window_days=90`.

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

### 5.2 Phase 1 results (2026-10-16, `embeddinggemma`)

All 398 papers are embedded (768-dim, `kind=abstract`), which took 11s through
`python -m app.backfill_embeddings`. Cosine similarity over the corpus:

| | p10 | p50 | p90 | max / p99 |
|---|---|---|---|---|
| Random pairs (3,000) | 0.28 | 0.41 | 0.54 | p99 0.67 |
| Each paper's nearest neighbour | 0.56 | 0.66 | 0.76 | max 0.89 |

- **The scale is compressed, so the floors move down.** A 0.75 floor would
  drop about 90% of nearest neighbours. 0.65 is roughly "more similar than
  99% of random pairs" and is the new starting point for A. No pair reaches
  0.9, so B's near-duplicate threshold can't be tuned until the corpus has a
  real duplicate.
- **Neighbours are topically sensible.** 89% of nearest neighbours (332/373)
  share a topic. Spot checks: the personalized reward-model paper's nearest
  neighbours are reward-hacking and preference-alignment papers, and the
  Helmholtz thin-coating paper's are scattering, PML and Schwarz-preconditioner
  papers.
- **Unlinked papers are still retrievable.** Papers no topic links to (the 25
  stale RLHF papers) stay in `papers` with embeddings, and one of them showed
  up as a neighbour. Phase 2 should either pass `topic_id` or require the
  neighbour to be linked to at least one topic.

### 5.3 Phase 2 results (2026-10-16)

**Related-work notes (A) fail the ship bar, so they're off by default.** Claude
(not the project owner) reviewed every note Gemma wrote against both
abstracts. The verdicts are in `evals/results/2026-10-16_related_notes_review.json`.

| Round | Floor | Prompt | Offered → notes | Correct | Vague | Wrong |
|---|---|---|---|---|---|---|
| 1 | 0.65 | "builds on, extends, competes with, or contradicts" | 21 → 8 | 2 | 2 | **4 (50%)** |
| 2 | 0.72 | "same problem or competing method; no lineage claims unless the abstract says so" | 46 → 19 | 9 | 5 | **5 (26%)** |

- No note was rejected by the citation check. Gemma always cited ids it had
  been given. Its errors are about *content*, which a citation check can't
  catch.
- Round 1's wrong notes all came from neighbours at ≤ 0.70 and invented
  "builds on" / "extends" lineage. Raising the floor and banning lineage
  claims halved the error rate.
- Round 2's wrong notes are spread across 0.72–0.84, so no floor fixes them.
  The pattern: Gemma projects the *new* paper's framing onto the *cited* one.
  It says the random-feature PINN paper is about adapting across PDEs (it's
  about spectral bias), pins a large-N approximation on a paper that doesn't
  use one, and misses that one paper is Part II of the other.
- 26% wrong is far above the 10% bar, so `RAG_RELATED_NOTES` defaults to
  false. The full pipeline (prompt, citation check, `paper_related` storage,
  rendering) is in place and tested. It can be switched on to re-evaluate
  with a stronger model, or with full text (E), since the cited papers'
  abstracts were the model's only evidence.

**What ships instead: "Similar earlier paper".** Retrieval itself was
reliable. Every *neighbour* reviewed at ≥ 0.75 was a genuinely related paper,
including the Part I/II pair and the two competing Helmholtz coarse-space
papers. So emails show the closest earlier, topic-linked paper at ≥ 0.75 as a
plain link, with no LLM claim attached. That covers 26 of 373 linked papers
(7%) today, and coverage grows with the corpus.

**Novelty (B) is in the ranking at 10%.** `prior_similarity` is computed for
397/398 papers (mean 0.62, range 0.32–0.89). Relevance and prior similarity
correlate positively (ρ 0.15 for Numerical Analysis, 0.69 for PINNs with
n=17, 0.23 overall): papers squarely on a topic resemble its earlier papers.
So novelty pulls slightly against relevance, as expected, and its weight
stays small (taken from h-index: 0.6 / 0.3 / 0.1). In
`app/services/ranking.py`, a central repeat still beats a tangential novel
paper.

**Near-duplicate collapse (B) is wired but has no live cases.** No pair in
the corpus reaches the 0.9 threshold, so `near_duplicate_of` is empty and the
email fold (duplicates listed under their original, outside the cap) is
covered only by tests.

**Other fixes made along the way:**
- Retrieval ignores papers no topic links to, such as the 25 stale RLHF
  papers (`linked_only`).
- The parser catches a `Related:` tacked onto the end of the paragraph. The
  round 1 eval caught one leaking into a summary, flagged by the
  unsupported-number check because it contained an arXiv id. A note must
  also say something beyond its citations.
- Retrieval failures inside `summarize_paper` are logged and the summary
  goes ahead without them.

### 5.4 Phase 3 results (2026-10-16): C doesn't help gemma3:12b

The setup: the 47 reference labels were imported as anchors
(`topic_paper.reference_relevance`, `python -m app.import_relevance_labels`).
Claude drafted descriptions for the three topics from the scope decisions it
made while labeling (below). Each variant rated all 47 papers with the
rate-only prompt and the combined summarize+rate prompt; the combined prompt
is the production path for new papers. Anchors never include the paper being
rated, so against the reference labels this is leave-one-out. Reports are
`evals/results/2026-10-16_15{27,31,37,43}_*.json`.

| Variant | ρ all (rate-only) | ρ on-topic (rate-only) | MAE | ρ all (combined) | ρ on-topic (combined) | MAE |
|---|---|---|---|---|---|---|
| baseline | **0.71** | 0.27 | **1.45** | 0.68 | 0.24 | **1.53** |
| description | 0.67 | 0.27 | 1.68 | **0.71** | **0.37** | 1.87 |
| anchors | 0.52 | 0.02 | 1.83 | 0.53 | 0.13 | 1.96 |
| description + anchors | 0.60 | 0.19 | 1.77 | 0.58 | 0.28 | 1.89 |

(On-topic means reference ≥ 6, n=32.)

- **Anchors hurt.** Gemma rarely copies an anchor (its rating matched the
  nearest anchor's in 3/47 cases). The examples add noise instead: with
  about 20 labels per topic, and fewer once the paper itself is left out, the
  nearest anchors are only loosely related (similarity 0.39–0.66). Seeing a
  10-rated anchor, a PDE-theory paper (reference 3) went from 3 to 8.
  `RAG_RATING_ANCHORS` now defaults to false.
- **The description is a wash.** It fixes peripheral papers: metric-reward
  RL went 7→3 (reference 3) and a turbulence perspective 7→5 (reference 2).
  But Gemma over-applies the "Not:" list to core papers: an entropy-stable
  scheme went 8→5 and a Maxwell PML analysis 7→4, both reference 10. The
  combined path's on-topic ρ rose 0.24 → 0.37, but at n=32 that's within
  noise (standard error ≈ 0.17), and MAE got worse on both paths.
- **What was kept:** `topics.description` and `PATCH /topics/{id}` stay as an
  opt-in, user-written feature. The rater sees a description whenever one is
  set. Claude's drafted descriptions were measured and then **cleared** from
  the live topics: they encode Claude's labeling choices, not the reader's,
  and their effect is inconclusive. The anchor machinery and the imported
  reference labels stay in place, unused while anchors are off.

**What this means for ranking.** The open problem from §5.1 stands: Gemma
can't order on-topic papers (ρ ≈ 0.25), yet relevance carries 60% of the
score among exactly those papers. Options that don't depend on prompt
calibration:
1. Use relevance as a coarse gate (off-topic vs on-topic) and rank on-topic
   papers by the other signals.
2. Collect real reader feedback (clicks, thumbs) and use it both as labels
   and as anchors. A real labeled pool of a few hundred papers per topic would
   also give anchors a fair test.
3. Use a stronger rater model. Measure it with `evals/rag.py` first.

Draft descriptions (Claude's, for the owner to edit or discard), to apply
with `PATCH /topics/{id}`:

- **Numerical Analysis:** Numerical analysis proper: the design and analysis
  of numerical methods (discretizations, time integrators, numerical linear
  algebra, solvers and preconditioners, error estimates, stability and
  convergence proofs, with computational experiments). Not: pure PDE or
  functional-analysis theory where numerics only checks the results, quantum
  algorithms and complexity, statistics and inverse problems, or ML and
  systems papers that happen to be cross-listed.
- **PINNs:** Physics-informed neural networks as the method: new PINN
  architectures, losses and training schemes, their failure modes (spectral
  bias, causality, stiffness), their theory, and applications where a PINN is
  the main tool. Not: papers that use PINNs only as a baseline or a data
  generator, mention them in passing, or are about other neural or
  physics-informed PDE methods with no PINN at the core.
- **RLHF:** Reinforcement learning from human feedback and preference-based
  alignment of language models as the subject: reward modeling, preference
  optimization, reward hacking, the theory of learning from preferences, and
  studies of the human feedback itself. Not: papers that cite RLHF only as
  background or motivation, safety or fine-tuning methods that do without
  human feedback, or general LLM work.

### 5.5 Relevance as a coarse gate (2026-10-16): rejected

The idea from §5.4: if Gemma can't order on-topic papers, use relevance only
to separate on-topic from off-topic (rating ≥ 6) and rank on-topic papers
by h-index, novelty and venue. It's implemented as
`score_papers(..., relevance_mode=GATED)` and measured offline with
`python -m evals.ranking`. Each topic's labeled papers are ranked as one email
batch with their real stored signals. h-index was looked up first for the
labeled papers, raising coverage from 12/47 to 43/47. Reports are
`evals/results/2026-10-16_1728_ranking_{combined,rate-only}.json`.

Mean over the three topics, relevance from the production (combined) call:

| Scheme | Spearman vs reference | NDCG@5 | NDCG@10 | Off-topic in top 10 |
|---|---|---|---|---|
| graded (current) | **0.54** | **0.86** | 0.87 | 1.3 |
| gated | 0.31 | 0.79 | 0.84 | 1.3 |
| signals only (no relevance) | 0.03 | 0.77 | 0.77 | 2.0 |
| relevance only | 0.67 | 0.85 | **0.92** | 0.7 |

- **The gate is worse on every topic.** For PINNs, the 95% bootstrap interval
  for gated − graded NDCG@10 is (−0.27, −0.01). For Numerical Analysis and
  RLHF it crosses zero. Rate-only ratings give the same ordering of schemes.
- **Why: the other signals carry no topical information.** Without
  relevance, h-index + novelty + venue correlate with the reference labels at
  ρ 0.03. So Gemma's weak 7-vs-9 distinction (on-topic ρ ≈ 0.27) is still the
  best topical signal available. The gate discards it in favour of signals
  that don't track topicality.
- **The default stays `GRADED`.** The gated mode stays in `ranking.py`, tested
  but unused, so the comparison can be rerun.
- **Caveat:** the reference labels measure topical centrality only. h-index
  and novelty are meant to capture something else (standing, freshness),
  which these labels can't reward. The "relevance only" row shows they cost
  topical precision (NDCG@10 0.92 → 0.87), but whether they earn it back in
  usefulness is untested. That needs reader feedback, which is the same gap
  §5.4 points to.

### 5.6 Phase 4 results (2026-10-22): history-aware overviews

`python -m evals.overviews` replays the 25 most recent completed digests
(14 Numerical Analysis, 11 PINNs) that have at least 3 summarized papers and
2 earlier overviews. Each is written without history (the old prompt) and
with the history the activity would have had at the time. Two automatic
measures: the overview's highest embedding similarity to the topic's
previous overviews (lower = less repetition), and numbers absent from the
writer's material. A blind A/B file was judged by Claude (not the project
owner) for usefulness and factual errors before the key was opened. Reports:
`evals/results/2026-10-22_1230_overviews_compare.json` and
`2026-10-22_1237_overviews_avoid_repeats.json`, each with a `review` section.

| | Similarity to previous overviews | Preferred over plain | Overviews with a factual error | Minor overstatements |
|---|---|---|---|---|
| plain (no history) | 0.85 | – | 0 / 25 | 0 |
| **compare**: lead with what's new vs. previous overviews + similar earlier papers | **0.77** (less repetitive in 24/25) | 14 vs 9 (2 ties) | **7 / 25** | 8 |
| **avoid_repeats**: see previous overviews only to avoid repeating them; never mention them | 0.81 (less repetitive in 22/25) | **15 vs 2** (8 ties) | **1 / 25** | 4 |

- **Plain overviews really do repeat themselves.** Day after day they open
  with "a broad range of approaches… improving efficiency, accuracy and
  stability". The previous overviews in the review read almost identically.
- **The `compare` style is the most different from earlier overviews, and
  also the most wrong.** Its errors are cross-references: an invented
  "October 2021 digest", a similar-paper link attached to the wrong paper, a
  "departure from an earlier theoretical emphasis" the history never showed.
  This is the same failure as phase 2's related-work notes: Gemma invents
  relationships when asked to relate things.
- **`avoid_repeats` keeps most of the gain without that risk.** Readers get
  overviews about *this* batch's specifics (quaternion tensors, boson stars,
  troubled-cell indicators) instead of field-level boilerplate. Its one
  error called a poker-tournament algorithm "physically constrained". The
  prompt also tells it not to refer to papers by number, which both plain
  and `compare` overviews did, though email readers can't see the numbers.
- **Shipped:** `RAG_OVERVIEW_HISTORY=true` with style `avoid_repeats`, using the
  last 3 overviews (`rag_overview_history_count`). `compare` stays available
  via `rag_overview_history_style` for re-evaluation, e.g. with a stronger model.
- **Caveat:** round 2's blinding was partial. Its plain overviews were reused
  from round 1, whose key had already been opened, so the plain side was
  recognizable. The error count is the more objective of the two measures,
  and it's the one the decision rests on.
- **Unsupported numbers are mostly paper indices.** "(paper 19)" accounts
  for nearly all of them. The only real one was the invented "2021".

### 5.7 Phase 5 results (2026-10-22): full-text excerpts

**arXiv's terms, and how the design follows them.**
- [API terms](https://info.arxiv.org/help/api/tou.html): "retrieve, store,
  and use the content of arXiv e-prints for your own personal use, or for
  research purposes" is allowed. Storing *and serving* e-prints needs the
  copyright holder's permission. So full text stays in the local database
  and is never emailed. Emails carry only the model's own summary and the
  arXiv link.
- Rate limit: no more than one request every 3 seconds, over a single
  connection, across all machines. `app.services.arxiv.throttle` is now one
  locked gate for every arXiv call in the worker process. Before this phase
  it wasn't thread-safe, even though topics' digests run concurrently.
- [Bulk data](https://info.arxiv.org/help/bulk_data.html): scripts should use
  `export.arxiv.org`, and the whole corpus should come from S3, not the API.
  Only newly ingested papers are fetched, about 20 a day.
  `arxiv.org/robots.txt` allows `/html` but sets a 15 s crawl delay for
  crawlers, a further reason to use the export host.
- **Before a public launch:** "personal use" covers this self-hosted digest.
  A multi-user service storing full text needs a fresh read of the terms.

**Pipeline.** `fetch_fulltext` (one activity per digest, one paper at a time,
heartbeating) fetches `export.arxiv.org/html/<id>`. A stdlib parser keeps
section titles, paragraphs and figure captions (math as short TeX), and
drops the abstract, footnotes, tables and references. Text is packed into
section-bounded chunks of ≤ 1,200 chars (≤ 80 per paper, about 60 on
average) and stored as `kind="chunk"` rows in `paper_embeddings`. The summary
gets the best chunk for each of three fixed queries: results, method,
limitations. Outcomes are recorded in `papers.fulltext_status`.

**Measurement** (`python -m evals.fulltext`, the 47 reference-labeled papers;
reports `evals/results/2026-10-22_1313_fulltext.json` and `_1333_fulltext.json`,
each with a blind `review`):

| | Plain (abstract only) | Full text, round 1 | Full text, round 2 (shipped) |
|---|---|---|---|
| HTML available | – | 45 / 47 | 45 / 47 |
| Numbers per summary | 0.02 | 2.18 | 1.69 |
| Summaries citing a figure only in the full text | 0 | 22 / 45 | 12 / 45 |
| Summaries with unsupported numbers | 0 | 3 / 45 | **0 / 45** |
| Relevance vs reference (ρ, within 1) | 0.66, 60% | 0.60, 42% | **0.72, 67%** |
| Blind review: preferred over plain (20 papers) | – | 15 vs 4 | **17 vs 3** |
| Blind review: summaries with an error | 0 / 20 | 3 / 20 | 3 / 20 → **2 / 20** after the placeholder guard |
| Latency per paper | 3.6 s | 6.7 s | 7.6 s (+ ~3.2 s fetch) |

- **Round 1** made summaries far more concrete but brought in borrowed and
  invented numbers. Examples: a prior paper's ~80% efficiency presented as
  this paper's, a gap given symbolically "estimated" as 10%, an invented 20%
  RLHF baseline. It also added setup trivia and LaTeX. And with excerpts in
  the same call, ratings got worse.
- **Round 2 changes:** the prompt now says to report a number only when the
  excerpt states it as this paper's own result, never cited work's, never
  computed; to take the headline result from the abstract; and to leave out
  trivia and LaTeX. The "experimental setup" query was dropped (3 excerpts).
  Long formulas become "[formula]". With excerpts present, relevance is rated
  in a separate abstract-only call; rate-only was already the stronger rater
  (§5.1).
- **What remains:** 2 of 20 still misread a number's context: prior work's
  efficiency figure again, and failure-case errors presented as success.
  That's right at the 10% bar. Readers preferred the full-text summaries
  17 to 3. **Shipped on** (`RAG_FULLTEXT=true`), and the project owner confirmed
  keeping it on after seeing this tradeoff (2026-10-22);
  one flag turns it off.
- **Bugs caught by reading the review material:** the formula collapser
  first ate the prose between two short formulas. It now pairs `$…$` left
  to right, with a regression test. And the eval's number matcher missed
  TeX thousands separators (`15{,}000`).

## 6. Phases

| # | Scope | Ships | Size |
|---|---|---|---|
| 0 | Eval harness + hand labels + baseline numbers | `evals/rag.py`, `evals/labels.csv` | S |
| 1 ✅ | pgvector, `paper_embeddings`, `Embedder`, `embed_papers` activity, backfill (see §5.2) | infra only, no change to outputs | M |
| 2 ✅ | **A + B**: related-work context with checked citations, near-dup collapse, novelty ranking signal (see §5.3: LLM notes off, "Similar earlier paper" link instead) | email link, new ranking term | M |
| 3 ✅ | **C**: `topics.description` + few-shot rating anchors (API: `PATCH /topics` accepts description); see §5.4: neither helped, anchors off, descriptions opt-in | no ranking gain | S–M |
| 4 ✅ | **D**: history-aware overviews (see §5.6: shipped in the `avoid_repeats` style) | less repetitive overview paragraph | S |
| 5 ✅ | **E**: full-text chunk retrieval from arXiv HTML (see §5.7: shipped on, 17 vs 3 preferred, 2/20 errors) | more concrete summaries | L |

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
