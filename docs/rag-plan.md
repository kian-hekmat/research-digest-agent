# Retrieval and prompt evaluations

What was measured, and what each result decided. Condensed on 2026-11-17 from
a 934-line research log; the full log, every raw report (`evals/results/`) and
the review files are in git history before that date.

## How evaluations work

- **`python -m evals.rag run`** rates (and optionally summarizes) a reference-labeled sample.
  - It reports spread, run-to-run stability, Spearman ρ and MAE against the labels, unsupported numbers and latency.
  - There are two label sets:
    - `evals/labels.csv`: 47 topic–paper pairs from Numerical Analysis, PINNs and RLHF;
    - `evals/labels_diverse.csv`: 47 papers from 8 unrelated fields.
  - The labels were written by Claude, not the owner; the `labeler` column says so.
- **`python -m evals.ranking`** replays each topic's labeled papers as one email batch under several scoring schemes, scored by NDCG with bootstrap intervals. It reads relevance from a saved `evals.rag` report, so run that first.
- **`python -m evals.embeddings`** calibrates the similarity thresholds for an embedding model.
- **`python -m evals.email_prompts`** evaluates the picks-first email prompts.
- **Blind reviews:** old and new outputs are shuffled into A/B files and judged before the key is opened.
- **Ship bar:** about 10% factual errors at most, and no regression on the rating eval.
- Reports go to `evals/results/` (gitignored). Every eval calls the real model, so it costs money; none runs in CI.

## 5. Results

### 5.1 Baseline (2026-10-16, `gemma3:12b`)
Rate-only ratings against the reference labels gave ρ 0.71 and MAE 1.45 overall, but only ρ 0.27 among on-topic papers. Gemma separates on-topic from off-topic papers well but barely orders the on-topic ones.

### 5.2 Paper embeddings (2026-10-16, `embeddinggemma`)
- **Scale:** random pairs have a median similarity of 0.41 (p99 0.67), and each paper's nearest neighbour a median of 0.66. Thresholds have to be set on this compressed scale.
- **Quality:** 89% of nearest neighbours share a topic.
- **Shipped:** abstract embeddings, the novelty signal, near-duplicate folding, and a "similar earlier paper" link at cosine ≥ 0.75.

### 5.3 LLM-written related-work notes: removed
Each summary got one sentence relating the paper to an earlier one, with its citations checked against the papers offered.
- **Round 1** (floor 0.65): 4 of 8 notes were wrong.
- **Round 2** (floor 0.72, no lineage claims): 5 of 19 (26%) were wrong.

The citation check never fired. The errors were in what the notes claimed, which no citation check can catch. Off by default, then deleted on 2026-11-16. Emails keep the plain "similar earlier paper" link instead.

### 5.4 Topic descriptions and few-shot rating anchors
Rate-only ρ (overall / on-topic) against the 47 labels:

| Variant | ρ | ρ on-topic |
|---|---|---|
| baseline | **0.71** | 0.27 |
| description | 0.67 | 0.27 |
| anchors | 0.52 | 0.02 |
| description + anchors | 0.60 | 0.19 |

- **Anchors** made ratings worse: with about 20 labels per topic, the nearest anchors are only loosely related. Deleted on 2026-11-16.
- **Descriptions** helped the combined summarize-and-rate call a little (on-topic ρ 0.24 → 0.37), but not ratings overall. They stay an optional, admin-written field.

### 5.5 Relevance as an on/off gate: removed
The idea: treat relevance only as on-topic or not, and rank on-topic papers by h-index, novelty and venue.
- **Gemma:** graded relevance had Spearman 0.54 and NDCG@10 0.87; gated had 0.31 and 0.84.
- **`gpt-5.4-mini`:** NDCG@10 was 0.92 graded against 0.83 gated.

Graded stays. The gated mode was deleted on 2026-11-17.

### 5.6 History-aware overviews (2026-10-22): shipped as `avoid_repeats`
Plain overviews repeated themselves week after week ("a broad range of approaches… improving efficiency, accuracy and stability"). Measured on 25 digests:

