# Research Digest & Alert Agent

A service that tracks research topics on arXiv, summarizes new papers with an LLM,
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

**Phase 2 (next):** arXiv ingestion + LLM summarization, wired into the digest trigger.
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
- `Topic` — something you're tracking (name + arXiv search query)
- `Paper` — an ingested arXiv paper, optionally summarized
- `Digest` — a generated batch of papers for a topic, with a status (pending/completed/failed)
- `topic_paper` — join table, since a paper can match more than one topic

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

# Trigger a digest (Phase 1: creates a pending record; Phase 2 will fill it in)
curl -X POST localhost:8000/digests/<topic_id>
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
constraints are part of what's being tested).

```bash
# with docker-compose's db already running:
TEST_DATABASE_URL=postgresql+psycopg2://digest_user:digest_pass@localhost:5432/digest_test_db pytest -v
```

CI runs this automatically against a fresh Postgres service container on every push.

## What I'd do with more time

- Phase 2/3 as described above — the Temporal workflow with a real retry policy
  on the arXiv/LLM calls is the highest-signal piece of this project and the
  main thing left to build.
- Rate limiting on the digest-trigger endpoint.
- Auth (currently open — fine for a portfolio project, not for anything real).
- Pagination cursors instead of offset/limit once topic/paper counts grow.
