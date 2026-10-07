# Research Digest Agent

[![CI](https://github.com/kian-hekmat/research-digest-agent/actions/workflows/ci.yml/badge.svg)](https://github.com/kian-hekmat/research-digest-agent/actions/workflows/ci.yml)

**A self-hosted research alerting service.** It watches arXiv for new papers on
topics you choose, summarizes and scores each paper with a locally hosted LLM
(Gemma 3 via Ollama), ranks them, and emails you one combined digest twice a week.
Everything is orchestrated as durable Temporal workflows, so a crash, reboot, or
sleeping laptop delays a digest instead of losing it.

> Built as a realistic backend/AI engineering project: an HTTP API, a relational
> schema with migrations, durable workflow orchestration, external API
> integration, local LLM inference, scheduled email delivery, and a 162-test
> suite running in CI.

---

## At a glance

| | |
|---|---|
| **What it does** | arXiv → LLM summary + relevance rating → ranked, per-topic email digests |
| **Backend** | Python 3.12, FastAPI, Pydantic, SQLAlchemy 2, PostgreSQL 16, Alembic |
| **Orchestration** | Temporal (workflows, activities, retry policies, cron schedules) |
| **AI / LLM** | Gemma 3 12B (`gemma3:12b`) served locally by Ollama: no API key, no per-call cost |
| **External data** | arXiv Atom API, OpenAlex / Semantic Scholar (author h-index) |
| **Email** | SMTP (Gmail or any relay), Mailpit for local development |
| **Infrastructure** | Docker Compose (5 services), colima on macOS, GitHub Actions CI |
| **Testing** | pytest, real Postgres, Temporal time-skipping test server, respx HTTP mocking |

## What a digest looks like

```
Subject: Research digest: 20 new papers (RLHF, PINNs, Numerical Analysis)

===================
RLHF - 10 new papers
===================
<LLM-written overview of the batch's common threads and tensions>

September 28, 2026
------------------
* Using Context Is Not Enough: Test-Time Training for Personalized Reward Modeling
  <3–4 sentence summary: problem, approach, headline result>
  https://arxiv.org/abs/2609.35109
...
+ 4 more RLHF papers not shown (showing the top 10).
```

Each subscriber chooses topics, a cadence (twice weekly, weekly or biweekly), and
a cap on papers per topic. When the cap cuts a topic down, the papers kept are the
highest-ranked, not just the newest.

---

## Skills demonstrated

**Backend & API design**
- REST API with FastAPI: typed request/response models, validation, meaningful
  status codes (201/204/404/409/422), partial updates via `PATCH`
- Dependency injection throughout (DB session, Temporal client, SMTP client, LLM
  HTTP client), so every external boundary can be faked in tests

**Data modeling**
- Normalized PostgreSQL schema: many-to-many join tables carrying data
  (per-topic relevance on `topic_paper`), native enums, cascading foreign keys,
  timezone-aware timestamps, indexed lookup columns
- 8 hand-reviewed Alembic migrations, including a Postgres enum change that has
  to commit outside the migration's transaction, and a reversible downgrade that
  rebuilds the enum type

**Distributed workflows (Temporal)**
- Deterministic workflow code with all I/O isolated in activities, each with its
  own timeout and retry policy (arXiv, LLM, DB and SMTP each tuned separately)
- Fan-out with child workflows, bounded concurrency (semaphore-capped LLM calls),
  and per-recipient failure isolation
- Cron schedules in a DST-aware time zone, reconciled in place on deploy, with
  catch-up of missed runs after downtime

**Applied AI / LLM engineering**
- Local inference with Ollama (Gemma 3 12B on Apple Silicon), reached from
  containers via the host gateway
- Prompt design for structured output: a summary plus a 1–10 relevance rating
  in one call, with a tolerant parser that never loses a summary over a
  malformed rating
- Multi-signal ranking: LLM relevance (60%), author h-index (40%, log-scaled),
  and a venue-acceptance bonus, with median imputation for missing signals

**Reliability engineering**
- Watermark-based incremental ingestion and delivery, advanced only on
  confirmed success, so a failure retries instead of silently dropping content
- Diagnosed and fixed real production-style incidents (see
  [Problems solved](#problems-solved)): non-durable scheduler state, data lost
  to API publication lag, race conditions between concurrent workflows, and
  test-environment leakage

**Testing & CI**
- 162 tests against a real Postgres built by running the migrations, so model
  and migration drift fails the suite
- Workflows run end to end against Temporal's time-skipping server, so minutes
  of retry backoff finish in about a second
- Regression tests for each incident, mutation-checked: each was confirmed to
  fail with its fix removed

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
               └──────────────┘                     └──┬──────┬────┬──┘
                                                       │      │    │
                       ┌───────────────────────────────┘      │    └──────────┐
                       ▼                                      ▼               ▼
              ┌──────────────────┐              ┌──────────────────┐   ┌────────────┐
              │ arXiv API        │              │ Ollama (on host) │   │ SMTP relay │
              │ OpenAlex /       │              │ gemma3:12b       │   │ / Mailpit  │
              │ Semantic Scholar │              └──────────────────┘   └────────────┘
              └──────────────────┘
```

- **`api`** is fast and synchronous: CRUD on topics and subscriptions, and
  starting workflows. It never does slow external work itself.
- **`worker`** runs every workflow and activity. All external calls (arXiv, the
  LLM, scholarly APIs, SMTP) happen here, each behind a retry policy.
- **Ollama runs natively on the Mac**, not in Docker, so Gemma can use the Apple
  Silicon GPU. Containers reach it at `host.docker.internal`; it stays bound to
  localhost and isn't exposed to the network.
- **Temporal persists its state** to a volume, so schedules and in-flight
  workflows survive restarts and resume where they stopped.

---

## How it works

### 1. Daily digest pipeline (`DigestWorkflow`)
Runs daily at 07:00 Pacific for every topic, and on demand via `POST /digests/{topic_id}`.

1. **Fetch** new papers from arXiv. Free-text topics are sent as exact-phrase
   searches; queries like `cat:math.NA` track a whole arXiv category.
2. **Ingest**: upsert papers by arXiv id and skip any the topic already has.
3. **Summarize and rate** each new paper with Gemma, at most 4 calls at a time.
   One call returns both the summary and a 1–10 relevance rating.
4. **Overview**: one LLM-written paragraph synthesizing the batch.
5. **Finalize**: mark the digest completed and advance the topic's watermark.

### 2. Email delivery (`SendDigestEmailsWorkflow`)
Runs Mondays and Thursdays at 08:00 Pacific.

1. **Refresh first**: run a digest pass and wait for any in-flight digests, so
   the email never goes out before fresh content is ready.
2. **Find due subscriptions**, each judged against its own cadence.
3. **Enrich**: look up author h-indices (Semantic Scholar with a key, OpenAlex
   without), done at send time because both lag arXiv by a day or more.
4. **Rank, cap and render** one email per recipient, with a section per topic
   and papers grouped by arXiv submission date.
5. **Send, then advance watermarks**, only for topics that were actually in a
   confirmed send.

### 3. Ranking (`app/services/ranking.py`)
`score = 0.6 × relevance + 0.4 × log-scaled max author h-index (+ up to 0.08 venue bonus)`

A missing signal is replaced with the batch median, so a paper too new to be
indexed neither sinks nor rises. With no signals at all, the order falls back to
newest first.

---

## Problems solved

Real failures found while running this day to day, each fixed with a
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

---

## Testing

```bash
venv/bin/python3 -m pytest -q        # 162 tests
```

| Layer | How |
|---|---|
| Database | Real PostgreSQL; schema built by running the Alembic migrations, torn down per test |
| Workflows | Real workflows and activities on Temporal's **time-skipping** test server, so retry backoff is fast-forwarded |
| Schedules | Temporal's local dev server (the time-skipping server has no Schedule API) |
| HTTP integrations | `respx` mocks for arXiv, Ollama, OpenAlex and Semantic Scholar; no test reaches a real external service |
| Email | Full MIME output checked (multipart parts, UTF-8 round trip, well-formed HTML, escaping) |
| Config | Static checks on `docker-compose.yml`: persistence, restart policies, worker env |

CI (GitHub Actions) runs the full suite against a fresh Postgres service
container on every push.

---

## Running locally

**Prerequisites:** Docker (colima on macOS) and [Ollama](https://ollama.com).

```bash
# 1. Local LLM (runs natively for GPU access)
brew install ollama
brew services start ollama
ollama pull gemma3:12b            # ~8 GB

# 2. Configure
cp .env.example .env              # defaults work out of the box; email goes to Mailpit

# 3. Start the stack: Postgres, Temporal, Mailpit, API, worker
docker-compose up -d --build
docker-compose exec api alembic upgrade head
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
| `POST /topics`, `GET /topics`, `GET /topics/{id}`, `DELETE /topics/{id}` | Manage tracked topics |
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
│   ├── ranking.py          multi-signal paper scoring (pure functions)
│   ├── scholar.py          author h-index from Semantic Scholar / OpenAlex
│   └── email.py            digest rendering + SMTP sender
├── temporal/
│   ├── workflows.py        DigestWorkflow, RunAllTopicDigestsWorkflow, SendDigestEmailsWorkflow
│   ├── activities.py       all I/O: DB, arXiv, LLM, SMTP
│   ├── schedule.py         idempotent, self-reconciling cron schedules
│   └── types.py            dependency-free dataclasses passed across the workflow boundary
├── worker.py               Temporal worker entrypoint
└── backfill_summaries.py   one-off: summarize papers waiting to be emailed
alembic/versions/           schema migrations
tests/                      162 tests (see Testing)
docs/                       design notes
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
