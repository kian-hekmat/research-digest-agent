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
| Topic preview, broad-phrase refusal, similar-topic suggestions (3b) | To build before launch | **Cut** (confirmed by the owner 2026-11-16). Broad topics: raise `ARXIV_MAX_RESULTS` from 25 to 100 instead | Suggestions had no calibrated threshold for the current embedding model anyway. With a handful of readers, the owner can eyeball topics |
| Email delivery | Brevo free tier on the service's own domain, with SPF, DKIM and DMARC | **Resend** (free tier: 3,000 emails a month, 100 a day), sending as `Papers Weekly <digest@papersweekly.com>`. Replies aren't received (owner's call, 2026-11-17). Gmail SMTP was used from 2026-11-16 to 2026-11-17 | The owner didn't want mail coming from a personal address. Resend's SMTP needs no code change beyond a display name, and adds no branding |
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
| Email | Resend SMTP, from `Papers Weekly <digest@papersweekly.com>`, with SPF, DKIM and DMARC on the domain (100 emails a day on the free tier) |

---

## Already built and live

- **Phase 1:** subscribers, shared topics with personal labels, admin token on the JSON API.
- **2a–2c:** spend log and budget guard, OpenAI embeddings, the three passes (ingest, send prep, send) with stored per-topic editions.
- **v2.2 switch-over:** Monday-only sends, $50 budget.
- **2d Round 1:** blurb and overview-and-picks prompts plus their eval script. Not wired in; that's Round 2, after launch.
- **3a:** signup, confirmation, manage page, one-click unsubscribe, delete my data, rate limits, Turnstile, a hand-admitted waitlist, and unsubscribe headers and links in digests. Signup fails closed until real Turnstile keys are set.

The per-phase plans were deleted on 2026-11-17; they're in git history. What's still useful from them is below.

---

## Remaining work to launch

### 1. Owner actions (no code)

- [x] Pick a name: **Papers Weekly**, `papersweekly.com` (chosen 2026-11-16). Nothing in the name may imply arXiv endorsement.
- [x] Register `papersweekly.com` (Cloudflare Registrar; done 2026-11-17).
- [x] Email (2026-11-17): Resend account created and `papersweekly.com` verified. The DKIM and SPF records are in Cloudflare, plus DMARC at `p=none`. The server's `.env` has the Resend API key and sends as `Papers Weekly <digest@papersweekly.com>`. A test email from the server was delivered.
- [x] Cloudflare Turnstile: keys are in `.env` and the running API has loaded them (2026-11-16). Make sure the widget's hostname list includes `papersweekly.com`.
- [x] OpenAI: set the project's hard monthly limit to about $55 (done 2026-11-17).
- [x] Hetzner: a CX23 in Nürnberg, Ubuntu 24.04, at 91.99.205.127, Backups are off (owner's call; see the backup line in section 3). The DNS records are verified (2026-11-17).
- [ ] Confirm the Hetzner firewall (TCP 22, 80, 443 and UDP 443 only) is applied, and that Turnstile lists `papersweekly.com`.

### 2. Small code changes (one sitting)

- [x] Set `ARXIV_MAX_RESULTS=100` (now the default; 2026-11-16). Ingest and rating cost go up a little for broad topics; the budget covers it.
- [x] Give arXiv API requests a `User-Agent` (`PapersWeekly/1.0 (+https://papersweekly.com)`) that names the service and its site. No personal email, per CLAUDE.md.
- [x] Add a short intro to `/signup` (plus an "Already subscribed?" manage-link form, and a footer with the legal links and the arXiv thank-you on every page): what it is, "Digests arrive Monday mornings", and the arXiv thank-you. Plus one small stylesheet that works at phone width.
- [x] Replace the placeholder `/terms` and `/privacy` text with short real versions:
  - **What's stored:** email, topics, labels, papers per topic, and send history.
  - **Rate limits:** they use hashed IP addresses.
  - **Who processes data:** Hetzner, Resend (sends the email), OpenAI (topics and abstracts only, never addresses) and Cloudflare.
  - **Where data is stored:** in the EU.
  - **Deleting data:** done from the manage page.
  - **Contact:** the owner's email address, since replies to `digest@` aren't received (2026-11-17).
- [x] Tests for each change (`tests/test_launch_pages.py`, `tests/test_ingestion.py`); full suite 585 passed. Still to do by hand: a look on a real phone.

### 3. Production host

The commands are in `docs/deploy.md`.

- [x] `docker-compose.prod.yml` (2026-11-17):
  - Caddy terminates TLS; only 80/443 are published.
  - Postgres and Temporal stay internal; the Temporal UI is reachable only over an SSH tunnel.
  - No `--reload` and no source mounts.
  - Images are pinned.
  - Exactly one worker.
  - The API runs uvicorn with `--proxy-headers`, so rate limits see the visitor's IP rather than Caddy's.
  - Covered by `tests/test_prod_config.py`.
- [x] `Caddyfile`: `papersweekly.com` is served by the API, `/` redirects to `/signup`, and `www` redirects to the bare domain.
- [x] `.env.production.example`: every value the server's `.env` needs (fresh secrets, Resend SMTP as Papers Weekly, Turnstile, `PUBLIC_BASE_URL=https://papersweekly.com`, `ALERT_EMAIL`).
- [x] `scripts/backup.sh`: a nightly `pg_dump -Fc` with 14 days kept on the server. Hetzner Backups are off to save money; the owner copies a dump to the Mac now and then.
- [x] Prepare the server (2026-11-17): packages updated and rebooted, Docker 29.1, Compose 2.40 and git installed, the repo cloned to `/opt/papersweekly`, and `.env` created (mode 600) with the database password, `SECRET_KEY` and `ADMIN_TOKEN` generated on the server.
- [x] Fill in the owner's values in the server's `.env` (2026-11-17): OpenAI key, alert email, the SMTP password and the Turnstile keys. That password was first a Gmail app password; since 2026-11-17 it's the Resend API key.
- [x] Move the Mac's database by dump and restore (done 2026-11-17). An 8.2 MB dump restored with identical row counts at migration `f0a1b2c3d4e5`. The stack is live at https://papersweekly.com with a Let's Encrypt certificate, and the worker recreated the three schedules. Caddy runs on the host network so IPv6 visitors keep their real address for rate limits. A fresh start would lose:
  - this month's `llm_usage`, so the budget guard would undercount spend;
  - the topic watermarks, so every topic would refetch and re-rate a week of papers;
  - the edition chain and earlier overviews that prep reads;
  - the 90-day novelty history;
  - the owner's own subscription.

  Dead full-text chunk rows were already deleted (2026-11-17, owner's OK; 21,256 rows). The database went from 222 MB to 30 MB. The backup taken first is `~/research-digest-backups/digest_db_20261117_pre-chunk-delete.sql`.
- [x] Install the backup cron and run it once by hand (03:15 UTC nightly; the first dump was 8.3 MB).
- [x] Stop the Mac worker for good, so two workers never send. Its schedules are paused too (2026-11-17); the Mac stack is now for development only.

### 4. Smoke test and invite

- [ ] Sign up through the real page, confirm, and check the manage link.
- [ ] Trigger a send (or wait for Monday), then check the digest's unsubscribe link and `List-Unsubscribe` header.
- [ ] Send a digest to a Gmail and an Outlook address and check both land in the inbox, with SPF, DKIM and DMARC passing in the raw headers.
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
| A paid Resend plan, or Amazon SES | Daily sends get close to Resend's free 100 a day |
| 3a review's deferred minors (rate-limit races, CSRF error page, `Referrer-Policy`, ...) | Real abuse, or open public signup |
| Full text | arXiv says in writing that it's allowed. The code is in git history before 2026-11-16 |
| OpenAlex as a paper source | Owner raises it again |

### Cleanup (2026-11-17)

- The per-phase plans were deleted, and `docs/rag-plan.md` was condensed to a decisions log; both are in git history.
- Code nothing used was deleted: the summary backfill script, `summarize_paper`, the gated ranking mode, an unused exception and an unused setting.
- Migration `0a1b2c3d4e5f` drops the removed features' leftovers: `papers.related_note`, `fulltext_status` and `fulltext_checked_at`, the `topic_embeddings` table, and the `preview` spend purpose.
- `API_DOCS=false` hides `/docs`, `/redoc` and `/openapi.json` in production.

### Notes for the picks-first email (2d Round 2), kept from the deleted plans

- **Already built (Round 1):**
  - `Summarizer.write_blurb` (a hook plus what the paper did) and `Summarizer.write_overview_and_picks` (a 70–110 word overview citing `[arXiv IDs]`, plus up to 2 picks from the top 5), both returning strict JSON;
  - the citation check `_check_citations`, which drops IDs that weren't offered;
  - the eval `evals.email_prompts`, with its reviewer prompt in `evals/email_prompts_review_prompt.md`.
- **Still to build:**
  - **Storage:** store each paper's hook next to `summary`, with a prompt-version marker; store the edition's overview text with its `[ID]` markers, and its picks (paper, hook, order).
  - **Prep:** call `write_overview_and_picks` in place of `write_overview`. Take its input and pick pool from `rank_edition`'s deduped `top`, not `paper_ids`. A failed or budget-blocked call stores the edition without overview and picks.
  - **The email, per topic section:**
    - a header (label, date range, "N new papers, your top K below");
    - **Start here:** the picks, with hooks;
    - **The period in brief:** the overview, with offered `[ID]`s linked to arXiv;
    - **Also new:** the rest, as hook plus what;
    - a fallback edition renders "Also new" only.
  - **Escaping and links:** escape all model text and labels before linkifying, and link only offered IDs.
  - **Before IDs become links:** run pick hooks through the citation check too.
- **Known gaps:** the review failed 3 of 4 gates (rag-plan §5.11). The prompts were tightened but not re-evaluated; re-run the eval before wiring it in.

---

## Decisions

| # | Decision | Status |
|---|---|---|
| 1 | Goal | A small real service for invited readers first; lean ops (v3, 2026-11-16) |
| 2 | Budget | $50/month hard cap on OpenAI; email alert at 80%, AI text off at 100% |
| 3 | LLM | `gpt-5.4-mini` for ratings, summaries and overviews |
| 4 | Embeddings | `text-embedding-3-small` at 768 dimensions |
| 5 | Full text | Off; code removed 2026-11-16 |
| 6 | Topics | Phrases only for the public; no preview or suggestions at launch (3b cut, reconfirmed 2026-11-16) |
| 7 | Cadence | Weekly, Monday 08:00 Eastern |
| 8 | Audience | US residents (signup checkbox), age 13+ |
| 9 | Email | Resend SMTP as `Papers Weekly <digest@papersweekly.com>`, no reply inbox (2026-11-17; was Gmail SMTP from 2026-11-16) |
| 10 | Hosting | One Hetzner CX23 in the EU, `git pull` deploys, nightly dumps |
| 11 | Name and domain | Papers Weekly, `papersweekly.com` (decided 2026-11-16; registration pending) |
| 12 | Email format | Today's format at launch; picks-first after launch |
| 13 | Beta | The 5–10 invited readers are the beta |
| 14 | Data on the server | The Mac's database, moved by dump and restore (2026-11-16) |
