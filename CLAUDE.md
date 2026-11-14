# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A research alerting service: FastAPI + Postgres/pgvector + Temporal. The end goal is to fetch new papers per topic from arXiv, rates them with OpenAI (`gpt-5.4-mini`), embeds them (`text-embedding-3-small`, 768 dims), and emails twice-weekly per-topic digests. It's being turned into a small public service per `docs/public-launch-plan.md`. Each phase has an implementation plan in `docs/superpowers/plans/`, and each plan ends with an "Outcome / Carried forward" section. Read the latest one before starting new work. `docs/rag-plan.md` records every retrieval and prompt measurement and the decision it led to.

## Commands

The venv's console scripts point at an old path, so always invoke tools as modules. The shell is macOS bash 3.2 (no `timeout`), and Compose is the standalone `docker-compose` binary.

```bash
venv/bin/python -m pytest -q                                  # full suite (~80 s); Postgres must be up
venv/bin/python -m pytest tests/test_prep_pass.py::test_name -q   # one test
docker-compose up -d --build api worker                       # rebuild after code changes
docker-compose run --rm worker alembic upgrade head           # migrate the live DB
docker-compose exec -T temporal temporal workflow list --query "ExecutionStatus='Running'"
docker-compose exec -T temporal temporal schedule list
docker-compose exec -T db psql -U digest_user -d digest_db    # live DB
venv/bin/python -m evals.rag run --variant baseline --repeats 2 --summaries 0   # rating eval (costs OpenAI money)
```

There is no linter or formatter configured. CI (`.github/workflows/ci.yml`) runs `pytest -v` against a pgvector Postgres.

**Tests.**
- They use a separate database, `digest_test_db`, on the same Postgres.
- The `db_session` fixture runs the Alembic migrations up and back down per test, so model and migration drift fails the suite.
- A killed test run can leave the test DB at head with rows in it. Reset it with `venv/bin/python -c "import tests.conftest as c; from alembic import command; command.downgrade(c._ALEMBIC_CFG,'base')"`.
- Workflow tests run on Temporal's time-skipping server via the `temporal_client` fixture in `tests/test_temporal_workflows.py`. It builds the real workers via `app.worker.build_workers` and patches `Summarizer`, `EmailSender`, `search_arxiv` and `fetch_max_author_h_index` with fakes.
- The time-skipping clock can jump years between workflow runs, so pass explicit slots (`slot_iso`) instead of relying on `workflow.now()`.
- Schedule tests need `WorkflowEnvironment.start_local()`, via the `temporal_env_only` fixture.

## Architecture

**Three passes, all in `America/New_York`.** The worker creates or updates the schedules on startup, in `app/temporal/schedule.py`.

| Pass | Schedule | Workflow | What it does |
|---|---|---|---|
| Ingest | `daily-topic-digests`, 21:30 daily | `RunAllTopicDigestsWorkflow` → `DigestWorkflow` per topic | fetch arXiv → ingest → embed → rate only |
| Send prep | `send-prep`, 01:00 Mon/Thu | `PrepEditionsWorkflow` → `PrepTopicEditionWorkflow` | re-rate unrated → h-index → rank → summarize the top K → overview → store an **edition** |
| Send | `weekly-digest-emails`, 08:00 Mon/Thu | `SendDigestEmailsWorkflow` | per due subscriber, render the topic's edition, capped at their `max_papers` |

**Editions** (`topic_editions`, `edition_papers`).
- One edition per topic per send slot. Its window is `(window_start, window_end]` on `Digest.completed_at`, and it chains from the topic's previous edition. All subscribers of a topic share it.
- K is the largest `max_papers` among the topic's active subscriptions (NULL counts as 10), and at least 5.
- If the send finds no edition, it runs that topic's prep as a child workflow under `prep_workflow_id(topic, slot)`, so it's never run twice. If the scheduled prep is already running, it waits for it. Only a failed prep leaves a `fallback` edition without AI text.
- Slots come from the scheduled fire time (`TemporalScheduledStartTime`), not the clock: `cadence.prep_slot` for prep, `cadence.latest_slot` for send. This is so a run caught up after the host slept keeps its slot.

**Temporal rules.**
- `workflows.py` is orchestration only. All I/O is in `activities.py`.
- Settings that affect concurrency are read through activities (`DigestContext`, `PipelineLimits`, `EditionPlan`), so they're recorded in workflow history.
- Every `Worker` must be built with `workflow_runner=WORKFLOW_RUNNER`, whose sandbox passthrough list includes `app.temporal.types`.
- `app/temporal/types.py` must stay stdlib-only and must not use `from __future__ import annotations`. Datetime fields otherwise fail to decode, and the workflow task retries forever, which looks like a hang.
- arXiv requests (`fetch_arxiv`, `fetch_fulltext`) run on a separate `<queue>-arxiv` task queue with `max_concurrent_activities=1`, behind a process-wide 3 s gate (`arxiv.throttle`).
- Changing a workflow's shape is a history change: deploy only with no workflows running.

**Spend guard** (`app/services/spend.py`, `app/spend_ledger.py`).
- Every OpenAI call goes through `Summarizer._complete` or `Embedder` with a `DbLedger`, which writes `llm_usage` rows.
- The monthly budget is $20. At 80% the owner gets an alert; at 100% chat calls return None (not an error) while embeddings continue.
- Owner alerts go through `app.alerts.send_alert`, deduped by a dated key in `sent_alerts`.

**Ranking** (`app/services/ranking.py`, used through `app/services/email.py`): 0.6 × relevance + 0.3 × log h-index + 0.1 × novelty, plus up to 0.08 for venue. Missing signals take the batch median. Novelty anchors and every retrieval threshold are per embedding model, in `Settings.retrieval_calibration`. The worker refuses to start without a calibration row for the active model.

**Prompts** (`app/services/summarize.py`).
- The rate-only prompt (`_RELEVANCE_SYSTEM`, built from `_RELEVANCE_BANDS`) is what ingest uses.
- Prep uses the combined `summarize_and_rate` prompt but stores only its summary.
- Prompt changes are gated by evals against `evals/labels.csv` and `evals/labels_diverse.csv`. Record the results in `docs/rag-plan.md`.

## Working in this repo

- **Git.** Commit straight to `main` and push, as the repo's git user, with no Claude or AI attribution anywhere. No branches, no PRs. Commit messages are lowercase past-tense sentences ("added send prep: one edition per topic, ...").
- **The live stack runs this working tree.** `worker` and `api` mount `.:/code`, the API runs `--reload`, and Temporal's sandbox re-reads workflow modules for each workflow run. Before a multi-commit change to workflows or models:
  1. confirm nothing is running;
  2. pause `daily-topic-digests`, `send-prep` and `weekly-digest-emails` with `temporal schedule toggle --pause`;
  3. `docker-compose stop worker`.

  Afterwards, migrate, rebuild and unpause; `ensure_*` doesn't unpause.
- **Back up before a live migration:** `docker-compose exec -T db pg_dump -U digest_user digest_db > ~/research-digest-backups/<name>.sql`.
- **Never read or print `.env` secrets** (`OPENAI_API_KEY`, `ADMIN_TOKEN`, `OPENALEX_API_KEY`). Ask the owner before OpenAI spend beyond what a plan calls for, such as summary backfills.
- **Never put the owner's email or other personal details in requests to external services.** That includes OpenAlex's `mailto`.
- **Full text stays off** (`RAG_FULLTEXT=false`) until arXiv allows it, and there's no bulk arXiv download, per arXiv's terms.
