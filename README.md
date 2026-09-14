# Research Digest & Alert Agent

A service that tracks research topics on arXiv, summarizes new papers with Claude,
and generates digests on a schedule. Built to rehearse a realistic backend/agent
stack: FastAPI, PostgreSQL, and Temporal-orchestrated workflows.

## Status

**Phase 1 complete:** core API and data model.
- [x] FastAPI app with a real resource model: topics, papers, digests
- [x] PostgreSQL persistence with a proper schema (many-to-many topics↔papers, FK on digests)
- [x] Alembic migrations
- [x] Pydantic validation + meaningful error responses (404s, 409 on duplicate topic)
- [x] Dockerized (docker-compose: app + Postgres)
- [x] Automated tests (pytest, hitting a real test DB)
- [x] CI (GitHub Actions) running tests on every push

**Phase 2 complete:** arXiv ingestion + LLM summarization.
- [x] `search_arxiv()` — arXiv Atom API client (newest-first, `since` watermark filter, request throttling)
- [x] `Summarizer` — per-paper summaries (`claude-haiku-4-5`) + a synthesized digest overview (`claude-sonnet-5`), via the Anthropic SDK
- [x] Per-topic `last_checked_at` high-water mark, advanced only on a successful run
- [x] Tests build the schema from migrations (not `create_all`); one `respx` test over the real Atom parser

**Phase 3 complete:** ingestion/summarization moved into Temporal workflows, with
retry policies on every external call, plus a daily schedule.
- [x] `DigestWorkflow` — Phase 2's pipeline, split into Activities (fetch / ingest
  / summarize / overview / finalize / advance watermark), each with its own retry
  policy; orchestration itself does no I/O
- [x] `RunAllTopicDigestsWorkflow` — the scheduled entrypoint: fans out a
  `DigestWorkflow` child per topic
- [x] A daily Temporal Schedule (06:00 UTC), created idempotently by the worker on
  startup — `overlap_policy=SKIP` so a slow run is never doubled up
- [x] `POST /digests/{topic_id}` now starts a `DigestWorkflow` instead of a FastAPI
  background task — same immediate-`pending`-then-poll contract as before
- [x] A dedicated `worker` container running `python -m app.worker`
- [x] Tests run the real workflows against Temporal's ephemeral, time-skipping test
  server (no Docker needed for tests) — including retry-exhaustion and multi-topic
  fan-out, both of which finish in seconds despite minutes of simulated backoff

## Architecture

```
┌─────────────┐      ┌──────────────┐      ┌─────────────┐
│   Client    │─────▶│   FastAPI    │─────▶│  PostgreSQL │
└─────────────┘      │   (app/)     │      └──────┬──────┘
                      └──────┬───────┘             │
                              │ start_workflow       │ SessionLocal()
                              ▼                      │ (each Activity
                      ┌──────────────┐               │  opens its own)
                      │   Temporal   │◀──────────────┘
                      │    server    │
                      └──────┬───────┘
                              │ polls digest-task-queue
                              ▼
                      ┌──────────────┐      ┌─────────────┐
                      │    worker    │─────▶│    arXiv    │
                      │ (app/worker) │      │  Anthropic  │
                      └──────────────┘      └─────────────┘
```

The `api` and `worker` containers both run the same code against the same
Postgres; `api` only ever talks to Temporal to start workflows and to Postgres
to read/write topics and digests directly (fast, synchronous). All the slow,
retryable, external work (arXiv, Anthropic) happens in `worker`'s Activities.

**Data model:**
- `Topic` — something you're tracking (name + arXiv search query). `last_checked_at`
  is the ingestion high-water mark (NULL = never run → pull everything)
- `Paper` — an ingested arXiv paper; `summary` is the LLM per-paper summary
- `Digest` — a generated batch of papers for a topic, with a `digest_status`
  enum (`pending`/`completed`/`failed`), an `overview` column (LLM-synthesized
  paragraph across the batch), and an `error` column for failure detail
- `topic_paper` — join table, since a paper can match more than one topic
- `digest_paper` — join table, since a paper recurs across a topic's digests and re-runs

All FKs are `ON DELETE CASCADE`; timestamps are timezone-aware with `created_at`/
`updated_at` on every entity; FK and lookup columns (`digests.topic_id`,
`digests.status`, `papers.published_at`) are indexed.

## How a digest runs

