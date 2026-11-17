# Public launch plan

Turning the research digest agent into a small public service: someone visits
the site, enters their email, up to five topics and how many papers they want
per topic, confirms by email, and gets a weekly digest on Monday mornings.

**Status: v3 (lean), 2026-11-16.** The first readers are 5–10 people the
owner invites; abuse and scale aren't real risks yet, and launching soon matters
more than polish. v3 keeps what's already built and cuts most of the remaining
scope of v2.2, which was sized for 200 strangers. v2.2 and earlier are in git
history.

---

## What changed from v2.2, and why

| Area | v2.2 | v3 | Why |
|---|---|---|---|
| Audience at launch | Up to 200 strangers, after a 3-week closed beta of about 20 | 5–10 invited readers. They are the beta | Owner's call: get it running for real people first |
| Topic preview, broad-phrase refusal, similar-topic suggestions (3b) | To build before launch | **Cut.** Broad topics: raise `ARXIV_MAX_RESULTS` from 25 to 100 instead | Suggestions had no calibrated threshold for the current embedding model anyway. With a handful of readers, the owner can eyeball topics |
| Picks-first email (2d Round 2) | Before launch | **After launch.** Launch with today's email (summaries plus overview) | Quality tuning was already deferred to after launch (decision #20) |
| Cost and load rehearsal (2e) | 200 subscribers, 100 topics, a week of measurement | **Cut** | Measured on the first live cycle: about $0.007 per topic per send. 10 readers × 5 topics is a few dollars a month against $50 |
| Website (Phase 4) | Landing page, Playwright end-to-end tests in CI, axe-core scans | A short intro on `/signup`, one small stylesheet, real terms and privacy text, and a check on a phone | |
| Production (Phase 5) | CI-built images, GHCR, rollback rehearsal, restore drill, Sentry, JSON logs, five alert types, stats page | One VM, Caddy, a production compose file, `git pull` deploys, a nightly `pg_dump` | |
| Legal (Phase 6) | Detailed CalOPPA policy, beta, staged announcement | Short terms (US only, 13+, AI text may be wrong) and a short privacy page listing what's stored and who processes it | Still collecting emails, so both pages stay |
| Dead code | Off-by-default features kept "for later" | **Removed 2026-11-16:** full text, LLM related-work notes, rating anchors, the "compare" overview style, similar-topic suggestions, the Ollama backends | Measured worse, ruled out by arXiv's terms, or unused (`docs/rag-plan.md`). In git history |

---

## Constraints

| Constraint | Value |
|---|---|
| OpenAI spend | $50/month, enforced in the app (alert at 80%, AI text stops at 100%); OpenAI project hard limit about $55 |
| Readers at launch | 5–10, invited. The caps stay as settings: 200 subscribers, 100 active topics, 5 topics per subscriber |
| Papers per topic per email | 5–10, default 5 |
| Topics (public) | Phrases only; `cat:` and other field queries are admin-only |
| Cadence | Weekly, Monday 08:00 `America/New_York` (prep 01:00, ingest 21:30 daily) |
| arXiv content | Metadata and abstracts only (CC0). No full text, no bulk download |
| Host | One Hetzner CX23 (2 vCPU / 4 GB) in the EU, about €6/month |
| Email | Brevo free tier over SMTP (300/day) |

---

## Already built and live

- **Phase 1:** subscribers, shared topics with personal labels, admin token on the JSON API.
- **2a–2c:** spend log and budget guard, OpenAI embeddings, the three passes (ingest, send prep, send) with stored per-topic editions.
- **v2.2 switch-over:** Monday-only sends, $50 budget.
- **2d Round 1:** blurb and overview-and-picks prompts plus their eval script. Not wired in; that's Round 2, after launch.
- **3a:** signup, confirmation, manage page, one-click unsubscribe, delete my data, rate limits, Turnstile, a hand-admitted waitlist, and unsubscribe headers and links in digests. Signup fails closed until real Turnstile keys are set.

Each built phase's plan and outcome is in `docs/plans/`.

---

## Remaining work to launch

### 1. Owner actions (no code)

- [x] Pick a name: **Papers Weekly**, `papersweekly.com` (chosen 2026-11-16). Nothing in the name may imply arXiv endorsement.
- [ ] Register `papersweekly.com` (Cloudflare Registrar, about $10.50/year at cost).
- [ ] Brevo: create the account; authenticate the sending domain (SPF, DKIM); publish DMARC at `p=none`.
- [ ] Cloudflare Turnstile: create a site key for the domain.
- [ ] OpenAI: set the project's hard monthly limit to about $55.
- [ ] Hetzner: create the account and a CX23 in Nuremberg or Helsinki (fallback: the EU ARM plan).

### 2. Small code changes (one sitting)

