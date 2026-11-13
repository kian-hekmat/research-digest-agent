# Phase 2b: OpenAI Embeddings — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Move embeddings off the owner's Mac to OpenAI's `text-embedding-3-small` without changing the vector column. Recalibrate every similarity threshold for the new model. Add topic-query embeddings with a similar-topic lookup for Phase 3's signup. Switch over with no moment where live ranking uses one model's similarities with another model's thresholds.

**Architecture:**
- `Embedder` gains an OpenAI backend next to Ollama, with the same spend ledger as `Summarizer`.
- Thresholds stop being loose scalar settings and become a per-model calibration table, so the active model always brings its own numbers. Switching back to `embeddinggemma` stays a pure config change.
- Topic embeddings live in their own table.
- Rollout runs in a fixed order, because paper signals (`prior_similarity`, near-duplicate links, similar-paper links) are stored per paper, not per model:
  1. deploy with the defaults unchanged;
  2. backfill the new model's embeddings side by side, without touching signals;
  3. calibrate from evals on the live corpus;
  4. flip the defaults and recompute signals in one step.

**Tech Stack:** Python 3.12, SQLAlchemy 2.0, Alembic, Postgres 16 + pgvector, httpx + respx, Temporal, pytest. No new dependencies.

**Spec:** `docs/public-launch-plan.md` (v2.1), §3.3 (similar-topic suggestions), §3.4 (embeddings), Phase 2's "OpenAI embeddings" items and evals, §0.1 rows "Embeddings" and "Recalibration". Background on the old thresholds: `docs/rag-plan.md` §5.2–5.3. Read the Phase 2a plan's "Carried forward" (prices validation is done here).

