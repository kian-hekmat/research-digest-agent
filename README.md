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

**Phase 4 complete:** weekly/biweekly email delivery to subscribers.
- [x] `Subscription` — an email + cadence (`weekly`/`biweekly`) attached to a topic,
  with its own `last_sent_at` watermark (mirrors `Topic.last_checked_at`)
- [x] `POST/GET /topics/{topic_id}/subscriptions`, `DELETE /subscriptions/{id}`
- [x] `SendDigestEmailsWorkflow` — the scheduled entrypoint: judges every
  subscription against its own cadence (cron has no native "every other week", so
  it fires weekly and each subscription decides for itself whether it's due),
  gathers everything completed since the subscriber's last email, and delivers it
  by SMTP; one subscriber's failure never blocks another's, and a skip (nothing
  new) leaves the watermark alone rather than silently dropping content
- [x] `EmailSender` (`app/services/email.py`) — plain SMTP, swappable via a client
  factory for tests, same injection pattern as `Summarizer`
- [x] A weekly Temporal Schedule (Mondays 08:00 UTC), created idempotently
- [x] Mailpit added to docker-compose as the local SMTP catcher — nothing sent by
  `docker-compose up` reaches a real inbox unless `SMTP_HOST`/`SMTP_PORT` are
  deliberately pointed elsewhere
- [x] Verified live: a seeded due subscription + completed digest produced a real
  email in Mailpit with correct subject/overview/paper content, the watermark
  advanced, and a second run correctly sent nothing (not yet due again)

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
                      │              │─────▶│  SMTP/Mailpit│
                      └──────────────┘      └─────────────┘
```

The `api` and `worker` containers both run the same code against the same
Postgres; `api` only ever talks to Temporal to start workflows and to Postgres
to read/write topics and digests directly (fast, synchronous). All the slow,
retryable, external work (arXiv, Anthropic, email) happens in `worker`'s Activities.

**Data model:**
- `Topic` — something you're tracking (name + arXiv search query). `last_checked_at`
  is the ingestion high-water mark (NULL = never run → pull everything)
- `Paper` — an ingested arXiv paper; `summary` is the LLM per-paper summary
- `Digest` — a generated batch of papers for a topic, with a `digest_status`
  enum (`pending`/`completed`/`failed`), an `overview` column (LLM-synthesized
  paragraph across the batch), and an `error` column for failure detail
- `Subscription` — an email + cadence (`weekly`/`biweekly`) subscribed to a topic,
  with its own `last_sent_at` delivery watermark
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

## How email delivery runs

`SendDigestEmailsWorkflow` fires weekly (Mondays 08:00 UTC), but each subscription
is judged against its *own* cadence, not the schedule's:

1. **list_due_subscriptions** — active subscriptions where
   `now - last_sent_at >= 7 days` (weekly) or `>= 14 days` (biweekly).
2. Per due subscription, concurrently:
   - **gather_digest_content** — every completed digest for that topic generated
     since the subscriber's `last_sent_at`, rendered into a subject + text/HTML
     body. Nothing new → skip the send *and* leave the watermark alone, so the
     subscriber never silently loses a week's content.
   - **send_digest_email** — SMTP via `EmailSender`, retried a few times.
   - **mark_subscription_sent** — only on a confirmed send, so a failed send
     (retries exhausted) is retried in full on the next scheduled run rather than
     skipped. One subscriber's failure never blocks another's delivery.

Subscribe via `POST /topics/{topic_id}/subscriptions` (`{"email": "...", "cadence":
"weekly"}`, cadence optional, defaults to weekly); list with `GET` on the same
path; unsubscribe with `DELETE /subscriptions/{id}`.

Configuration (`app/config.py`, env-driven — see `.env.example`): `ANTHROPIC_API_KEY`
(required for real runs), `SUMMARY_MODEL`, `OVERVIEW_MODEL`, `ARXIV_MAX_RESULTS`,
`ARXIV_PAGE_DELAY`, `TEMPORAL_ADDRESS`, `TEMPORAL_NAMESPACE`, `TEMPORAL_TASK_QUEUE`,
`SMTP_HOST`, `SMTP_PORT`, `SMTP_USERNAME`, `SMTP_PASSWORD`, `SMTP_USE_TLS`,
`SMTP_FROM_ADDRESS`.

## Running locally

```bash
docker-compose up --build
```

This starts five containers: `db` (Postgres), `temporal` (Temporal dev server),
`mailpit` (local SMTP catcher), `api` (FastAPI), and `worker` (the Temporal worker
— polls the task queue and creates both schedules on startup, idempotently). Once
healthy:
- API docs: http://localhost:8000/docs
- Health check: http://localhost:8000/health
- Temporal Web UI: http://localhost:8233 — inspect workflow runs, retries, and the
  `daily-topic-digests` / `weekly-digest-emails` schedules directly
- Mailpit Web UI: http://localhost:8025 — every email the app sends locally lands
  here, not in a real inbox

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

arXiv, Anthropic, and email are faked by monkeypatching `app.temporal.activities`
directly (`search_arxiv`, `Summarizer`, `EmailSender`); one `respx` test exercises
the real Atom parser, one exercises `EmailSender`/`render_digest_email` against a
fake SMTP client, and one test asserts the `DigestStatus` enum and the plain string
literals the workflow uses for it (`app/temporal/types.py`, kept dependency-free of
SQLAlchemy) haven't drifted apart.

**A gotcha worth knowing if you add a new cross-boundary dataclass:**
`app/temporal/types.py` deliberately does *not* use `from __future__ import
annotations`. Temporal's payload converter resolves a dataclass's field types via
`dataclasses.fields()`, which returns raw (unresolved) string annotations under
postponed evaluation — this silently breaks `datetime` fields specifically (it
needs the concrete type object) when that dataclass is returned nested inside a
generic like `list[...]`. It doesn't raise; the workflow task just fails and
Temporal retries it forever, which looks exactly like a hang. `DueSubscription`
hit this first, since it was the first `list[...]`-returned dataclass with a
`datetime` field. Python 3.10+'s `X | None` syntax works fine at runtime without
the future import, so there's no downside to leaving it out in that file.

```bash
# with docker-compose's db already running:
TEST_DATABASE_URL=postgresql+psycopg2://digest_user:digest_pass@localhost:5432/digest_test_db pytest -v
```

CI runs this automatically against a fresh Postgres service container on every push.