- [ ] Set `ARXIV_MAX_RESULTS=100` (a setting, not code). Ingest and rating cost go up a little for broad topics; the budget covers it.
- [ ] Give arXiv API requests a `User-Agent` that names the service and its site. No personal email, per CLAUDE.md.
- [ ] Add a short intro to `/signup`: what it is, "Digests arrive Monday mornings", and the arXiv thank-you. Plus one small stylesheet that works at phone width.
- [ ] Replace the placeholder `/terms` and `/privacy` text with short real versions:
  - **What's stored:** email, topics, labels, papers per topic, and send history.
  - **Rate limits:** they use hashed IP addresses.
  - **Who processes data:** Hetzner, Brevo, OpenAI (topics and abstracts only, never addresses) and Cloudflare.
  - **Where data is stored:** in the EU.
  - **Deleting data:** done from the manage page.
- [ ] Tests for each change. The full suite stays green.

### 3. Production host

- [ ] `docker-compose.prod.yml`:
  - Caddy terminates TLS; only 80/443 are published.
  - Postgres, Temporal and its UI stay internal; reach the UI over an SSH tunnel.
  - No `--reload` and no source mounts.
  - Images are pinned (no `:latest`).
  - Exactly one worker.
- [ ] The server's `.env` holds the real secrets:
  - fresh `SECRET_KEY`, `ADMIN_TOKEN` and DB password;
  - the OpenAI key;
  - Brevo SMTP settings;
  - the Turnstile keys;
  - `PUBLIC_BASE_URL` set to the domain;
  - `ALERT_EMAIL`.
- [ ] Move the data:
  1. `pg_dump` from the Mac and restore it on the server.
  2. Run `alembic upgrade head`.
  3. Let the worker recreate the schedules on a fresh Temporal.
- [ ] Nightly `pg_dump` cron, copied off the box and kept for 14 days.
- [ ] Deploys: `git pull && docker-compose -f docker-compose.prod.yml up -d --build`, then migrations. Stop the worker first when a deploy changes a workflow.
- [ ] Stop the Mac stack once the server is live, so two workers never send.

### 4. Smoke test and invite

- [ ] Sign up through the real page, confirm, and check the manage link.
- [ ] Trigger a send (or wait for Monday), then check the digest's unsubscribe link and `List-Unsubscribe` header.
- [ ] mail-tester.com score of 9/10 or better, and SPF, DKIM and DMARC passing in the raw headers.
- [ ] Invite the 5–10 readers.

**Exit:** the owner gets a Monday digest from production, and the invited readers are signed up.

---

## Deferred until there's a reason

| Item | Revisit when |
|---|---|
| Picks-first email, prompt tuning, model choice (2d Round 2, rag-plan §5.11) | Right after launch, or when readers say the email feels generic |
| Topic preview and broad-phrase refusal | A topic regularly hits `ARXIV_MAX_RESULTS`, or strangers start signing up |
| Alerts beyond spend and failed ratings/summaries; uptime check; Sentry | A missed Monday goes unnoticed |
| CI image builds, rollback rehearsal, restore drill | The service has readers who aren't the owner's contacts |
| 3a review's deferred minors (rate-limit races, CSRF error page, `Referrer-Policy`, ...) | Real abuse, or open public signup |
| Dropping the unused columns and tables (`papers.related_note`, `papers.fulltext_status`, `papers.fulltext_checked_at`, `topic_embeddings`, full-text chunk rows) | Any convenient migration; they're harmless meanwhile |
| Full text | arXiv says in writing that it's allowed. The code is in git history before 2026-11-16 |
| OpenAlex as a paper source | Owner raises it again |

---

## Decisions

| # | Decision | Status |
|---|---|---|
| 1 | Goal | A small real service for invited readers first; lean ops (v3, 2026-11-16) |
| 2 | Budget | $50/month hard cap on OpenAI; email alert at 80%, AI text off at 100% |
| 3 | LLM | `gpt-5.4-mini` for ratings, summaries and overviews |
| 4 | Embeddings | `text-embedding-3-small` at 768 dimensions |
| 5 | Full text | Off; code removed 2026-11-16 |
| 6 | Topics | Phrases only for the public; no preview or suggestions at launch |
| 7 | Cadence | Weekly, Monday 08:00 Eastern |
| 8 | Audience | US residents (signup checkbox), age 13+ |
| 9 | Email | Brevo free tier over SMTP |
| 10 | Hosting | One Hetzner CX23 in the EU, `git pull` deploys, nightly dumps |
| 11 | Name and domain | Papers Weekly, `papersweekly.com` (decided 2026-11-16; registration pending) |
| 12 | Email format | Today's format at launch; picks-first after launch |
| 13 | Beta | The 5–10 invited readers are the beta |
