# Papers Weekly

[![CI](https://github.com/kian-hekmat/research-digest-agent/actions/workflows/ci.yml/badge.svg)](https://github.com/kian-hekmat/research-digest-agent/actions/workflows/ci.yml)

**Live at [papersweekly.com](https://papersweekly.com).** A weekly email of the
new arXiv papers worth reading for the topics you follow.
- **Signup:** pick up to five topics as plain phrases.
- **Nightly:** every new matching paper is fetched, embedded and rated for relevance by an LLM.
- **Monday mornings:** each reader gets the best few, with short summaries and an overview of the week.

It runs as a small public service for a handful of invited readers, on one
€7/month server and a $50/month OpenAI budget. I designed, built and operate it
end to end. (The repository's original name, research-digest-agent, is from
when it was a personal tool.)

## Highlights

- **Durable pipeline on Temporal.**
  - Three scheduled passes (ingest, send prep, send). Each external call is its own activity with its own retry policy.
  - A crash or a host that was down delays a digest instead of losing it.
  - Missed runs catch up and keep their original send slot.
- **One shared edition per topic per week.** Prep ranks and summarizes once per topic; the send only assembles stored content, with no LLM calls.
- **Hard cost control.**
  - Every OpenAI call is priced and recorded.
  - A $50 monthly guard alerts at 80% and stops AI text at 100% rather than failing.
  - Behind it sits the OpenAI project's own hard limit.
- **Eval-gated LLM work.**
  - Ratings are checked against reference labels (Spearman ρ 0.77) and ranking by NDCG.
  - Features that didn't beat the baseline were removed, not just switched off.
- **Self-service without accounts:**
  - double opt-in;
  - signed, purpose-scoped links;
  - RFC 8058 one-click unsubscribe;
  - delete my data;
  - rate limits;
  - responses that never reveal who has signed up.
- **Production on one VM.**
  - Docker Compose behind Caddy with automatic HTTPS, and nightly database dumps.
  - Covered by 596 tests run against real Postgres and Temporal's time-skipping server.

## What a digest looks like

```
Subject: Reward modeling: 5 new papers

==============================
Reward modeling - 5 new papers
==============================
<An overview of the week: the specific findings and the threads that
 connect this week's papers, written knowing what earlier weeks said>

November 13, 2026
-----------------
* <Paper title>
  <3–4 sentence summary of the abstract: problem, method, headline result>
  Similar earlier paper: <title> https://arxiv.org/abs/2610.01234
  https://arxiv.org/abs/2611.04567
...
Unsubscribe from Reward modeling: <one-click link>
--
Manage your topics: <link>
Unsubscribe from everything: <link>
Thank you to arXiv for use of its open access interoperability.
```

Readers choose 5–10 papers per topic. When a topic has more, the highest-ranked
are kept, not the newest. A near-duplicate of a paper already in the email is
folded under it rather than taking a slot.

## How it works

Three passes, on a schedule in `America/New_York`:

| Pass | When | What it does |
|---|---|---|
| **Ingest** | Nightly 21:30 | For each topic with an active reader: fetch new papers from arXiv (abstracts only), embed them, and rate each one's relevance to the topic (1–10) |
| **Send prep** | Monday 01:00 | Per topic: re-rate anything left unrated, look up author h-indices, rank the week, summarize the top K, write the overview, and store it as the topic's **edition** |
| **Send** | Monday 08:00 | Per reader: assemble their topics' editions, each cut to their own cap, and send one email |

**Ranking:** `0.6 × relevance + 0.3 × log(max author h-index) + 0.1 × novelty`, plus up to 0.08 for a venue acceptance.
- Novelty is how unlike the paper is to the topic's papers from the last 90 days, by embedding similarity.
- A missing signal takes the batch median, so a paper too new to be indexed neither sinks nor rises.

## Architecture

```
             ┌─────────────────── Hetzner CX23 (Docker Compose) ───────────────────┐
 Browser ───▶│ Caddy (HTTPS) ──▶ FastAPI ──────────────▶ PostgreSQL 16 + pgvector  │
             │                     │ starts workflows             ▲                 │
             │                     ▼                              │                 │
             │                  Temporal ◀──────────────▶ Worker (one process) ─────┼──▶ arXiv API (1 request / 3 s)
             │                                                                      ├──▶ OpenAI (gpt-5.4-mini, embeddings)
             │                                                                      ├──▶ OpenAlex / Semantic Scholar
             └──────────────────────────────────────────────────────────────────────┴──▶ Resend (SMTP)
```

- **FastAPI** serves the public pages: server-rendered Jinja signup, confirm, manage, unsubscribe, terms and privacy. It also serves a token-protected JSON admin API. It never makes slow external calls itself.
- **The worker** runs every workflow and activity. `workflows.py` is orchestration only; all I/O is in `activities.py`.
- **arXiv requests** go through a separate one-slot task queue and a process-wide 3-second gate, so there's only ever one request in flight.
- **PostgreSQL** holds everything, including the vector index (`paper_embeddings`, HNSW, cosine). There's no separate vector database.

**Stack:** Python 3.12, FastAPI, Pydantic, SQLAlchemy 2, Alembic, PostgreSQL 16 + pgvector, Temporal, Jinja2, Caddy, Docker Compose, GitHub Actions.

## Retrieval and evaluation

The LLM originally saw one abstract at a time. Five retrieval features were
then built and measured against that abstract-only baseline before deciding
whether to ship each one. Every number, review and decision is in
[`docs/rag-plan.md`](docs/rag-plan.md).

The summarization model moved from a local Gemma 3 12B to `gpt-5.4-mini` on 2026-11-05:

| Measure | `gemma3:12b` | `gpt-5.4-mini` |
|---|---|---|
| Rating vs reference labels, Spearman ρ | 0.71 | 0.77 |
| Rating, on-topic papers only, ρ | 0.27 | 0.37 |
| Ranking NDCG@10 | 0.874 | 0.923 |
| Latency, summary with rating | 3.7 s | 1.6 s |

**Shipped**

| Feature | Evidence |
|---|---|
| Abstract embeddings in pgvector (`text-embedding-3-small`, 768 dims) | Nearest neighbours share a topic 89% of the time; thresholds recalibrated per model |
| "Similar earlier paper" link (no LLM claim attached) | Every neighbour reviewed at the threshold was genuinely related |
| Novelty in ranking (10% weight) | Kept small, because on-topic papers resemble earlier ones |
| Near-duplicate folding | A near-identical later paper is listed under the earlier one |
| History-aware overviews (avoid repeating the last 3) | Preferred 15 vs 2 in a blind review; 1 factual error in 25 |

**Measured and removed**

| Feature | Why |
|---|---|
| Full-text excerpts | Readers preferred them 17 vs 3, but arXiv's terms allow e-print content only for personal use or research |
| LLM-written related-work notes | 26% misdescribed the cited paper |
| Few-shot rating anchors | Made ratings worse (ρ 0.71 → 0.52) |
| Overviews that compare against history | 28% contained an invented cross-reference |
| Relevance as an on/off gate | Ranked worse than graded relevance on every topic |

**How it's measured** (`evals/`, run on demand against the real model, never in CI):
- **`evals.rag`:** rates and summarizes a reference-labeled sample and reports spread, stability, Spearman/MAE, unsupported numbers and latency. There are two label sets: 47 topic–paper pairs, and 47 papers from 8 unrelated fields.
- **`evals.ranking`:** replays each topic's labeled papers as one email batch under several scoring schemes, scored by NDCG with bootstrap intervals.
- **`evals.email_prompts`:** generates the next email format's overviews and blind summary pairs for review.

## Production

- **Server:** one Hetzner CX23 (2 vCPU, 4 GB) in Nürnberg, running [`docker-compose.prod.yml`](docker-compose.prod.yml).
- **HTTPS:** Caddy is the only thing on ports 80 and 443, and gets Let's Encrypt certificates itself. Postgres and Temporal aren't reachable from outside; the Temporal UI is reached over an SSH tunnel.
- **Secrets:** the server's `.env`, mode 600 (template: [`.env.production.example`](.env.production.example)).
- **Email:** sent as `Papers Weekly <digest@papersweekly.com>` through Resend's SMTP, with SPF and DKIM on the domain and DMARC set to `p=none`. Digests carry `List-Unsubscribe` headers.
- **Deploys:** `git pull` and `docker compose up -d --build` on the server, after the suite passes locally and in CI.
- **Backups:** a nightly `pg_dump` with 14 days kept ([`scripts/backup.sh`](scripts/backup.sh)).

The full runbook is in [`docs/deploy.md`](docs/deploy.md): server setup, the database move, backups, the Temporal UI and logs.

## Running locally

Prerequisites: Docker (colima on macOS) and an [OpenAI API key](https://platform.openai.com/api-keys).

```bash
cp .env.example .env               # set OPENAI_API_KEY and ADMIN_TOKEN; add SECRET_KEY for the public pages
docker-compose up -d --build       # Postgres + pgvector, Temporal, Mailpit, API, worker
docker-compose exec api alembic upgrade head
python -m venv venv && venv/bin/pip install -r requirements.txt
venv/bin/python -m pytest -q       # 596 tests; needs the local Postgres
```

| URL | What |
|---|---|
| http://localhost:8000/signup | The public signup page (Turnstile needs keys in `.env`) |
| http://localhost:8000/docs | The admin API (every route needs the `X-Admin-Token` header; off in production) |
| http://localhost:8233 | Temporal UI: workflow runs, retries, schedules |
| http://localhost:8025 | Mailpit: every email sent locally lands here |

**Key settings** (all environment variables; the full list with defaults is in [`app/config.py`](app/config.py)):

| Variable | Default | Purpose |
|---|---|---|
| `OPENAI_API_KEY` / `OPENAI_MODEL` | unset / `gpt-5.4-mini` | Ratings, summaries and overviews |
| `SUMMARY_BACKEND` | `openai` | `none` sends digests without AI text |
| `OPENAI_MONTHLY_BUDGET_USD` / `ALERT_EMAIL` | `50` / unset | The spend guard and where its alerts go |
| `EMBEDDING_MODEL` | `text-embedding-3-small` | Needs a calibration row in `RETRIEVAL_CALIBRATION` (the worker refuses to start without one) |
| `ADMIN_TOKEN` | unset | Required for the admin API; when unset, the API refuses everything |
| `SECRET_KEY` / `PUBLIC_BASE_URL` | unset / `http://localhost:8000` | Signs confirm, manage and unsubscribe links. The public pages return 503 without it |
| `TURNSTILE_SITE_KEY` / `TURNSTILE_SECRET_KEY` | unset | Cloudflare Turnstile on signup; with no secret, every signup is refused |
| `SMTP_*`, `SMTP_FROM_NAME` | Mailpit | Outgoing email (production: Resend, as "Papers Weekly") |
| `ARXIV_MAX_RESULTS` / `ARXIV_LOOKBACK_DAYS` | `100` / `7` | Papers per topic per fetch, and the window for arXiv's announcement lag |
| `SEMANTIC_SCHOLAR_API_KEY` | unset | Optional; without it, author h-indices come from OpenAlex |

## Further reading

- [`docs/public-launch-plan.md`](docs/public-launch-plan.md): the launch plan. It covers what's done, what was cut to launch lean, and what's deferred.
- [`docs/rag-plan.md`](docs/rag-plan.md): every retrieval and prompt evaluation, and the decision it led to.
- [`docs/deploy.md`](docs/deploy.md): the production runbook.