| Style | Similarity to previous overviews | Preferred over plain | Factual errors |
|---|---|---|---|
| plain | 0.85 | – | 0 / 25 |
| compare (may relate the batch to earlier overviews and papers) | 0.77 | 14 vs 9 | **7 / 25** |
| **avoid_repeats** (sees earlier overviews only so it doesn't repeat them) | 0.81 | **15 vs 2** | **1 / 25** |

- **compare** invented cross-references, the same failure as §5.3. Deleted on 2026-11-16.
- **Shipped:** `avoid_repeats`, with the last 3 overviews (`RAG_OVERVIEW_HISTORY`).
- **Caveat:** blinding was partial, so the error count carries the decision.

### 5.7 Full-text excerpts (2026-10-22): removed for arXiv's terms
Summaries drew on the paper's results, method and limitations sections, retrieved from arXiv's HTML.
- **Results:** readers preferred them 17 vs 3, and numbers per summary rose from 0.02 to 1.69. 2 of 20 misread a figure's context.
- **Why it was removed:** arXiv's API terms allow e-print content only for personal use or research. When this became a public service, it was turned off, then deleted on 2026-11-16.
- **Restoring it:** requires arXiv's written permission and a re-run of this eval.

### 5.8 Re-measured on `gpt-5.4-mini` (2026-11-05)

| Measure | `gemma3:12b` | `gpt-5.4-mini` |
|---|---|---|
| Rate-only ρ / MAE (n=47) | 0.71 / 1.45 | 0.77 / 1.36 |
| Rate-only, on-topic ρ (n=32) | 0.27 | 0.37 |
| Combined call ρ / on-topic ρ | 0.68 / 0.24 | 0.78 / 0.42 |
| Run-to-run stability (exact / ρ) | – | 81% / 0.98 |
| Ranking NDCG@10: graded / relevance only | 0.87 / 0.92 | 0.92 / 0.98 |
| Latency, summary with rating | 3.7 s | 1.6 s |

Every on/off decision above held where it was re-tested.

### 5.9 Moving to `text-embedding-3-small` (2026-11-12)
- **Re-embedding:** every paper was embedded side by side for $0.005. At 768 dimensions the two models' similarity scales nearly match.
- **Topic sharing:** 95% of nearest neighbours share a topic.
- **New thresholds,** carried over by quantile and now in `app/config.py`:
  - similar paper: 0.75 → 0.749;
  - near duplicate: 0.9 → 0.92;
  - novelty anchors: 0.85 / 0.45 → 0.838 / 0.459.
- **Review:** all 20 neighbours just above 0.749 were genuinely related.
- **Similar-topic suggestions:** no threshold works ("RLHF" and its own expansion score 0.196), so suggestions were dropped.

### 5.10 Rating rubric audit for public topics (2026-11-12): adopted
The old rubric over-rated papers that only use the topic: 51% of ratings on the 8-field probe were 10. A banded rubric (10 / 8–9 / 6–7 / 4–5 / 2–3 / 1, naming "used as a tool", "motivation" and "a different meaning") cut that to 34%:

| Label set | ρ: old → new | ρ on-topic: old → new |
|---|---|---|
| `labels_diverse.csv` | 0.84 → 0.87 | 0.37 → 0.56 |
| `labels.csv` (two passes) | 0.76 → 0.74 | 0.38 → 0.38 |

- **Adopted** as the rate-only ingest prompt (`_RELEVANCE_BANDS`), owner's call, despite the original set's flat result.
- **The combined summarize-and-rate prompt** keeps its evaluated rubric. Prep uses only that call's summary.
- **Known weakness:** papers that cite the topic only as motivation, or use it as an application, still rate about 8.

### 5.11 Picks-first email prompts, round 1 (2026-11-16): not yet wired in
- **What was built:** a blurb (hook plus what the paper did) and an overview-and-picks call, both returning strict JSON.
- **Eval:** 17 weekly windows and 20 blind blurb pairs, for $0.15.
- **Automatic checks:** no citations of IDs that weren't offered, and no unsupported numbers in overviews. Overviews ran long (mean 123 words against 80–120).
- **Strict review:** failed 3 of 4 gates. Blurbs were preferred 15–5, but overviews overclaimed, cited abstracts that didn't support the claim, and invented comparisons.
- **Owner's call:** tune after launch. The prompts were tightened (keep hedges, no week-level verdicts without support, copy counts exactly, 70–110 words) but not re-run.
- **Next, after launch:**
  - re-run the eval with the reviewer prompt in `evals/email_prompts_review_prompt.md`;
  - add a fact-check pass;
  - choose the model.
- **Model research (2026-11-16):** on Vectara's HHEM leaderboard, larger models were *less* faithful. `gpt-5.4-nano` scored 3.1% unfaithful summaries, against 5.5% for `gpt-5.4-mini`. A nano or mini writer with a nano checker would cost about $2–6 a month at 100 topics.
