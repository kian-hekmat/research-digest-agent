# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

**Papers Weekly** (https://papersweekly.com): a research alerting service built on FastAPI, Postgres/pgvector and Temporal.
- Each night it fetches new papers per topic from arXiv, embeds them (`text-embedding-3-small`, 768 dims) and rates each one's relevance with OpenAI (`gpt-5.4-mini`).
- Each Monday morning it emails per-topic digests: the top papers with short summaries and an overview of the week.

It's a small public service for 5–10 invited readers first.

## Status (2026-11-17)

- **Live in production** on one Hetzner CX23 (Nürnberg, Ubuntu 24.04, `91.99.205.127`), deployed from `/opt/papersweekly` with `docker-compose.prod.yml`.
- **The Mac stack is for development only.** Its worker is stopped and its three schedules are paused. Never unpause them or start that worker: two workers would both send.
- **Built:**
  - Phase 1: subscribers, shared topics, the admin token.
  - 2a–2c: spend guard, OpenAI embeddings, the three passes with editions.
  - The v2.2 switch-over: Monday-only sends, $50 budget.
  - 2d Round 1: picks-first prompts, not wired in.
  - 3a: signup, confirm, manage, unsubscribe, hand-admitted waitlist.
  - Launch section 2: signup intro, stylesheet, real terms and privacy pages, `ARXIV_MAX_RESULTS=100`, the arXiv User-Agent.
  - Launch section 3: production host, HTTPS, the database moved from the Mac, nightly backups.
- **Next:** launch section 4: a signup smoke test on the real site, then invites. After launch: a nicer frontend, and 2d Round 2 (the picks-first email).
- **Cut on 2026-11-16 (don't rebuild unasked):**
  - from the plan: 3b (topic preview and suggestions) and 2e (load rehearsal);
  - from the code, and still in git history: full text, LLM related-work notes, rating anchors, the "compare" overview style and the Ollama backends.
- **Docs:**
  - `docs/public-launch-plan.md` (v3, lean): the launch checklist, what was cut or deferred and why, and the notes for the picks-first email (2d Round 2). Read it before starting new work. Per-phase plans were deleted on 2026-11-17; they're in git history.
  - `docs/deploy.md`: the server runbook.
  - `docs/rag-plan.md`: a condensed log of every retrieval and prompt evaluation and its decision. Section numbers (§5.x) are cited in code comments. Eval reports go to `evals/results/`, which is gitignored.

## Commands

**Local (development).** The venv's console scripts point at an old path, so always invoke tools as modules. The shell is macOS bash 3.2 (no `timeout`), and local Compose is the standalone `docker-compose` binary.

```bash
venv/bin/python -m pytest -q                                  # full suite (~90 s, 596 tests); local Postgres must be up
venv/bin/python -m pytest tests/test_prep_pass.py::test_name -q   # one test
docker-compose up -d db temporal mailpit api                  # dev stack; leave the worker stopped
venv/bin/python -m evals.rag run --variant baseline --repeats 2 --summaries 0   # rating eval (costs OpenAI money)
```

**Production (the live service).**
- Reach the server with `ssh root@91.99.205.127`.
- The owner's SSH key has a passphrase. If BatchMode SSH fails with "Permission denied", ask the owner to run `ssh-add --apple-use-keychain ~/.ssh/id_ed25519`.
- On the server, Compose is the `docker compose` plugin. Run these in `/opt/papersweekly`:

```bash
C="docker compose -f docker-compose.prod.yml"
git pull && $C up -d --build                                  # deploy what's pushed to main
scripts/backup.sh && $C build api worker && $C run --rm worker alembic upgrade head && $C up -d   # deploy with a migration: build BEFORE migrating, since `run` uses the last-built image
$C exec -T temporal temporal workflow list --query "ExecutionStatus='Running'"
$C exec -T temporal temporal schedule list
$C exec -T db psql -U digest_user -d digest_db                # live DB
$C logs -f api worker caddy
scripts/backup.sh                                             # a dump now (cron runs it nightly at 03:15 UTC)
```

There is no linter or formatter configured. CI (`.github/workflows/ci.yml`) runs `pytest -v` against a pgvector Postgres.

**Tests.**
- They use a separate database, `digest_test_db`, on the local Postgres.
- The `db_session` fixture runs the Alembic migrations up and back down per test, so model and migration drift fails the suite.
- A killed test run can leave the test DB at head with rows in it. Reset it with `venv/bin/python -c "import tests.conftest as c; from alembic import command; command.downgrade(c._ALEMBIC_CFG,'base')"`.
- Workflow tests run on Temporal's time-skipping server via the `temporal_client` fixture in `tests/test_temporal_workflows.py`. It builds the real workers via `app.worker.build_workers` and patches `Summarizer`, `EmailSender`, `search_arxiv` and `fetch_max_author_h_index` with fakes.
- The time-skipping clock can jump years between workflow runs, so pass explicit slots (`slot_iso`) instead of relying on `workflow.now()`.
- Schedule tests need `WorkflowEnvironment.start_local()`, via the `temporal_env_only` fixture.
- Summarizer tests fake OpenAI with respx through `openai_test_settings()` / `openai_reply()` / `OPENAI_TEST_CHAT` in `tests/conftest.py`.
- `tests/test_prod_config.py` checks the production stack, the Caddyfile, the env template and the backup script.

## Architecture

**Three passes, all in `America/New_York`.** The worker creates or updates the schedules on startup, in `app/temporal/schedule.py`.

| Pass | Schedule | Workflow | What it does |
|---|---|---|---|
| Ingest | `daily-topic-digests`, 21:30 daily | `RunAllTopicDigestsWorkflow` → `DigestWorkflow` per topic | fetch arXiv (up to 100 results) → ingest → embed → rate only |
| Send prep | `send-prep`, 01:00 Monday | `PrepEditionsWorkflow` → `PrepTopicEditionWorkflow` | re-rate unrated → h-index → rank → summarize the top K → overview → store an **edition** |
| Send | `weekly-digest-emails`, 08:00 Monday | `SendDigestEmailsWorkflow` | per due subscriber, render the topic's edition, capped at their `max_papers` |

**Editions** (`topic_editions`, `edition_papers`).
- One edition per topic per send slot. Its window is `(window_start, window_end]` on `Digest.completed_at`, and it chains from the topic's previous edition. All subscribers of a topic share it.
- K is the largest `max_papers` among the topic's active subscriptions (NULL counts as 10), and at least 5.
- If the send finds no edition, it runs that topic's prep as a child workflow under `prep_workflow_id(topic, slot)`, so it's never run twice. If the scheduled prep is already running, it waits for it. Only a failed prep leaves a `fallback` edition without AI text.
- Slots come from the scheduled fire time (`TemporalScheduledStartTime`), not the clock: `cadence.prep_slot` for prep, `cadence.latest_slot` for send. This is so a run caught up after the host was down keeps its slot.

**Temporal rules.**
- `workflows.py` is orchestration only. All I/O is in `activities.py`.
- Settings that affect concurrency are read through activities (`DigestContext`, `PipelineLimits`, `EditionPlan`), so they're recorded in workflow history.
- Every `Worker` must be built with `workflow_runner=WORKFLOW_RUNNER`, whose sandbox passthrough list includes `app.temporal.types`.
- `app/temporal/types.py` must stay stdlib-only and must not use `from __future__ import annotations`. Datetime fields otherwise fail to decode, and the workflow task retries forever, which looks like a hang.
- arXiv requests (`fetch_arxiv`) run on a separate `<queue>-arxiv` task queue with `max_concurrent_activities=1`, behind a process-wide 3 s gate (`arxiv.throttle`). They send `User-Agent: PapersWeekly/1.0 (+https://papersweekly.com)`.
- Changing a workflow's shape is a history change: deploy only with no workflows running.

**Spend guard** (`app/services/spend.py`, `app/spend_ledger.py`).
- Every OpenAI call goes through `Summarizer._complete` or `Embedder` with a `DbLedger`, which writes `llm_usage` rows. Summaries and embeddings are OpenAI-only.
- The monthly budget is $50 (`openai_monthly_budget_usd`); the OpenAI project's own hard limit is about $55. At 80% the owner gets an email alert; at 100% chat calls return None (not an error) while embeddings continue.
- Owner alerts go through `app.alerts.send_alert`, deduped by a dated key in `sent_alerts`.

**Public pages** (Phase 3a, `app/routers/public.py`). Plain server-rendered Jinja templates sharing one inline stylesheet and footer in `base.html`. A nicer frontend comes after launch.
- **Routes:** `/signup` (`/` redirects there), `/confirm` (GET shows a button; POST confirms), `/manage`, `/manage/{action}`, `/unsubscribe` (RFC 8058 one-click POST, no CSRF), `/resubscribe`, `/manage-link`, `/terms` and `/privacy`. They're listed in `app.auth.PUBLIC_PATHS`.
- **Signed links** (`app/tokens.py`) use `SECRET_KEY`, salted by purpose. The public routes return 503 while it's empty. Production has its own key, so links signed on the Mac don't work there.
- **No leaking who's signed up:** work that depends on the address runs in a `BackgroundTask`.
- **Rate limits** (`app/ratelimit.py`) are rows in `rate_limit_events` with HMAC'd keys, per client IP.
  - In production, Caddy runs on the host network and uvicorn runs with `--proxy-headers`, so the client IP is the real visitor, over IPv4 and IPv6. Don't put Caddy back on the compose network: Docker's IPv6 proxy hides visitors behind its gateway.
- **Cap check:** `app/waitlist.apply_waitlist` is the one place it happens. People are admitted by hand: `GET /admin/waitlist`, then `POST /admin/subscribers/{id}/admit` or `POST /admin/subscriptions/{id}/admit`.
- **Account emails** go through `SendAccountEmailWorkflow`. Digests carry `List-Unsubscribe` and footer links.
- **Settings:** `SECRET_KEY`, `PUBLIC_BASE_URL` (`https://papersweekly.com` in production), `TURNSTILE_SITE_KEY` and `TURNSTILE_SECRET_KEY`. With an empty Turnstile secret, every signup is refused.

**Email.**
- Production sends as `Papers Weekly <digest@papersweekly.com>` through Resend SMTP: `smtp.resend.com:587` with STARTTLS, username `resend`, and an API key as the password.
- `SMTP_FROM_NAME` sets the display name; the SMTP envelope stays the bare address.
- The domain's SPF, DKIM and DMARC records are in Cloudflare DNS.
- Replies to `digest@` aren't received; the privacy page gives the owner's email as the contact.
- The Mac's `.env` still uses Gmail SMTP for development.

**Ranking** (`app/services/ranking.py`, used through `app/services/email.py`): 0.6 × relevance + 0.3 × log h-index + 0.1 × novelty, plus up to 0.08 for venue. Missing signals take the batch median. Novelty anchors and every retrieval threshold are per embedding model, in `Settings.retrieval_calibration`. The worker refuses to start without a calibration row for the active model.

**Prompts** (`app/services/summarize.py`).
- The rate-only prompt (`_RELEVANCE_SYSTEM`, built from `_RELEVANCE_BANDS`) is what ingest uses.
- Prep uses the combined `summarize_and_rate` prompt but stores only its summary. Overviews see the topic's last 3 overviews only to avoid repeating them.
- Prompt changes are gated by evals against `evals/labels.csv` and `evals/labels_diverse.csv`. Record the results in `docs/rag-plan.md`.

**Production stack** (`docker-compose.prod.yml`, `Caddyfile`, `.env.production.example`, `scripts/backup.sh`).
- **Caddy** is on the host network and the only thing on 80/443. It gets Let's Encrypt certificates itself, so Cloudflare DNS must stay "DNS only".
- **API** on `127.0.0.1:8000`, two uvicorn workers.
- **Worker:** exactly one.
- **Temporal:** the dev server with SQLite on a volume; its UI is on `127.0.0.1:8233`, reached over an SSH tunnel.
- **Postgres:** on the compose network only.
- **The server's `.env`** is mode 600, filled from the template, with `API_DOCS=false` so there's no public `/docs`, `/redoc` or `/openapi.json`. Never print it.
- **Backups:** nightly dumps in `/var/backups/papersweekly` (14 days). There are no Hetzner Backups; the owner copies a dump to the Mac now and then.

## Working in this repo

- **Git.** Commit straight to `main` and push. No branches, no pull requests. Commit and push as the repo's git user, with no Claude or AI attribution anywhere: no `Co-Authored-By`, no "Generated with" lines and no "authored by Claude", in commits, comments or docs. Commit messages are lowercase past-tense sentences ("added send prep: one edition per topic, ...").
- **Deploying.** Production runs what's pushed to `main`, once it's pulled on the server. Run the full suite locally before pushing. Then on the server: `git pull && $C up -d --build`. Before deploying a change to workflows or models:
  1. confirm nothing is running on the server;
  2. pause `daily-topic-digests`, `send-prep` and `weekly-digest-emails` with `temporal schedule toggle --pause`;
  3. `$C stop worker`.

  Afterwards, migrate, rebuild and unpause; `ensure_*` doesn't unpause.
- **Back up before a live migration** with `scripts/backup.sh` on the server. Older Mac-era backups are in `~/research-digest-backups/`.
- **Never read or print `.env` secrets**, local or on the server (`OPENAI_API_KEY`, `ADMIN_TOKEN`, `SECRET_KEY`, `POSTGRES_PASSWORD`, `SMTP_PASSWORD`, `TURNSTILE_SECRET_KEY`, `OPENALEX_API_KEY`). Check keys by name only. Ask the owner before OpenAI spend beyond what a plan calls for, such as summary backfills.
- **Never put the owner's email or other personal details in requests to external services.** That includes OpenAlex's `mailto` and the arXiv User-Agent.
- **Abstracts only.** Full-text fetching was removed on 2026-11-16 (arXiv's terms; it's in git history), and there's no bulk arXiv download.
- **Size work for 5–10 trusted readers.** Don't add security hardening, scale work, alerting or CI ceremony unless asked; propose the minimal version.
- Never falsify tests, if a test fails, always report as such to the user, propose fixes, and get their approval before implementing. Never try to circumvent failing tests by rewriting them.  
- Prioritize test-driven development. Every chunk of features that's added should be driven by a test suite and should be reported to the features along with its results after it's done being implemented. 
- Never use superpowers skills unless explicitly prompted to do so by user (i.e. "use test-driven-development superpowers skills). Never assume superpowers skills should be used. Every time you use a superpower skill, the user must then be explicitly told which skill you are using and the specific way that they requested it. 
