# Research Digest Agent

[![CI](https://github.com/kian-hekmat/research-digest-agent/actions/workflows/ci.yml/badge.svg)](https://github.com/kian-hekmat/research-digest-agent/actions/workflows/ci.yml)

**A self-hosted research alerting service.** It watches arXiv for new papers on
topics you choose, summarizes and scores each paper with a locally hosted LLM
(Gemma 3 via Ollama), ranks them, and emails you one combined digest on your
cadence. Summaries and overviews are retrieval-augmented: each paper's full text
(from arXiv's HTML rendering), its nearest earlier papers (by embedding, in
pgvector), and the topic's recent digests inform what the model writes. Every
retrieval feature was measured against a baseline before it was switched on.
Everything is orchestrated as durable Temporal workflows, so a crash, reboot, or
sleeping laptop delays a digest instead of losing it.

> Built as a realistic backend/AI engineering project: an HTTP API, a relational
> schema with migrations, durable workflow orchestration, external API
> integration, local LLM inference with retrieval (RAG) over a vector store, an
> offline evaluation harness, scheduled email delivery, and a 286-test suite
> running in CI.

---

## At a glance

| | |
|---|---|
| **What it does** | arXiv → full text + embeddings → retrieval-augmented LLM summary and relevance rating → ranked, per-topic email digests |
| **Backend** | Python 3.12, FastAPI, Pydantic, SQLAlchemy 2, PostgreSQL 16 + pgvector, Alembic |
| **Orchestration** | Temporal (workflows, activities, retry policies, cron schedules) |
| **AI / LLM** | Gemma 3 12B (`gemma3:12b`) and EmbeddingGemma (`embeddinggemma`) served locally by Ollama: no API key, no per-call cost |
| **Retrieval** | pgvector (HNSW, cosine) over abstract and full-text chunk embeddings |
| **External data** | arXiv Atom API and HTML full text, OpenAlex / Semantic Scholar (author h-index) |
| **Email** | SMTP (Gmail or any relay), Mailpit for local development |
| **Infrastructure** | Docker Compose (5 services), colima on macOS, GitHub Actions CI |
| **Testing** | pytest, real Postgres + pgvector, Temporal time-skipping test server, respx HTTP mocking |
| **Evaluation** | Offline harness (`evals/`): reference labels, rank correlation, NDCG, blind A/B reviews |

## What a digest looks like

```
Subject: Research digest: 20 new papers (RLHF, PINNs, Numerical Analysis)

===================
RLHF - 10 new papers
===================
<LLM-written overview of what is specific to this batch, written knowing what
 the topic's last few digests already said>

September 28, 2026
------------------
* Using Context Is Not Enough: Test-Time Training for Personalized Reward Modeling
  <3–4 sentence summary: problem, method, setting, headline result - drawing on
   the paper's full text, e.g. "outperforming the strongest baseline by an
   average of 3.94 percentage points">
  <when an earlier paper is close (cosine >= 0.75):
   Similar earlier paper: <its title> <its arXiv link>>
  https://arxiv.org/abs/2609.35109
...
+ 4 more RLHF papers not shown (showing the top 10).
```

Each subscriber chooses topics, a cadence (twice weekly, weekly or biweekly), and
a cap on papers per topic. When the cap cuts a topic down, the papers kept are the
highest-ranked, not just the newest. A paper that is a near-duplicate of another
in the same section is folded under it ("Also (near-duplicate): ...") instead of
taking a slot. Only the model's own summary and links to arXiv go out; the
paper's text never does.

---

## Skills demonstrated

**Backend & API design**
- REST API with FastAPI: typed request/response models, validation, meaningful
  status codes (201/204/404/409/422), partial updates via `PATCH`
- Dependency injection throughout (DB session, Temporal client, SMTP client, LLM
  HTTP client), so every external boundary can be faked in tests

**Data modeling**
- Normalized PostgreSQL schema: many-to-many join tables carrying data
  (per-topic LLM and reference relevance on `topic_paper`, cited similarity on
  the self-referencing `paper_related`), native enums, check constraints,
  cascading foreign keys, timezone-aware timestamps, indexed lookup columns
- pgvector without an extra driver dependency: a small SQLAlchemy `Vector` type
  and one module (`retrieval.py`) that owns all vector SQL; embeddings keyed by
  model so a new model can be backfilled side by side
- 12 hand-reviewed Alembic migrations, including a Postgres enum change that has
  to commit outside the migration's transaction, and a reversible downgrade that
  rebuilds the enum type

**Distributed workflows (Temporal)**
- Deterministic workflow code with all I/O isolated in activities, each with its
  own timeout and retry policy (arXiv, LLM, DB and SMTP each tuned separately)
- Fan-out with child workflows, bounded concurrency (semaphore-capped LLM calls),
  and per-recipient failure isolation
- Cron schedules in a DST-aware time zone, reconciled in place on deploy, with
  catch-up of missed runs after downtime
- New workflow steps added behind `workflow.patched(...)`, so digests already in
  flight during a deploy replay their old history safely; retrieval steps
  degrade (the digest completes without them) rather than fail

**Applied AI / LLM engineering**
- Local inference with Ollama (Gemma 3 12B on Apple Silicon), reached from
  containers via the host gateway
- Prompt design for structured output: a summary plus a 1–10 relevance rating
  in one call, with a tolerant parser that never loses a summary over a
  malformed rating
- Multi-signal ranking: LLM relevance (60%), author h-index (30%, log-scaled),
  novelty from embedding similarity (10%), and a venue-acceptance bonus, with
  median imputation for missing signals
- Retrieval-augmented generation end to end: full-text fetch and section-aware
  chunking, embedding with task prompts, per-query chunk retrieval, a citation
  check on any model-written cross-reference, and guards that strip output the
  model copies from placeholders
- Evaluation-driven decisions: every RAG feature was measured against the
  abstract-only baseline (rank correlation with reference labels, NDCG, blind
  A/B review with an error tally) against a ~10% error bar; five ideas were
  measured, found wanting, and left off, and full-text summaries, right at the
  bar, shipped with the owner's sign-off
  ([Retrieval and evaluation](#retrieval-and-evaluation))

**Reliability engineering**
- Watermark-based incremental ingestion and delivery, advanced only on
  confirmed success, so a failure retries instead of silently dropping content
- Diagnosed and fixed real production-style incidents (see
  [Problems solved](#problems-solved)): non-durable scheduler state, data lost
  to API publication lag, race conditions between concurrent workflows,
  test-environment leakage, and a rate limiter that wasn't thread-safe
- Third-party terms respected by construction: every arXiv request (search and
  full text) goes through one process-wide gate, at most one per 3 seconds, to
  the export host arXiv asks scripts to use

**Testing & CI**
- 286 tests against a real Postgres built by running the migrations, so model
  and migration drift fails the suite
- Workflows run end to end against Temporal's time-skipping server, so minutes
  of retry backoff finish in about a second
- Regression tests for each incident's code fix, mutation-checked: each was
  confirmed to fail with its fix removed

---

## Architecture

```
               ┌──────────────┐  start workflows   ┌─────────────────┐
  HTTP client ─▶│   FastAPI    │───────────────────▶│ Temporal server │
               │    (api)     │                     │ (durable state) │
               └──────┬───────┘                     └────────┬────────┘
                      │ read/write                           │ task queue
                      ▼                                      ▼
               ┌──────────────┐   activities open   ┌─────────────────┐
               │  PostgreSQL  │◀────own sessions────│ Temporal worker │
               │  + pgvector  │                     │                 │
               └──────────────┘                     └──┬──────┬────┬──┘
                                                       │      │    │
                       ┌───────────────────────────────┘      │    └──────────┐
                       ▼                                      ▼               ▼
              ┌──────────────────┐              ┌──────────────────┐   ┌────────────┐
              │ arXiv API + HTML │              │ Ollama (on host) │   │ SMTP relay │
              │ OpenAlex /       │              │ gemma3:12b       │   │ / Mailpit  │
              │ Semantic Scholar │              │ embeddinggemma   │   └────────────┘
              └──────────────────┘              └──────────────────┘
```

- **`api`** is fast and synchronous: CRUD on topics and subscriptions, and
  starting workflows. It never does slow external work itself.
- **`worker`** runs every workflow and activity. All external calls (arXiv, the
  LLM and embedding model, scholarly APIs, SMTP) happen here, each behind a
  retry policy.
- **PostgreSQL** holds everything, including the vector index: paper and
  full-text chunk embeddings live in `paper_embeddings` (pgvector, HNSW), so
  there is no separate vector database to run.
- **Ollama runs natively on the Mac**, not in Docker, so Gemma can use the Apple
  Silicon GPU. Containers reach it at `host.docker.internal`; it stays bound to
  localhost and isn't exposed to the network.
- **Temporal persists its state** to a volume, so schedules and in-flight
  workflows survive restarts and resume where they stopped.

---

## How it works

### 1. Daily digest pipeline (`DigestWorkflow`)
Runs daily at 10:00 Pacific for every topic, and on demand via `POST /digests/{topic_id}`.

1. **Fetch** new papers from arXiv. Free-text topics are sent as exact-phrase
   searches; queries like `cat:math.NA` track a whole arXiv category.
2. **Ingest**: upsert papers by arXiv id and skip any the topic already has.
3. **Embed** new papers with EmbeddingGemma into pgvector and compute retrieval
   signals: novelty, near duplicates, and the closest earlier paper (shown in
   the email as "Similar earlier paper" when it's close enough).
4. **Full text**: fetch each new paper's arXiv HTML (from `export.arxiv.org`,
   one request per 3 s across the whole worker, kept locally and never
   emailed), split it into section-aware chunks and embed them.
5. **Summarize and rate** each new paper with Gemma, at most 4 calls at a time.
   The summary draws on the full-text chunks that best match "main results",
   "method" and "limitations"; the 1–10 relevance rating comes from the
   abstract alone.
6. **Overview**: one LLM-written paragraph synthesizing the batch. The writer also
   sees the topic's last three overviews, so it says what is specific to this
   batch instead of re-describing the field each time.
7. **Finalize**: mark the digest completed and advance the topic's watermark.

### 2. Email delivery (`SendDigestEmailsWorkflow`)
Runs Mondays and Thursdays at 08:00 Pacific.

1. **Refresh first**: run a digest pass and wait for any in-flight digests, so
   the email never goes out before fresh content is ready.
2. **Find due subscriptions**, each judged against its own cadence.
3. **Enrich**: look up author h-indices (Semantic Scholar with a key, OpenAlex
   without), done at send time because both lag arXiv by a day or more.
4. **Rank, cap and render** one email per recipient, with a section per topic
   and papers grouped by arXiv submission date. Near-duplicates fold under the
   paper they duplicate; papers with a close earlier paper get a "Similar
   earlier paper" link.
5. **Send, then advance watermarks**, only for topics that were actually in a
   confirmed send.

### 3. Ranking (`app/services/ranking.py`)
`score = 0.6 × relevance + 0.3 × log-scaled max author h-index + 0.1 × novelty (+ up to 0.08 venue bonus)`

Novelty is how unlike the paper is to earlier topic papers from the last 90 days,
by embedding similarity (pgvector).

A missing signal is replaced with the batch median, so a paper too new to be
indexed neither sinks nor rises. With no signals at all, the order falls back to
newest first. Novelty's weight is deliberately small: papers squarely on a topic
resemble its earlier papers, so novelty pulls slightly against relevance. An
alternative that used relevance only as an on/off-topic gate was implemented
(`relevance_mode=GATED`) and measured worse, so it stays off.

---

## Retrieval and evaluation

The LLM originally saw only one abstract at a time. Five phases added
retrieval, and each was measured on the same set of papers before deciding
its default. The full plan, every number and every review are in
[`docs/rag-plan.md`](docs/rag-plan.md); raw reports are in `evals/results/`.

**What's on**

| Feature | What it does | Evidence |
|---|---|---|
| Paper embeddings | Every paper's abstract embedded with EmbeddingGemma into pgvector | Nearest neighbours share a topic 89% of the time |
| Similar earlier paper | Emails link the closest earlier paper (cosine ≥ 0.75), with no LLM claim attached | Every neighbour reviewed at ≥ 0.75 was genuinely related |
| Novelty in ranking | 10% of the score: how unlike earlier topic papers this one is | Small by design, since novelty correlates with relevance |
| Near-duplicate folding | A near-identical later paper is listed under the earlier one | Wired and tested; no live duplicates yet |
| History-aware overviews | The overview writer sees the topic's last 3 overviews so it stops repeating them | Preferred 15 vs 2 over plain overviews in a blind review; 1 error in 25 |
| Full-text excerpts | Summaries draw on the paper's results, method and limitations sections | Preferred 17 vs 3; numbers per summary 0.02 → 1.69; 2 of 20 misread a figure's context |

**What was measured and left off**

| Feature | Why it's off |
|---|---|
| LLM-written related-work notes | 26% misdescribed the cited paper, even after a second round of prompt work |
| Few-shot rating anchors | Made ratings worse (overall ρ 0.71 → 0.52) with ~20 labels per topic |
| Topic descriptions (as defaults) | No measurable gain; kept as an opt-in, user-written field |
| Relevance as an on/off gate | Ranked worse than graded relevance on every topic |
| Overviews that compare against history | 28% contained an invented cross-reference |

**How it's measured** (`evals/`, run on demand against the local stack, never in CI)
- `evals.rag`: rates and summarizes a stratified, reference-labeled sample
  (`evals/labels.csv`, 47 topic–paper pairs) and reports spread, run-to-run
  stability, Spearman/MAE against the labels, unsupported numbers and latency.
  The labels were written by Claude, not the project owner, and are marked as
  such in a `labeler` column.
- `evals.ranking`: replays each topic's labeled papers as one email batch under
  several scoring schemes; NDCG@k with bootstrap intervals.
- `evals.overviews` and `evals.fulltext`: generate old-vs-new outputs for the
  same inputs, compute automatic checks, and write a blind A/B file. The key is
  kept in a separate report, so the review is judged before unblinding.

**Data use.** Full text comes from arXiv's HTML rendering on
`export.arxiv.org`, fetched only for newly ingested papers (about 20 a day),
at most one request every 3 seconds across the whole worker. It is stored
locally for personal use, as arXiv's terms allow, and never sent in an email.
A multi-user deployment would need a fresh read of those terms.

---

## Problems solved

Real failures found while running this day to day. Each code fix has a
regression test:

| Symptom | Root cause | Fix |
|---|---|---|
| A scheduled digest silently never sent | Temporal's dev server kept schedules in memory, so every container restart reset them with no record of missed runs | Persisted Temporal state to a volume; missed runs now catch up automatically |
| Topics went weeks without new papers | arXiv publishes papers 1–3 days after their timestamp, and a strict "newer than last run" filter dropped them permanently | Look back 7 days past the watermark and de-duplicate by arXiv id |
| Emails repeated one date many times and misdated papers | Papers were grouped by when the job ran, not when they were published | Group by each paper's own submission date; de-duplicate across runs |
| Papers could be skipped forever | Delivery keyed on when a digest *started*, so one still running during a send fell behind the new watermark | Key delivery on completion time, snapshotted before reading |
| Catch-up emails went out half-empty | After a laptop woke, the missed digest and email runs fired at the same moment and raced | The email workflow refreshes and waits for in-flight digests before sending |
| A reboot left everything down | No container restart policies | `restart: unless-stopped` on every service, colima started at login |
| Tests called the developer's real LLM | A cached settings object loaded the local `.env` before the test harness disabled it | Fixed load order; a guard test asserts the session is isolated |
| One topic was mostly off-topic papers | 25 papers ingested before the exact-phrase search fix had stayed linked to RLHF and skewed ratings and evals | Unlinked them (kept as digest history); retrieval now ignores papers no topic links to |
| Switching to the pgvector image warned of a collation mismatch | The obvious `pgvector:pg16` tag is built on an older glibc than `postgres:16`, so text indexes could sort differently | Pinned the `-trixie` build, which matches the original glibc, before anything was written |
| arXiv's rate limit could be exceeded | The request throttle wasn't thread-safe, and topics' digests run concurrently | One locked, process-wide gate for every arXiv call; a test checks spacing across threads |
| Full-text excerpts lost sentences | The formula collapser matched from one formula's closing `$` to the next one's opening `$`, deleting the prose between | Pair `$…$` left to right; caught while reading an eval's inputs, before shipping |

---

## Testing

```bash
venv/bin/python3 -m pytest -q        # 286 tests
```

| Layer | How |
|---|---|
| Database | Real PostgreSQL with pgvector; schema built by running the Alembic migrations, torn down per test |
| Retrieval | Real vector queries on synthetic embeddings whose similarities are known by construction (unit vectors at chosen angles) |
| Workflows | Real workflows and activities on Temporal's **time-skipping** test server, so retry backoff is fast-forwarded |
| Schedules | Temporal's local dev server (the time-skipping server has no Schedule API) |
| HTTP integrations | `respx` mocks for arXiv (API and HTML), Ollama (chat and embed), OpenAlex and Semantic Scholar; no test reaches a real external service |
| Prompts | Exact request bodies asserted, including that every retrieval feature leaves the original prompt byte-for-byte unchanged when it has nothing to add |
| Email | Full MIME output checked (multipart parts, UTF-8 round trip, well-formed HTML, escaping) |
| Config | Static checks on `docker-compose.yml`: persistence, restart policies, worker env |

CI (GitHub Actions) runs the full suite against a fresh Postgres + pgvector
service container on every push. Model-quality evaluations live in `evals/`
and run on demand against the real local model (see
[Retrieval and evaluation](#retrieval-and-evaluation)); only their pure metric
helpers are unit-tested in CI.

---

## Running locally

**Prerequisites:** Docker (colima on macOS) and [Ollama](https://ollama.com).

```bash
# 1. Local LLM (runs natively for GPU access)
brew install ollama
brew services start ollama
ollama pull gemma3:12b            # ~8 GB
ollama pull embeddinggemma        # ~0.6 GB, for retrieval

# 2. Configure
cp .env.example .env              # defaults work out of the box; email goes to Mailpit

# 3. Start the stack: Postgres (+ pgvector), Temporal, Mailpit, API, worker
docker-compose up -d --build
docker-compose exec api alembic upgrade head

# 4. Optional, for an existing database: embed papers ingested before retrieval
docker-compose exec worker python -m app.backfill_embeddings
```

| URL | What |
|---|---|
| http://localhost:8000/docs | Interactive API docs (Swagger) |
| http://localhost:8233 | Temporal UI: workflow runs, retries, schedules |
| http://localhost:8025 | Mailpit: every email sent locally lands here |

### Example

```bash
# Track a topic (exact phrase), or a whole arXiv category with "cat:math.NA"
curl -X POST localhost:8000/topics -H 'Content-Type: application/json' \
  -d '{"name": "RLHF", "query": "reinforcement learning from human feedback"}'

# Say what you do and don't want from it; the relevance rater reads this
curl -X PATCH localhost:8000/topics/<topic_id> -H 'Content-Type: application/json' \
  -d '{"description": "Reward modeling and preference optimization; not papers that only cite RLHF"}'

# Subscribe: twice weekly, the 10 best papers per email
curl -X POST localhost:8000/topics/<topic_id>/subscriptions -H 'Content-Type: application/json' \
  -d '{"email": "you@example.com", "cadence": "twice_weekly", "max_papers": 10}'

# Run a digest now, then poll it (pending -> completed)
curl -X POST localhost:8000/digests/<topic_id>
curl localhost:8000/digests/<digest_id>
```

### API

| Method & path | Purpose |
|---|---|
| `POST /topics`, `GET /topics`, `GET /topics/{id}`, `PATCH /topics/{id}`, `DELETE /topics/{id}` | Manage tracked topics (`PATCH` edits the description) |
| `POST /digests/{topic_id}`, `GET /digests`, `GET /digests/{id}` | Trigger and inspect digest runs |
| `GET /papers/by-topic/{topic_id}` | Papers ingested for a topic |
| `POST`/`GET /topics/{id}/subscriptions` | Subscribe to a topic, list subscribers |
| `PATCH`/`DELETE /subscriptions/{id}` | Change cadence, cap or active flag; unsubscribe |
| `GET /health` | Health check |

---

## Configuration

All settings are environment variables (see [`.env.example`](.env.example)).

| Variable | Default | Purpose |
|---|---|---|
| `SUMMARY_BACKEND` | `ollama` | `ollama` for Gemma summaries; `none` to send digests without AI text |
| `OLLAMA_MODEL` | `gemma3:12b` | Model for summaries, ratings and overviews |
| `OLLAMA_BASE_URL` | `http://host.docker.internal:11434` (in Docker) | Where the worker reaches Ollama |
| `SMTP_HOST` / `SMTP_PORT` / `SMTP_USERNAME` / `SMTP_PASSWORD` / `SMTP_USE_TLS` / `SMTP_FROM_ADDRESS` | Mailpit | Outgoing email |
| `SEMANTIC_SCHOLAR_API_KEY` | unset | Optional; without it, h-indices come from OpenAlex |
| `ARXIV_MAX_RESULTS` / `ARXIV_LOOKBACK_DAYS` | `25` / `7` | Fetch size and publication-lag window |
| `ARXIV_PAGE_DELAY` | `3.0` | Minimum seconds between any two arXiv requests (search or full text) |
| `RAG_ENABLED` | `true` | Embeddings and every retrieval feature (also off when `SUMMARY_BACKEND=none`) |
| `EMBEDDING_MODEL` | `embeddinggemma` | Embedding model; one with a different vector width needs a migration |
| `RAG_FULLTEXT` | `true` | Full-text excerpts in summaries |
| `RAG_OVERVIEW_HISTORY` | `true` | Overviews see the topic's last 3 overviews so they don't repeat them |
| `RAG_RELATED_NOTES` | `false` | LLM-written related-work notes (measured: 26% wrong) |
| `RAG_RATING_ANCHORS` | `false` | Reference-rated few-shot examples for the relevance rater (measured: worse) |

Every field of `Settings` in [`app/config.py`](app/config.py) can be overridden
the same way. The retrieval thresholds there (for example
`RAG_SIMILAR_PAPER_SIMILARITY=0.75`) carry comments explaining where each value
came from.

---

## Project structure

```
app/
├── main.py                 FastAPI app
├── routers/                topics, papers, digests, subscriptions endpoints
├── models.py, schemas.py   SQLAlchemy models, Pydantic schemas
├── crud.py                 database access
├── services/
│   ├── arxiv.py            arXiv Atom API client
│   ├── summarize.py        Gemma via Ollama: summaries, ratings, overviews
│   ├── embed.py            EmbeddingGemma via Ollama: paper embeddings
│   ├── retrieval.py        pgvector storage + nearest-neighbour queries
│   ├── fulltext.py         arXiv HTML fetch (rate-gated), parse, chunk
│   ├── ranking.py          multi-signal paper scoring (pure functions)
│   ├── scholar.py          author h-index from Semantic Scholar / OpenAlex
│   └── email.py            digest rendering + SMTP sender
├── temporal/
│   ├── workflows.py        DigestWorkflow, RunAllTopicDigestsWorkflow, SendDigestEmailsWorkflow
│   ├── activities.py       all I/O: DB, arXiv, LLM, embeddings, full text, SMTP
│   ├── schedule.py         idempotent, self-reconciling cron schedules
│   └── types.py            dependency-free dataclasses passed across the workflow boundary
├── worker.py               Temporal worker entrypoint
├── backfill_summaries.py   one-off: summarize papers waiting to be emailed
├── backfill_embeddings.py  one-off: embed papers ingested before retrieval existed
└── import_relevance_labels.py  one-off: load reference ratings (few-shot anchors)
alembic/versions/           schema migrations
evals/                      offline evaluation harness (not run in CI)
├── rag.py                  ratings + summaries vs reference labels, per variant
├── ranking.py              ranking schemes vs reference labels (NDCG, bootstrap)
├── overviews.py            history-aware overviews: repetition, errors, blind A/B
├── fulltext.py             full-text summaries: concreteness, faithfulness, blind A/B
├── metrics.py              pure metric helpers (unit-tested)
├── labels.csv              reference labels (47 topic-paper pairs, labeler marked)
└── results/                every report the decisions in docs/rag-plan.md rest on
tests/                      286 tests (see Testing)
docs/
├── rag-plan.md             the retrieval plan, measurements and decisions
└── public-launch-plan.md   multi-user launch plan
```

<details>
<summary><strong>Developer notes</strong></summary>

**Migrations**
```bash
alembic revision --autogenerate -m "description"   # after changing app/models.py
alembic upgrade head
```

**Summaries for papers ingested while summarization was off.** A paper is never
re-ingested for the same topic, so fill in any still waiting to be emailed with:
```bash
docker-compose exec worker python -m app.backfill_summaries
```

**Embeddings for retrieval.** Digest runs embed new papers with `embeddinggemma`
(`ollama pull embeddinggemma`) into pgvector. To embed papers ingested before
that, or to backfill a new `EMBEDDING_MODEL` side by side:
```bash
docker-compose exec worker python -m app.backfill_embeddings
```

**Evaluations.** With the stack and Ollama up, from the repo root:
```bash
venv/bin/python -m evals.rag run --variant baseline     # or rag-related, desc, anchors, ...
venv/bin/python -m evals.ranking
venv/bin/python -m evals.overviews --style avoid_repeats
venv/bin/python -m evals.fulltext
```
`python -m evals.rag label` hand-labels the sample in the terminal (stored with
`labeler=human`); `python -m app.import_relevance_labels` loads labels into the
database as reference ratings.

**Full text** is fetched once per paper, at ingestion, and the outcome is
recorded in `papers.fulltext_status`. There is deliberately no bulk backfill:
arXiv asks that the corpus not be downloaded through its API.

**Reboot resilience on macOS.** Every service has `restart: unless-stopped`; for
that to survive a reboot, colima itself must start at login:
`brew services start colima`.

**A Temporal serialization gotcha.** `app/temporal/types.py` deliberately does
*not* use `from __future__ import annotations`. Temporal's payload converter
resolves dataclass field types via `dataclasses.fields()`, which returns
unresolved string annotations under postponed evaluation. That silently breaks
`datetime` fields in dataclasses nested inside `list[...]`: it doesn't raise,
the workflow task fails and retries forever, and it looks exactly like a hang.

</details>