`POST /digests/{topic_id}` creates a `pending` digest, starts a `DigestWorkflow`
for it (workflow id `digest-{digest_id}`), and returns immediately. Poll
`GET /digests/{digest_id}` for the result. The same workflow also runs as a
child of `RunAllTopicDigestsWorkflow`, fired daily by the Temporal Schedule.

`DigestWorkflow` (`app/temporal/workflows.py`) is pure orchestration — no DB/HTTP
calls of its own, just a sequence of Activities (`app/temporal/activities.py`):

1. **get_digest_context** — load the topic (query, `last_checked_at`) behind the digest.
2. **fetch_arxiv** — `search_arxiv(query, since=...)`, newest submission first.
   Retried up to 5x with backoff (5s→10s→20s→40s). If every attempt fails: digest
   → `failed`, watermark untouched (so the next run gets the same window).
3. **ingest_papers** — each result upserted by `arxiv_id` (version suffix stripped)
   and linked to the topic and this digest.
4. **summarize_paper** — one Activity call per paper without a summary yet, run
   *concurrently*; each retried independently. A paper whose summary ultimately
   fails is kept without one, and the count lands in `digest.error` — the digest
   still completes.
5. **write_overview** — a 5–6 sentence synthesis across the batch's summaries.
6. **finalize_digest** / **advance_watermark** — status → `completed`, watermark
   moves to when the run started.

`RunAllTopicDigestsWorkflow` (the scheduled entrypoint) lists every topic, creates
a pending digest per topic, and runs a `DigestWorkflow` child for each, concurrently.

Configuration (`app/config.py`, env-driven — see `.env.example`): `ANTHROPIC_API_KEY`
(required for real runs), `SUMMARY_MODEL`, `OVERVIEW_MODEL`, `ARXIV_MAX_RESULTS`,
`ARXIV_PAGE_DELAY`, `TEMPORAL_ADDRESS`, `TEMPORAL_NAMESPACE`, `TEMPORAL_TASK_QUEUE`.

## Running locally

```bash
docker-compose up --build
```

This starts four containers: `db` (Postgres), `temporal` (Temporal dev server),
`api` (FastAPI), and `worker` (the Temporal worker — polls the task queue and
creates the daily schedule on startup, idempotently). Once healthy:
- API docs: http://localhost:8000/docs
- Health check: http://localhost:8000/health
- Temporal Web UI: http://localhost:8233 — inspect workflow runs, retries, and
  the `daily-topic-digests` schedule directly

Example flow:
```bash
# Create a topic
curl -X POST localhost:8000/topics \
  -H "Content-Type: application/json" \
  -d '{"name": "RLHF", "query": "reinforcement learning from human feedback"}'

# List topics
curl localhost:8000/topics

# Trigger a digest — returns a pending digest; a DigestWorkflow fills it in
curl -X POST localhost:8000/digests/<topic_id>

# Poll for the result (status flips pending -> completed/failed)
curl localhost:8000/digests/<digest_id>
```

The `temporal` container uses in-memory persistence (matches not persisting
Postgres data outside its named volume either) — workflow history resets on
restart, and the worker recreates the daily schedule on its next startup.

## Running migrations

```bash
# generate a new migration after changing app/models.py
alembic revision --autogenerate -m "description"

# apply migrations
alembic upgrade head
```

## Running tests

Tests run against a real Postgres instance (no mocking the DB — the schema and
constraints are part of what's being tested), with the schema built by running
the Alembic migrations so model/migration drift fails the suite.

Temporal workflow tests (`tests/test_temporal_workflows.py`) run the real
`DigestWorkflow` / `RunAllTopicDigestsWorkflow` against Temporal's ephemeral,
*time-skipping* test server (`temporalio.testing.WorkflowEnvironment`) — no
Docker or running Temporal server needed. Time-skipping fast-forwards through
retry backoff, so a test that exhausts all 5 arXiv retry attempts (which would
take over a minute of real backoff) finishes in about a second. Only the
schedule-creation test needs the full dev server (`start_local()` — the
time-skipping server doesn't implement the Schedule API), which does download
a small server binary on first use.

arXiv and Anthropic are faked by monkeypatching `app.temporal.activities`
directly; one `respx` test exercises the real Atom parser and one test asserts
the `DigestStatus` enum and the plain string literals the workflow uses for it
(`app/temporal/types.py`, kept dependency-free of SQLAlchemy) haven't drifted
apart.

```bash
# with docker-compose's db already running:
TEST_DATABASE_URL=postgresql+psycopg2://digest_user:digest_pass@localhost:5432/digest_test_db pytest -v
```

CI runs this automatically against a fresh Postgres service container on every push.
