# Research Digest Agent

[![CI](https://github.com/kian-hekmat/research-digest-agent/actions/workflows/ci.yml/badge.svg)](https://github.com/kian-hekmat/research-digest-agent/actions/workflows/ci.yml)

**A self-hosted research alerting service.** It watches arXiv for new papers on
topics you choose, summarizes and scores each paper with an LLM (OpenAI's
GPT-5.4 mini), ranks them, and emails you one combined digest on your
cadence. Summaries and overviews are retrieval-augmented: each paper's nearest
earlier papers (by embedding, in pgvector) and the topic's recent overviews
inform what the model writes, and so can its full text from arXiv's HTML
rendering (built and evaluated, but off until arXiv allows it). Every
retrieval feature was measured against a baseline before it was switched on.
Everything is orchestrated as durable Temporal workflows, so a crash, reboot, or
sleeping laptop delays a digest instead of losing it.

> Built as a realistic backend/AI engineering project: an HTTP API, a relational
> schema with migrations, durable workflow orchestration, external API
> integration, LLM summarization with retrieval (RAG) over a vector store, an
> offline evaluation harness, scheduled email delivery, and a 530-test suite
> running in CI.

**Status (2026-11-13): being turned into a small public service.** The plan is
[`docs/public-launch-plan.md`](docs/public-launch-plan.md) (v2.1): free
twice-weekly digests for US readers, capped at 200 subscribers and $20/month of
OpenAI spend. There is no public signup yet; the service still runs for its
owner.

| Step | What | State |
|---|---|---|
| Phase 1 | Subscriber records, shared topics, admin-token lockdown | Live |
| 2a | OpenAI spend log, monthly budget guard, owner alerts | Live |
| 2b | OpenAI embeddings (`text-embedding-3-small`), recalibrated thresholds | Live |
| 2c | Three passes on Eastern time (ingest, send prep, send), stored per-topic editions, a rating rubric audited across fields | Live since 2026-11-13; first full cycle Mon 2026-11-16 |
| 2d | Picks-first email and rewritten summary/overview prompts | Planned |
| 2e | Cost and load rehearsal | Planned |
| 3–6 | Public signup, website, production host (Hetzner), legal and launch | Planned |

Each phase's implementation plan, with its outcome, is in
[`docs/superpowers/plans/`](docs/superpowers/plans/).

---

## At a glance

| | |
|---|---|
| **What it does** | arXiv → full text + embeddings → retrieval-augmented LLM summary and relevance rating → ranked, per-topic email digests |
| **Backend** | Python 3.12, FastAPI, Pydantic, SQLAlchemy 2, PostgreSQL 16 + pgvector, Alembic |
| **Orchestration** | Temporal (workflows, activities, retry policies, cron schedules) |
| **AI / LLM** | OpenAI `gpt-5.4-mini` for summaries, ratings and overviews; OpenAI `text-embedding-3-small` (768-dim) for embeddings; Ollama optional for local development |
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

Each subscriber chooses topics and a cap on papers per topic. Every subscription
is currently twice weekly; the weekly and biweekly cadences still exist but are
admin-only, and the public launch will offer twice weekly only. When the cap cuts a topic down, the papers kept are the
highest-ranked, not just the newest. A paper that is a near-duplicate of another
in the same section is folded under it ("Also (near-duplicate): ...") instead of
taking a slot. Only the model's own summary and links to arXiv go out; the
paper's text never does.

---

## Skills demonstrated

**Backend & API design**
- REST API with FastAPI: typed request/response models, validation, meaningful
  status codes (201/204/401/404/409/422), partial updates via `PATCH`
- Admin-token auth on every route except `/health`: constant-time comparison,
  fails closed when no token is configured, and a test that sweeps every route
  so a new one can't ship unprotected by accident
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
- 13 hand-reviewed Alembic migrations, including a Postgres enum change that has
  to commit outside the migration's transaction, a reversible downgrade that
  rebuilds the enum type, and a data migration that moves emails into a
  `subscribers` table (case-insensitive) and merges topics whose queries
  normalize the same, rehearsed on a copy of the live database before it ran

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
- Swappable summarization backends behind one interface: the OpenAI API by
  default, or a local model through Ollama, with no change to the pipeline
- Swappable embedding backends too (OpenAI or a local Ollama model), with
  similarity thresholds calibrated per model on the real corpus, and a
  side-by-side backfill so switching models never mixes two models' scales
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
- 530 tests against a real Postgres built by running the migrations, so model
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
              │ arXiv API + HTML │              │ OpenAI API       │   │ SMTP relay │
              │ OpenAlex /       │              │ gpt-5.4-mini     │   │ / Mailpit  │
              │ Semantic Scholar │              │ text-embedding-  │   └────────────┘
              └──────────────────┘              │ 3-small          │
                                                └──────────────────┘
```

- **`api`** is fast and synchronous: CRUD on topics and subscriptions, and
  starting workflows, all behind an admin token. It never does slow external
  work itself.
- **`worker`** runs every workflow and activity. All external calls (arXiv, the
  LLM and embedding model, scholarly APIs, SMTP) happen here, each behind a
  retry policy.
- **PostgreSQL** holds everything, including the vector index: paper and
  full-text chunk embeddings live in `paper_embeddings` (pgvector, HNSW), so
  there is no separate vector database to run.
- **OpenAI writes the text and the embeddings**: summaries, relevance ratings
  and overviews with `gpt-5.4-mini`, embeddings with `text-embedding-3-small`.
  Every call is priced and recorded against the monthly budget. The API key
  lives only in the gitignored `.env`.
- **Ollama is optional**, for running everything locally: set
  `SUMMARY_BACKEND=ollama` and/or `EMBEDDING_BACKEND=ollama`. It runs natively
  on the Mac (for the GPU), reached from containers at `host.docker.internal`.
- **Temporal persists its state** to a volume, so schedules and in-flight
  workflows survive restarts and resume where they stopped.

---

## How it works

Three passes, all on Eastern time: a cheap daily **ingest**, a twice-weekly
**send prep** that does the expensive work only for the papers that will be
emailed, and a **send** that only assembles stored content.

| Schedule | When (America/New_York) | Workflow | What it does |
|---|---|---|---|
| `daily-topic-digests` | Daily 21:30 | `RunAllTopicDigestsWorkflow` | Ingest: fetch, embed and rate each subscribed topic's new papers |
| `send-prep` | Mon and Thu 01:00 | `PrepEditionsWorkflow` | One edition per topic for the 08:00 send: rank, summarize the top K, write the overview |
| `weekly-digest-emails` | Mon and Thu 08:00 | `SendDigestEmailsWorkflow` | Email each due subscriber their topics' editions |

The worker creates or updates all three on startup. A run missed while the
host was down or asleep fires when it's back, and keeps the slot it was
scheduled for.

### 1. Daily ingest (`DigestWorkflow`)
Runs daily at 21:30 Eastern for every topic with at least one active
subscription from a confirmed subscriber, and on demand for any topic via
`POST /digests/{topic_id}`. Topics run in batches of `FANOUT_BATCH_SIZE`.

1. **Fetch** new papers from arXiv. Free-text topics are sent as exact-phrase
   searches; queries like `cat:math.NA` track a whole arXiv category. Every
   arXiv request goes through its own task queue, served one at a time.
2. **Ingest**: upsert papers by arXiv id and skip any the topic already has.
3. **Embed** new papers (and the topic's query) into pgvector and compute retrieval
   signals: novelty, near duplicates, and the closest earlier paper (shown in
   the email as "Similar earlier paper" when it's close enough).
4. **Rate** each new paper's relevance to the topic (1–10, rate-only prompt),
   at most `LLM_MAX_CONCURRENCY` calls at a time. If every rating in a run
   fails, the owner gets one alert that day.
5. **Finalize**: mark the digest completed and advance the topic's watermark.

### 2. Send prep (`PrepEditionsWorkflow`)
Runs Mondays and Thursdays at 01:00 Eastern and stores one **edition** per
topic for the coming send: the window of papers ingested since the topic's
previous edition, ranked, with an overview.

1. **Re-rate** papers the ingest left unrated (a failure, or the budget).
2. **Enrich**: look up author h-indices (Semantic Scholar with a key, OpenAlex
   without), done now because both lag arXiv by a day or more.
3. **Rank** the window and take the top K, the largest paper count any
   subscriber of the topic asked for (at least 5).
4. **Full text** for the top K only, when `RAG_FULLTEXT` is on (off by
   default, pending arXiv's permission): fetch the arXiv HTML, split it into
   section-aware chunks and embed them.
5. **Summarize** the top K papers that don't have a summary yet.
6. **Overview**: one LLM-written paragraph over the top K. The writer also
   sees the topic's last three overviews, so it says what is specific to this
   edition instead of re-describing the field each time.
7. **Store** the edition. If every summary failed, the owner gets an alert.

### 3. Email delivery (`SendDigestEmailsWorkflow`)
Runs Mondays and Thursdays at 08:00 Eastern. Its own steps make no LLM,
arXiv or h-index call.

1. **Find due subscriptions**, each judged against its own cadence. Only active
   subscriptions of confirmed subscribers who haven't unsubscribed and aren't
   waitlisted count, the same rule that decides which topics are fetched.
2. **Use each topic's edition** for the slot. A topic with none (the host
   slept through 01:00, so prep and send fire together on wake) has its prep
   run first, or waited for if the scheduled prep is already on it. Only a
   prep that fails gets a fallback edition, ranked without AI text.
3. **Cap and render** one email per recipient, with a section per topic, each
   cut to that subscriber's own cap, in the edition's order. Near-duplicates
   fold under the paper they duplicate; papers with a close earlier paper get
   a "Similar earlier paper" link.
4. **Send, then advance watermarks**, only for topics that were actually in a
   confirmed send.

### 4. Ranking (`app/services/ranking.py`)
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

The tables below were first measured on the previous summarization model, Gemma
3 12B (`gemma3:12b`). The main evals were re-run on `gpt-5.4-mini` on 2026-11-05
([details](docs/rag-plan.md)):

| Measure | `gemma3:12b` | `gpt-5.4-mini` |
|---|---|---|
| Rating vs reference labels, Spearman ρ (higher is better) | 0.71 | 0.77 |
| Rating, on-topic papers only, ρ | 0.27 | 0.37 |
| Rating spread, standard deviation | 2.47 | 2.85 |
| Ranking NDCG@10, shipped graded scheme | 0.874 | 0.923 |
| Latency, summary with rating | 3.7 s | 1.6 s |
| Latency, full-text summary | 7.6 s | 2.7 s |
| Numbers per full-text summary | 1.69 | 1.20 |
| Summaries with numbers only in the full text, of 45 | 12 | 5 |
| Full text preferred over abstract-only, of 20 | 17 vs 3 | 17 vs 1, 2 ties |
| Full-text summaries with a factual error, of 20 | 3 | 0 |

The evidence below stays as measured on Gemma. Each feature's on/off setting
held up where it was re-tested, but related-work notes and rating anchors were
not re-tested. The overview review was partial and its preference count isn't
usable, because the two versions were recognizable by their opening words.

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
Excerpts are sent to OpenAI as part of each summary prompt, along with titles
and abstracts. A multi-user deployment would need a fresh read of those terms.

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
| Catch-up emails went out half-empty | After a laptop woke, the missed digest and email runs fired at the same moment and raced | Sends now assemble stored editions; a topic with no edition gets a fallback built from its window |
| A reboot left everything down | No container restart policies | `restart: unless-stopped` on every service, colima started at login |
| Tests called the developer's real LLM | A cached settings object loaded the local `.env` before the test harness disabled it | Fixed load order; a guard test asserts the session is isolated |
| One topic was mostly off-topic papers | 25 papers ingested before the exact-phrase search fix had stayed linked to RLHF and skewed ratings and evals | Unlinked them (kept as digest history); retrieval now ignores papers no topic links to |
| Switching to the pgvector image warned of a collation mismatch | The obvious `pgvector:pg16` tag is built on an older glibc than `postgres:16`, so text indexes could sort differently | Pinned the `-trixie` build, which matches the original glibc, before anything was written |
| arXiv's rate limit could be exceeded | The request throttle wasn't thread-safe, and topics' digests run concurrently | One locked, process-wide gate for every arXiv call; a test checks spacing across threads |
| Full-text excerpts lost sentences | The formula collapser matched from one formula's closing `$` to the next one's opening `$`, deleting the prose between | Pair `$…$` left to right; caught while reading an eval's inputs, before shipping |

---

## Testing

```bash
venv/bin/python -m pytest -q        # 521 tests
```

| Layer | How |
|---|---|
| Database | Real PostgreSQL with pgvector; schema built by running the Alembic migrations, torn down per test |
| Data migrations | Old-schema rows seeded with raw SQL, then upgrade, downgrade and upgrade again, asserting which rows survive a merge, not just counts |
| Auth | Every route except `/health` is swept with no token, a wrong token and an empty one, and must return 401 |
| Retrieval | Real vector queries on synthetic embeddings whose similarities are known by construction (unit vectors at chosen angles) |
| Workflows | Real workflows and activities on Temporal's **time-skipping** test server, so retry backoff is fast-forwarded |
| Schedules | Temporal's local dev server (the time-skipping server has no Schedule API) |
| HTTP integrations | `respx` mocks for arXiv (API and HTML), OpenAI, Ollama (chat and embed), OpenAlex and Semantic Scholar; no test reaches a real external service |
| Prompts | Exact request bodies asserted, including that every retrieval feature leaves the original prompt byte-for-byte unchanged when it has nothing to add |
| Email | Full MIME output checked (multipart parts, UTF-8 round trip, well-formed HTML, escaping) |
| Config | Static checks on `docker-compose.yml`: persistence, restart policies, worker env |

CI (GitHub Actions) runs the full suite against a fresh Postgres + pgvector
service container on every push. Model-quality evaluations live in `evals/`
and run on demand against the real model (see
[Retrieval and evaluation](#retrieval-and-evaluation)); only their pure metric
helpers are unit-tested in CI.

---

## Running locally

**Prerequisites:** Docker (colima on macOS) and an
[OpenAI API key](https://platform.openai.com/api-keys). [Ollama](https://ollama.com)
only if you want local models (`EMBEDDING_BACKEND=ollama`, `SUMMARY_BACKEND=ollama`).

```bash
# 1. Configure
cp .env.example .env              # then set OPENAI_API_KEY and ADMIN_TOKEN in .env; email goes to Mailpit

# 2. Start the stack: Postgres (+ pgvector), Temporal, Mailpit, API, worker
docker-compose up -d --build
docker-compose exec api alembic upgrade head

# 3. Optional, for an existing database: embed papers ingested before retrieval
docker-compose exec worker python -m app.backfill_embeddings
```

| URL | What |
|---|---|
| http://localhost:8000/docs | Interactive API docs (Swagger) |
| http://localhost:8233 | Temporal UI: workflow runs, retries, schedules |
| http://localhost:8025 | Mailpit: every email sent locally lands here |

### Example

```bash
# Every endpoint except /health needs the admin token (ADMIN_TOKEN in .env)
export ADMIN_TOKEN=...
H="X-Admin-Token: $ADMIN_TOKEN"

# Track a topic (exact phrase), or a whole arXiv category with "cat:math.NA"
curl -H "$H" -X POST localhost:8000/topics -H 'Content-Type: application/json' \
  -d '{"name": "RLHF", "query": "reinforcement learning from human feedback"}'

# Say what you do and don't want from it; the relevance rater reads this
curl -H "$H" -X PATCH localhost:8000/topics/<topic_id> -H 'Content-Type: application/json' \
  -d '{"description": "Reward modeling and preference optimization; not papers that only cite RLHF"}'

# Subscribe: twice weekly, the 10 best papers per email
curl -H "$H" -X POST localhost:8000/topics/<topic_id>/subscriptions -H 'Content-Type: application/json' \
  -d '{"email": "you@example.com", "cadence": "twice_weekly", "max_papers": 10}'

# Run a digest now, then poll it (pending -> completed)
curl -H "$H" -X POST localhost:8000/digests/<topic_id>
curl -H "$H" localhost:8000/digests/<digest_id>
```

### API

| Method & path | Purpose |
|---|---|
| `POST /topics`, `GET /topics`, `GET /topics/{id}`, `PATCH /topics/{id}`, `DELETE /topics/{id}` | Manage tracked topics (`PATCH` edits the description). Topics are unique by normalized query, so `RLHF` and `"rlhf"` are one topic (409) |
| `POST /digests/{topic_id}`, `GET /digests`, `GET /digests/{id}` | Trigger and inspect digest runs |
| `GET /papers/by-topic/{topic_id}` | Papers ingested for a topic |
| `GET /admin/usage?month=YYYY-MM` | OpenAI spend for a month (UTC, default the current one): total against the budget, the spend-guard state, and breakdowns by purpose and by topic |
| `POST`/`GET /topics/{id}/subscriptions` | Subscribe an email to a topic (an admin-added subscriber counts as confirmed), list subscribers |
| `PATCH`/`DELETE /subscriptions/{id}` | Change cadence, cap or active flag; unsubscribe |
| `GET /health` | Health check (the only route without the admin token) |

Every route except `/health` needs the `X-Admin-Token` header and returns 401
without it. The public signup, confirmation and unsubscribe pages come in a later
phase of the launch plan.

---

## Configuration

All settings are environment variables (see [`.env.example`](.env.example)).

| Variable | Default | Purpose |
|---|---|---|
| `ADMIN_TOKEN` | unset | Required for every API route except `/health`; when unset, the API refuses everything. Generate one with `python -c "import secrets; print(secrets.token_urlsafe(32))"` and keep it in `.env` |
| `SUMMARY_BACKEND` | `openai` | `openai` for summaries through the OpenAI API; `ollama` for a local model; `none` to send digests without AI text |
| `OPENAI_API_KEY` | unset | Required with `SUMMARY_BACKEND=openai`. Keep it in `.env`, which is gitignored; never commit it |
| `OPENAI_MODEL` | `gpt-5.4-mini` | Model for summaries, ratings and overviews. Must accept `reasoning_effort: none` |
| `OPENAI_MONTHLY_BUDGET_USD` | `20` | Monthly OpenAI cap, enforced in the app: alert at 80%, AI text paused at 100% until the 1st (UTC). Every call is recorded in `llm_usage` |
| `ALERT_EMAIL` | unset | Where spend alerts are emailed; while unset they're only logged |
| `OPENAI_PRICES` | `gpt-5.4-mini` and `text-embedding-3-small` | USD per 1M tokens per model, as JSON. A model with no price can't be called, so spend is never unrecorded |
| `OLLAMA_BASE_URL` | `http://host.docker.internal:11434` (in Docker) | Where the worker reaches Ollama, for embeddings and the `ollama` backend |
| `OLLAMA_MODEL` | `gemma3:12b` | Summary model with `SUMMARY_BACKEND=ollama` (`ollama pull gemma3:12b`, ~8 GB) |
| `SMTP_HOST` / `SMTP_PORT` / `SMTP_USERNAME` / `SMTP_PASSWORD` / `SMTP_USE_TLS` / `SMTP_FROM_ADDRESS` | Mailpit | Outgoing email |
| `SEMANTIC_SCHOLAR_API_KEY` | unset | Optional; without it, h-indices come from OpenAlex |
| `ARXIV_MAX_RESULTS` / `ARXIV_LOOKBACK_DAYS` | `25` / `7` | Fetch size and publication-lag window |
| `ARXIV_PAGE_DELAY` | `3.0` | Minimum seconds between any two arXiv requests (search or full text) |
| `RAG_ENABLED` | `true` | Embeddings and every retrieval feature (also off when `SUMMARY_BACKEND=none`) |
| `EMBEDDING_BACKEND` / `EMBEDDING_MODEL` | `openai` / `text-embedding-3-small` | Where embeddings come from. Requested at 768 dimensions; a model needs a `RETRIEVAL_CALIBRATION` row (the worker refuses to start without one). `ollama` / `embeddinggemma` runs locally |
| `RAG_FULLTEXT` | `false` | Full-text excerpts in the top K's summaries (off until arXiv allows it) |
| `LLM_MAX_CONCURRENCY` | `8` | LLM calls in flight per workflow (ratings at ingest, summaries at prep) |
| `FANOUT_BATCH_SIZE` | `10` | Topics whose ingest or prep run at once |
| `WORKER_ACTIVITY_THREADS` | `20` | Activities the worker runs at once (arXiv requests are separate, one at a time) |
| `RAG_OVERVIEW_HISTORY` | `true` | Overviews see the topic's last 3 overviews so they don't repeat them |
| `RAG_RELATED_NOTES` | `false` | LLM-written related-work notes (measured: 26% wrong) |
| `RAG_RATING_ANCHORS` | `false` | Reference-rated few-shot examples for the relevance rater (measured: worse) |

Every field of `Settings` in [`app/config.py`](app/config.py) can be overridden
the same way. Retrieval thresholds (the "similar earlier paper" floor, near
duplicates, novelty anchors, similar-topic suggestions) are calibrated per
embedding model in `RETRIEVAL_CALIBRATION`, because cosine scales differ between
models; `app/config.py` explains where each value came from, and using a model
with no calibration row fails rather than borrowing another model's numbers.

---

## Project structure

```
app/
├── main.py                 FastAPI app
├── auth.py                 admin-token check, and the list of public paths
├── routers/                topics, papers, digests, subscriptions endpoints
├── models.py, schemas.py   SQLAlchemy models, Pydantic schemas
├── crud.py                 database access, incl. the one "active subscription" rule
├── topic_query.py          query normalization and public-input validation
├── services/
│   ├── arxiv.py            arXiv Atom API client
│   ├── summarize.py        OpenAI or Ollama: summaries, ratings, overviews
│   ├── embed.py            OpenAI or Ollama: paper, chunk and topic embeddings
│   ├── retrieval.py        pgvector storage + nearest-neighbour queries
│   ├── fulltext.py         arXiv HTML fetch (rate-gated), parse, chunk
│   ├── ranking.py          multi-signal paper scoring (pure functions)
│   ├── scholar.py          author h-index from Semantic Scholar / OpenAlex
│   └── email.py            digest rendering + SMTP sender
├── temporal/
│   ├── workflows.py        ingest, send prep and send workflows
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
├── labels_diverse.csv      cross-field reference labels (47 papers from 8 unrelated fields)
└── results/                every report the decisions in docs/rag-plan.md rest on
tests/                      530 tests (see Testing)
docs/
├── rag-plan.md             the retrieval plan, measurements and decisions
├── public-launch-plan.md   public launch plan (v2.1); Phase 1 and 2a–2c done
└── superpowers/plans/      one implementation plan per launch phase, with its outcome
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

**Embeddings for retrieval.** Digest runs embed new papers and their topic
into pgvector. To embed everything missing and recompute every paper's signals:
```bash
docker-compose exec worker python -m app.backfill_embeddings
```
Switching models goes in two steps, because signals are stored per paper,
not per model:
1. Backfill the new model with `--embed-only` (with `-e EMBEDDING_BACKEND=...
   -e EMBEDDING_MODEL=...`), which touches no signal.
2. Calibrate it with `python -m evals.embeddings` (see `docs/rag-plan.md` §5.9).
3. Flip the settings, and run the default mode before starting the worker.

**Evaluations.** With the stack up, from the repo root (these call
the configured summary model, so with `SUMMARY_BACKEND=openai` they cost money):
```bash
venv/bin/python -m evals.rag run --variant baseline     # or rag-related, desc, anchors, ...
venv/bin/python -m evals.ranking
venv/bin/python -m evals.overviews --style avoid_repeats
venv/bin/python -m evals.fulltext
```
`python -m evals.rag label` hand-labels the sample in the terminal (stored with
`labeler=human`); `python -m app.import_relevance_labels` loads labels into the
database as reference ratings.

**Full text** (off by default) is fetched once per paper, at send prep and for
the top K only, and the outcome is recorded in `papers.fulltext_status`. There is deliberately no bulk backfill:
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
