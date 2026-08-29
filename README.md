# Research Digest & Alert Agent

A service that tracks research topics on arXiv, summarizes new papers with Claude,
and generates digests on a schedule. Built to rehearse a realistic backend/agent
stack: FastAPI, PostgreSQL, and (in Phase 3) Temporal-orchestrated workflows.

## Status

**Phase 1 complete:** core API and data model.
- [x] FastAPI app with a real resource model: topics, papers, digests
- [x] PostgreSQL persistence with a proper schema (many-to-many topics↔papers, FK on digests)
- [x] Alembic migrations
- [x] Pydantic validation + meaningful error responses (404s, 409 on duplicate topic)
- [x] Dockerized (docker-compose: app + Postgres)
- [x] Automated tests (pytest, hitting a real test DB)
- [x] CI (GitHub Actions) running tests on every push

**Phase 2 complete:** arXiv ingestion + LLM summarization, wired into the digest trigger.
- [x] `search_arxiv()` — arXiv Atom API client (newest-first, `since` watermark filter, request throttling)
- [x] `Summarizer` — per-paper summaries (`claude-haiku-4-5`) + a synthesized digest overview (`claude-sonnet-5`), via the Anthropic SDK
- [x] `run_digest()` — framework-free orchestration (fetch → upsert & link → summarize → finalize), ready to lift into Temporal Activities
- [x] `POST /digests/{topic_id}` returns a `pending` digest immediately; a FastAPI background task fills it in and flips it to `completed`/`failed`
- [x] Per-topic `last_checked_at` high-water mark, advanced only on a successful run
- [x] Tests build the schema from migrations (not `create_all`); external calls are faked, with one `respx` test over the real Atom parser

**Phase 3 (next):** move ingestion/summarization into a Temporal workflow running on
a daily schedule, with retry policies on the external API calls.

## Architecture

```
┌─────────────┐      ┌──────────────┐      ┌─────────────┐
│   Client    │─────▶│   FastAPI    │─────▶│  PostgreSQL │
└─────────────┘      │   (app/)     │      └─────────────┘
                      └──────┬───────┘
                             │ (Phase 3)
                             ▼
                      ┌──────────────┐
                      │   Temporal   │  scheduled workflow:
                      │   worker     │  fetch → summarize → store
                      └──────────────┘
```

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

## How a digest runs (Phase 2)

`POST /digests/{topic_id}` creates a `pending` digest and returns it right away.
A FastAPI background task then runs `app/services/digest.py::run_digest`:

1. **fetch** — `search_arxiv(topic.query, since=topic.last_checked_at)` hits the
   arXiv Atom API, newest submission first. A failed fetch → digest `failed`,
   watermark untouched (so the next run retries the same window).
2. **upsert & link** — each result is upserted by `arxiv_id` (version suffix
   stripped for a stable key) and linked to the topic and this digest.
3. **summarize** — every paper without a `summary` gets one from
   `claude-haiku-4-5`. A per-paper failure is tolerated: the paper is kept
   without a summary and the count lands in `digest.error`.
4. **overview** — `claude-sonnet-5` synthesizes a 5–6 sentence paragraph across
   the batch's summaries into `digest.overview` (skipped when there are none).
5. **finalize** — status → `completed` (even with zero new papers), then the
   topic watermark advances to when the run started.

`run_digest` and its steps take no FastAPI/Temporal imports — Phase 3 wraps each
step as a Temporal Activity and turns `run_digest` into the Workflow.

Configuration (`app/config.py`, env-driven — see `.env.example`): `ANTHROPIC_API_KEY`
(required for real runs), `SUMMARY_MODEL`, `OVERVIEW_MODEL`, `ARXIV_MAX_RESULTS`,
`ARXIV_PAGE_DELAY`.

## Running locally

```bash
docker-compose up --build
```

This starts Postgres and the API. Once healthy:
- API docs: http://localhost:8000/docs
- Health check: http://localhost:8000/health

Example flow:
```bash
# Create a topic
curl -X POST localhost:8000/topics \
  -H "Content-Type: application/json" \
  -d '{"name": "RLHF", "query": "reinforcement learning from human feedback"}'

# List topics
curl localhost:8000/topics

# Trigger a digest — returns a pending digest; the background task fills it in
curl -X POST localhost:8000/digests/<topic_id>

# Poll for the result (status flips pending -> completed/failed)
curl localhost:8000/digests/<digest_id>
```

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
the Alembic migrations so model/migration drift fails the suite. The arXiv and
Anthropic calls are faked via dependency overrides; one `respx` test exercises
the real Atom parser.

```bash
# with docker-compose's db already running:
TEST_DATABASE_URL=postgresql+psycopg2://digest_user:digest_pass@localhost:5432/digest_test_db pytest -v
```

CI runs this automatically against a fresh Postgres service container on every push.