**Not in this plan:** retuning `_MAX_CONCURRENT_SUMMARIES` (2c, with the pipeline's concurrency); turning full text off by default (2c); signup and topic waitlisting that will call `similar_topics` (Phase 3).

## Global Constraints

- Commit straight to `main`; repo's git user; history's message style; no Claude attribution. Push after each task.
- Tests: `venv/bin/python -m pytest`. Baseline 422 passed. Alembic head `b2c3d4e5f6a8`.
- New model: `text-embedding-3-small`, requested with `dimensions` = `settings.embedding_dim` (768), so `vector(768)` is unchanged. Endpoint `POST {openai_base_url}/embeddings`, body `{"model", "input": [...], "dimensions"}`. Price $0.02 per 1M input tokens (already in `openai_prices`).
- Embedding calls are recorded with purpose `"embedding"` and never blocked by the budget (`DbLedger.allow("embedding")` is always True). OpenAI's spend-limit and out-of-credits 429s are not retried.
- No task prompts for OpenAI. The text embedded and stored in `paper_embeddings.content` is `"{title}\n\n{abstract}"` for documents and the bare query for queries. EmbeddingGemma's prompts (`title: … | text: …`, `task: search result | query: …`) apply only on the Ollama backend.
- **Code defaults stay `embedding_backend="ollama"`, `embedding_model="embeddinggemma"` until Task 5.** Tasks 1–4 must not change what the live pipeline does.
- Old calibration, which becomes the `embeddinggemma` row:
  - similar paper 0.75;
  - near duplicate 0.9;
  - related-work floor 0.72;
  - novelty anchors 0.85 (novelty 0) and 0.45 (novelty 1).

## Review Focus

1. **OpenAI returns embeddings out of order.** `data[].index` decides the order, not list position. Pinned in Task 1.
2. **Nothing to embed** (empty list) makes no HTTP call and costs nothing. Pinned in Task 1.
3. **A model with no calibration row** fails loudly before any signal is written, instead of silently using another model's thresholds. Pinned in Task 2.
4. **The side-by-side backfill leaves live signals alone.** `--embed-only` writes new-model embeddings and changes no `prior_similarity`, `near_duplicate_of` or `paper_related` row. Pinned in Task 3.
5. **An `OPENAI_PRICES` override that names only one model** keeps the other default prices, so the embedder never loses its price. A price entry missing `cached_input` is rejected at startup. Pinned in Task 1.

---

### Task 1: OpenAI embedder, recorded like every OpenAI call

**Files:**
- Modify: `app/services/embed.py`, `app/services/retrieval.py` (use the embedder's text methods), `app/config.py` (`embedding_backend`; validated, merged `openai_prices`), `app/services/spend.py` (`SpendLimitReached`), `app/temporal/activities.py` (pass ledger; non-retryable on spend limit), `app/backfill_embeddings.py`, `evals/overviews.py`, `evals/fulltext.py` (pass ledger), `tests/test_spend_coverage.py` (cover `Embedder`), `docker-compose.yml` (`EMBEDDING_BACKEND` on api and worker, default `ollama`), `.env.example`
- Test: `tests/test_embed.py`, `tests/test_spend.py`

**Interfaces:**
- Produces:
  - Setting `embedding_backend: Literal["openai", "ollama"] = "ollama"`.
  - `Embedder(settings=None, http_client=None, *, ledger: SpendLedger | None = None, topic_id: str | None = None)`. `embed(texts) -> list[list[float]] | None` keeps its contract: None when embeddings are off; raises on failure; `[]` for `[]` with no HTTP call.
  - `Embedder.document_text(title: str, abstract: str) -> str` and `Embedder.query_text(query: str) -> str`. Ollama keeps today's EmbeddingGemma prompts; OpenAI uses the formats in Global Constraints. The module-level functions `document_text` / `query_text` are removed, and `retrieval.py` calls the methods instead.
  - OpenAI path, in this order:
    1. With a ledger, check the price exists first (`UnknownModelPrice` raises before any HTTP).
    2. POST in batches of `embedding_batch_size`.
    3. A spend-limit or out-of-credits 429 → `ledger.hard_limit_hit()`, then raise `SpendLimitReached`.
    4. `raise_for_status()`.
    5. `ledger.record(model, purpose="embedding", topic_id, usage_from_openai(body))`, before parsing.
    6. Order the vectors by `data[].index`.
    7. Check the count and width, raising as today.
  - `app/services/spend.py`: `class SpendLimitReached(RuntimeError)`.
  - The activities `embed_papers`, `fetch_fulltext` and `_excerpts` build `Embedder(ledger=DbLedger())`. In `embed_papers`, `SpendLimitReached` becomes `ApplicationError(non_retryable=True)`, which the workflow already treats as "no retrieval this run". `fetch_fulltext` and `_excerpts` already log and skip per paper.
  - `openai_prices`: each entry is validated to have exactly the keys `input`, `cached_input` and `output`, all non-negative numbers. An env override is merged over the defaults (`{**DEFAULT_OPENAI_PRICES, **override}`), so overriding one model never drops another. Export `DEFAULT_OPENAI_PRICES` from `app/config.py`.
  - The AST coverage test also requires `ledger=` on every `Embedder(...)` call outside `app/services/embed.py`.

- [ ] **Step 1: Write the failing tests**, in `tests/test_embed.py`, using respx and the `FakeLedger` pattern from `tests/test_summarize_usage.py`:

```python
OPENAI_EMBED = "http://openai.test/v1/embeddings"

def _openai(**kw):
    return Settings(**{"summary_backend": "openai", "openai_api_key": "k", "openai_base_url": "http://openai.test/v1",
                       "embedding_backend": "openai", "embedding_model": "text-embedding-3-small", **kw})

def _vectors(n, dim=768, order=None):
    order = order or list(range(n))
    return {"data": [{"index": i, "embedding": [float(i)] * dim} for i in order],
            "usage": {"prompt_tokens": 7 * n, "total_tokens": 7 * n}}

def test_openai_request_shape_and_no_task_prompts(): ...
    # body == {"model": "text-embedding-3-small", "input": ["T\n\nA"], "dimensions": 768}; bearer key sent
    # e = Embedder(_openai()); e.document_text("T", "A") == "T\n\nA"; e.query_text("main results") == "main results"

def test_ollama_keeps_embeddinggemma_prompts(): ...
    # Embedder(Settings(embedding_backend="ollama")).document_text("T","A") == "title: T | text: A"

def test_vectors_are_ordered_by_index(): ...
    # response data in order [2, 0, 1] → embed(["a","b","c"]) returns vectors whose first element is 0.0, 1.0, 2.0

def test_empty_input_makes_no_call(): ...
    # route not called; embed([]) == []

def test_batches_and_records_one_row_per_request(): ...
    # embedding_batch_size=2, 5 texts → 3 requests; ledger has 3 records, purpose "embedding", usage input_tokens 14/14/7

def test_wrong_width_raises(): ...      # dimensions ignored by server → 1536-wide vectors → ValueError naming 768
def test_spend_limit_raises_spend_limit_reached_and_reports(): ...   # 429 insufficient_quota → SpendLimitReached; hard_limit_hit once; nothing recorded
def test_unknown_model_price_fails_before_http(): ...                # embedding_model="text-embedding-9" with a ledger
def test_embeddings_are_never_budget_blocked(): ...                  # FakeLedger(allow=False) still embeds (embedder doesn't ask)
```

  In `tests/test_spend.py`:

```python
def test_price_override_merges_with_defaults(monkeypatch):
    monkeypatch.setenv("OPENAI_PRICES", '{"gpt-5.4-mini": {"input": 1, "cached_input": 0.1, "output": 5}}')
    prices = Settings().openai_prices
    assert prices["gpt-5.4-mini"]["input"] == 1.0
    assert prices["text-embedding-3-small"]["input"] == 0.02

def test_price_entry_missing_a_key_is_rejected(monkeypatch):
    monkeypatch.setenv("OPENAI_PRICES", '{"m": {"input": 1, "output": 2}}')
    with pytest.raises(ValidationError):
        Settings()
```

  The existing `test_prices_overridable_from_env` asserts replace semantics; change it to the merge semantics above (ledger the change).

  `tests/test_embed.py::test_document_text_uses_embeddinggemma_document_prompt` imports the module-level `document_text`; move it to `Embedder(Settings(embedding_backend="ollama")).document_text(...)` with the same assertion.

- [ ] **Step 2: Run to verify they fail** — `venv/bin/python -m pytest tests/test_embed.py tests/test_spend.py -v` — Expected: FAIL (unexpected keyword `ledger`, missing `document_text` method, etc.).
- [ ] **Step 3: Implement** per Interfaces; wire every call site; extend the coverage test.
- [ ] **Step 4: Run the full suite** — Expected: all pass. Existing Ollama embedder tests pass unchanged, because Ollama stays the default backend.
- [ ] **Step 5: Commit** — `git commit -m "added an OpenAI embedding backend, recorded like every OpenAI call"`

---

### Task 2: Per-model retrieval calibration

**Files:**
- Modify: `app/config.py`, `app/services/ranking.py`, `app/services/email.py` (`build_topic_section` passes novelty anchors), `app/temporal/activities.py`, `app/backfill_embeddings.py`, `evals/rag.py`, `README.md` (Configuration: replace `RAG_SIMILAR_PAPER_SIMILARITY` mention with `RETRIEVAL_CALIBRATION`), `.env.example`
- Test: `tests/test_calibration.py` (new), `tests/test_ranking.py`

**Interfaces:**
- Produces:
  - `class Calibration(BaseModel)` in `app/config.py`, with fields:
    - `similar_paper: float` — floor for "Similar earlier paper";
    - `near_duplicate: float`;
    - `related_min: float` — floor for related-work candidates;
    - `novelty_repeat: float` — novelty 0;
    - `novelty_unrelated: float` — novelty 1;
    - `topic_suggestion: float | None = None` — None means similar-topic suggestions are off for that model.
  - Setting `retrieval_calibration: dict[str, Calibration]`, default `{"embeddinggemma": Calibration(similar_paper=0.75, near_duplicate=0.9, related_min=0.72, novelty_repeat=0.85, novelty_unrelated=0.45)}`. An env override (`RETRIEVAL_CALIBRATION` JSON) merges per model, like prices.
  - `Settings.calibration -> Calibration`: the entry for `embedding_model`. If it's missing, raise `MissingCalibration(model)` (a `RuntimeError`) with a message saying to run `evals.embeddings`.
  - Remove the settings `rag_similar_paper_similarity`, `rag_near_duplicate_similarity` and `rag_min_similarity`. Every reader uses `settings.calibration.<field>`.
  - `ranking.score_papers(papers, relevance_mode=GRADED, *, novelty_repeat: float = 0.85, novelty_unrelated: float = 0.45)` and `novelty_component(prior_similarity, repeat=0.85, unrelated=0.45)`. The module constants go; the defaults equal the old values, so existing ranking tests pass.
  - `build_topic_section(..., novelty: tuple[float, float] = (0.85, 0.45))`. `gather_digest_content` passes `(cal.novelty_repeat, cal.novelty_unrelated)`.
  - `compute_signals` is unchanged; its callers pass calibration values. `embed_papers` reads `settings.calibration` **before** writing anything, so a missing calibration fails the activity with no partial signal writes.

- [ ] **Step 1: Write the failing tests**, in `tests/test_calibration.py`:

```python
def test_embeddinggemma_calibration_is_the_old_numbers():
    c = Settings(embedding_model="embeddinggemma").calibration
    assert (c.similar_paper, c.near_duplicate, c.related_min, c.novelty_repeat, c.novelty_unrelated) == (0.75, 0.9, 0.72, 0.85, 0.45)
    assert c.topic_suggestion is None

def test_missing_calibration_names_the_model_and_the_fix():
    with pytest.raises(MissingCalibration, match="text-embedding-3-small.*evals.embeddings"):
        Settings(embedding_model="text-embedding-3-small").calibration

def test_env_override_merges_per_model(monkeypatch): ...
    # RETRIEVAL_CALIBRATION='{"m": {...all fields...}}' → both "m" and "embeddinggemma" present

def test_embed_papers_with_uncalibrated_model_writes_no_signals(db_session, monkeypatch): ...
    # settings.embedding_model = "uncalibrated" with a fake enabled embedder; activity raises MissingCalibration;
    # no PaperEmbedding rows and no prior_similarity written
```

  In `tests/test_ranking.py`: `test_novelty_anchors_come_from_the_caller` — the same papers score differently under anchors `(0.95, 0.6)` and `(0.85, 0.45)`, and the defaults match `(0.85, 0.45)`.

- [ ] **Step 2: Run to verify they fail.**
- [ ] **Step 3: Implement**; update every reader of the removed settings (grep `rag_similar_paper_similarity|rag_near_duplicate_similarity|rag_min_similarity|NOVELTY_`).
- [ ] **Step 4: Run the full suite** — Expected: all pass.
- [ ] **Step 5: Commit** — `git commit -m "made retrieval thresholds a per-model calibration"`

---

### Task 3: Topic embeddings, similar-topic lookup, and a backfill that can leave signals alone

**Files:**
- Create: `alembic/versions/c3d4e5f6a7b9_topic_embeddings.py` (`down_revision = "b2c3d4e5f6a8"`)
- Modify: `app/models.py` (`TopicEmbedding`), `app/services/retrieval.py`, `app/temporal/activities.py` (`embed_papers` also embeds its topic), `app/temporal/types.py` (`EmbedPapersInput.topic_id: str = ""`), `app/temporal/workflows.py` (pass `topic_id`), `app/backfill_embeddings.py`
- Test: `tests/test_topic_embeddings.py` (new), `tests/test_retrieval.py`

**Interfaces:**
- Produces:
  - `models.TopicEmbedding` (table `topic_embeddings`): pk (`topic_id` FK `topics.id` `ondelete="CASCADE"`, `model`); `content` Text not null (exactly what was embedded); `embedding` `Vector(EMBEDDING_DIM)` not null; `created_at`.
  - `retrieval.embed_missing_topics(db, embedder, topic_ids: list[str] | None = None) -> int` — embeds `embedder.query_text(topic.normalized_query)` for topics with no row for `embedder.model`; commits; returns how many; 0 when the embedder is disabled.
  - `@dataclass(frozen=True) class TopicMatch: topic_id: str; name: str; normalized_query: str; similarity: float`.
  - `retrieval.similar_topics(db, embedder, query: str, *, min_similarity: float, k: int = 3) -> list[TopicMatch]` — embeds `embedder.query_text(normalize_query(query))` (one call), returns up to `k` topics at or above `min_similarity`, most similar first. An empty list if the embedder is disabled. Phase 3 calls it with `settings.calibration.topic_suggestion` and skips the call when that is None.
  - `embed_papers` activity: after embedding papers, `embed_missing_topics(db, embedder, [input.topic_id])` when `input.topic_id` is set. The workflow passes it, guarded by a new `workflow.patched("embed-topic")` so in-flight digests replay.
  - `python -m app.backfill_embeddings [--embed-only | --signals-only]`:
    - default: today's behaviour, plus topics;
    - `--embed-only`: papers and topics for the configured model, no `compute_signals`;
    - `--signals-only`: just `compute_signals` for every paper, with the configured model's calibration.
    - The two flags are mutually exclusive.

- [ ] **Step 1: Write the failing tests**, using an `AngleEmbedder`-style fake with known geometry (see `tests/test_retrieval.py`):

```python
def test_embed_missing_topics_embeds_each_topic_once_per_model(db_session): ...
def test_topic_embedding_text_is_the_normalized_query(db_session): ...      # topic query ' "RLHF" ' → content "rlhf" (OpenAI-style fake)
def test_similar_topics_ranks_and_filters_by_floor(db_session): ...         # three topics at known angles; floor drops the far one; order by similarity
def test_similar_topics_disabled_embedder_returns_empty(db_session): ...
def test_deleting_a_topic_deletes_its_embedding(db_session): ...
def test_embed_only_backfill_leaves_signals_untouched(db_session, monkeypatch): ...
    # papers with existing prior_similarity / near_duplicate_of / paper_related rows; run main(["--embed-only"]) with a fake
    # embedder for a second model → new PaperEmbedding rows for that model, signals byte-for-byte unchanged
def test_signals_only_backfill_recomputes_with_the_configured_calibration(db_session, monkeypatch): ...
def test_backfill_flags_are_exclusive(): ...                                 # argparse error
```

  Also add a workflow test: a digest run embeds its topic once (a fake embedder records the call), and a second run doesn't re-embed it.

- [ ] **Step 2: Run to verify they fail.**
- [ ] **Step 3: Implement** the migration, model, retrieval functions, activity/workflow wiring and backfill flags (`main(argv: list[str] | None = None)`).
- [ ] **Step 4: Run the full suite** — Expected: all pass.
- [ ] **Step 5: Commit** — `git commit -m "added topic embeddings, a similar-topic lookup and an embed-only backfill"`

---

### Task 4: Deploy, backfill side by side, and calibrate on the live corpus

The live pipeline keeps running on `embeddinggemma` throughout this task.

**Files:**
- Create: `evals/embeddings.py`, `evals/topic_pairs.csv`
- Modify: `app/config.py` (add the `text-embedding-3-small` calibration row), `docs/rag-plan.md` (new §5.9 with the numbers)
- Test: `tests/test_eval_metrics.py` (quantile-mapping helper)

**Interfaces:**
- Produces, in `evals/embeddings.py` (writes JSON to `evals/results/`; read-only on the DB except `llm_usage` through the ledger):
  - `scale --model M`. Over papers with a `kind="abstract"` embedding for M, it reports:
    - cosine percentiles (p10, p50, p90, p99, max) of 3,000 seeded random pairs, and of each paper's nearest neighbour;
    - the neighbours-share-a-topic rate among topic-linked papers;
    - coverage, i.e. the share of linked papers whose nearest earlier, linked neighbour reaches a threshold (passed with `--similar`).
  - `map --from embeddinggemma --to text-embedding-3-small`. Proposes the new model's calibration by quantile mapping, using `metrics.quantile_map(value, from_sample, to_sample) -> float`, a pure function added to `evals/metrics.py`. The mappings:
    - `similar_paper` and `related_min`: map through the nearest-neighbour distributions.
    - `novelty_repeat`: map through the nearest-neighbour distributions.
    - `novelty_unrelated`: map through the random-pair distributions.
    - `near_duplicate`: the old value sits above every observed pair (no real duplicates exist), so the proposal is `max(new-model NN max + 0.01, quantile_map of 0.9)`. That keeps today's "nothing in the corpus is a duplicate" true.
  - `review --model M --at T --n 20`. Writes a Markdown review file of the `n` nearest-neighbour pairs just at or above T, with both titles and the first 300 characters of each abstract, for a related or not-related call.
  - `topics --model M`. Embeds both sides of every row in `evals/topic_pairs.csv` (columns `a,b,label` where label ∈ `same|different`; about 30 rows: acronyms and synonyms of real topics, sibling fields such as "physics-informed neural networks" vs "neural operators", and unrelated pairs). It reports the similarities and proposes `topic_suggestion` as the midpoint between the lowest `same` and the highest `different` pair. If those overlap, it proposes the value that maximises F1 instead and flags it.

- [ ] **Step 1: Write the failing test** for `quantile_map` in `tests/test_eval_metrics.py`: mapping the old sample's 90th percentile value returns the new sample's 90th percentile value; values outside the old sample's range clamp to the new sample's min or max.
- [ ] **Step 2: Run to verify it fails.**
- [ ] **Step 3: Implement** `quantile_map` and `evals/embeddings.py`; write `evals/topic_pairs.csv`. Commit and push: `git commit -m "added embedding calibration evals"`.
- [ ] **Step 4: Deploy Tasks 1–4 code.**
  1. `pg_dump` live to `~/research-digest-backups/`.
  2. `docker-compose stop worker`, then `docker exec research-digest-agent-api-1 alembic upgrade head`, then `docker-compose up -d --build api worker`, away from 10:00 PT daily and Mon/Thu 08:00 PT.
  3. Expected: worker schedules unchanged; behaviour unchanged (defaults are still Ollama).
- [ ] **Step 5: Backfill side by side.**
  - Command: `docker exec -e EMBEDDING_BACKEND=openai -e EMBEDDING_MODEL=text-embedding-3-small research-digest-agent-worker-1 python -m app.backfill_embeddings --embed-only`.
  - Expected:
    - one new-model abstract embedding per paper with an abstract (about 880), plus 3 topic embeddings;
    - `GET /admin/usage` shows `embedding` rows costing about a cent;
    - `prior_similarity` checksums unchanged. Record `select md5(string_agg(coalesce(prior_similarity::text,''), ',' order by id)) from papers` before and after.
- [ ] **Step 6: Run the evals** inside the worker container with the same `-e` overrides:
  - `scale` for both models;
  - `map`;
  - `review` at the proposed `similar_paper`;
  - `topics`.
- [ ] **Step 7: Apply the bars.** Proceed only if all three hold:
  - neighbours-share-a-topic rate ≥ 85% (`embeddinggemma`: 89%);
  - at least 18 of the 20 reviewed pairs are related;
  - `topic_suggestion` separates the `same` and `different` pairs, or misses by at most 2 pairs.

  If any bar fails, stop and report to the owner with the numbers. Do not cut over.
- [ ] **Step 8: Record** the `text-embedding-3-small` row in the `retrieval_calibration` defaults. Write `docs/rag-plan.md` §5.9 with both models' tables, the review tally and the chosen values. Commit and push: `git commit -m "calibrated retrieval thresholds for text-embedding-3-small"`.

---

### Task 5: Cut over to OpenAI embeddings

**Files:**
- Modify:
  - `app/config.py`: defaults `embedding_backend="openai"`, `embedding_model="text-embedding-3-small"`;
  - `docker-compose.yml`: defaults `EMBEDDING_BACKEND=openai` and `EMBEDDING_MODEL=text-embedding-3-small` on api and worker; `OLLAMA_*` stay for the local Ollama backend;
  - `.env.example`;
  - `README.md`: Ollama becomes optional in prerequisites and architecture; "At a glance" says OpenAI embeddings; the Configuration rows.
- Test: `tests/test_calibration.py`, `tests/test_embed.py`

- [ ] **Step 1: Write the failing test:** `Settings()` defaults to `("openai", "text-embedding-3-small")`, and `Settings().calibration` resolves without raising.
- [ ] **Step 2: Run it to verify it fails; then flip the defaults.** Pin `embedding_backend="ollama"` explicitly in any existing test that relies on the Ollama default, and ledger each one. Run the full suite. Expected: all pass. Commit (`git commit -m "switched embeddings to OpenAI text-embedding-3-small"`) but don't push yet.
- [ ] **Step 3: Cut over live, in one sitting, away from scheduled runs.**
  1. `pg_dump` live.
  2. `docker-compose stop worker`.
  3. Push the commit, then `docker-compose up -d --build api worker`.
  4. Immediately run `docker exec research-digest-agent-worker-1 python -m app.backfill_embeddings --signals-only`.

  Expected: signals recomputed for every embedded paper with the new calibration; the "similar earlier paper" coverage roughly matches the `scale` report.
- [ ] **Step 4: Verify after the next daily run.**
  - `llm_usage` has `embedding` rows for the new papers.
  - `paper_embeddings` gains only `text-embedding-3-small` rows.
  - Nothing calls Ollama: the worker logs show no `host.docker.internal:11434` requests.
- [ ] **Step 5: Record the outcome** at the end of this plan: commits, cutover time, the calibration chosen, and coverage before and after. Keep the `embeddinggemma` rows; rolling back is `EMBEDDING_BACKEND=ollama`, `EMBEDDING_MODEL=embeddinggemma` in `.env`, plus `--signals-only`.
