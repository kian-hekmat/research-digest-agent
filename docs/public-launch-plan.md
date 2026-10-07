# Public launch plan

Turning the research digest agent into a public service: anyone visits a website,
enters their email, topics, cadence and papers-per-topic, confirms by email, and
gets digests. Built on the existing pipeline (FastAPI, Postgres, Temporal, the
arXiv → LLM summary/relevance → ranked email flow), not a rewrite.

Status: draft, 2026-10-06.

---

## 1. Where we are: gap audit

The pipeline is solid for one trusted user on a laptop. These are the parts that
assume that, and have to change before strangers can use it:

| Area | Today | Needed for public |
|---|---|---|
| Access control | Every endpoint is open. Anyone can delete topics or trigger digests, and `GET /topics/{id}/subscriptions` returns subscribers' emails | Public pages only expose signup and self-service. The existing JSON API becomes admin-only |
| Identity | A subscription is just an email string. Nothing proves the person owns it | Double opt-in confirmation; signed links to manage or unsubscribe; no passwords |
| Unsubscribe | None. Emails have no unsubscribe link | One-click unsubscribe (link plus `List-Unsubscribe` headers) and a manage-preferences link in every email. Legally required (CAN-SPAM) and required by Gmail and Yahoo for bulk senders |
| Topics | Global, unique by name, created through the admin API | Created on demand from user input. Deduplicated by normalized query so 50 people tracking "RLHF" share one topic and one set of LLM calls. Each subscription keeps its own display label |
| What gets fetched | Every topic, every day | Only topics with at least one confirmed, active subscriber (this is the main cost control) |
| arXiv politeness | `_throttle` is per-process and not thread-safe. The daily fan-out runs every topic's fetch concurrently on a 20-thread worker | At most one arXiv request in flight globally, at least 3s apart. arXiv asks this of API clients; ignoring it at scale risks an IP ban |
| LLM | Ollama on your Mac, called directly by the worker on the same machine | Still Ollama on your Mac, but as a separate LLM-only worker the server queues work for. Emails send only what's been summarized, with a fallback when the Mac has been off too long (§2, decision 4) |
| Email | Mailpit locally | Transactional email provider on your own domain with SPF/DKIM/DMARC; bounce and complaint handling |
| Hosting | docker-compose on a laptop. Postgres (with the default password), Temporal and its unauthenticated UI, and Mailpit are all published on every network interface | Always-on host. Only HTTPS is public; secrets come from the environment; backups run |
| Abuse | Nothing stops anyone from subscribing someone else's address, or from creating 1,000 topics | CAPTCHA on signup, rate limits, per-subscriber and global topic caps |
| Legal | None | Privacy policy, terms, postal address in the email footer, self-serve data deletion, arXiv attribution |

What carries over unchanged: digest workflows, retries, cadence logic, ranking,
email rendering, the email workflow's per-recipient failure isolation, and the
test harness (real Postgres + time-skipping Temporal).

---

## 2. Target architecture

```
Browser ──HTTPS──▶ Caddy (TLS) ──▶ FastAPI ──▶ Postgres
                                     │  ├─ public site (Jinja2 + htmx pages)
                                     │  ├─ token endpoints (confirm / manage / unsubscribe)
                                     │  ├─ email-provider webhooks (bounces, complaints)
                                     │  └─ admin JSON API (token-protected)
                                     ▼
                                  Temporal ◀── Worker ──▶ arXiv  (one global, rate-limited queue)
                                    ▲                ├──▶ OpenAlex (h-index)
                                    │                └──▶ Brevo (SMTP)
                                    │ outbound only, over Tailscale
                                    │
                    Your Mac: LLM worker (polls the `llm` task queue only) ──▶ Ollama (gemma3:12b)
```

### Key decisions, with my recommendation

1. **Frontend: server-rendered pages in the existing FastAPI app (Jinja2 + htmx),
   not a separate SPA.** The site is roughly six forms and status pages. One
   deployable means no CORS, no second build pipeline, and the same test client
   for pages and API. htmx covers the one dynamic piece: a live topic preview.
2. **Identity: the email address is the account, with no passwords.** Signup
   sends a confirmation link. Every digest carries a signed "manage" link, and
   the site has a "send me my manage link" form. Links are HMAC-signed tokens
   bound to a purpose (confirm, manage, unsubscribe) with expiry where it makes
   sense. Revoking means bumping a per-subscriber token version.
3. **Topics are shared, labels are personal.** `Topic` gets a unique
   `normalized_query` (trimmed, lowercased, whitespace and quotes collapsed).
   Each `Subscription` gets a user-facing `label`. Only the normalized query is
   sent to the LLM, never a user-chosen label, so a malicious label can't reach
   other users' prompts or emails.
4. **LLM: stays free, on your Mac (decided 2026-10-06).** The always-on server
   does everything except LLM calls. Those go on a dedicated `llm` Temporal task
   queue, which only a worker on your Mac polls.
   - **How it connects:** the Mac worker connects outbound to Temporal over
     Tailscale; no public ports.
   - **Sleep and wake:** when the Mac sleeps, LLM tasks wait in the queue; when
     it wakes, they drain. A launchd agent starts the worker at login.
   - **No DB credentials on the laptop:** LLM activities become pure (text in,
     text out), and the server-side worker writes results to Postgres.
   - **Delivery policy:** emails include only papers that have their summary
     and rating. An unsummarized paper waits for the next email, up to
     `LLM_HOLD_DAYS` (default 3). After that it's sent without a summary,
     ranked on h-index and venue alone, so a week away from the laptop delays
     summaries but never loses papers.
   - **Capacity:** measured at ~4s per paper on gemma3:12b, which is about 900
     papers per hour the Mac is awake. That throughput, not money, sets the
     global topic cap. `gemma3:4b` is the faster fallback if the queue backs
     up.
   - **Overviews:** move from once per daily digest run to once per topic per
     send.
5. **Email: Brevo over SMTP (decided).** Its free tier allows 300 emails a day,
   enough for about 300 recipients per send day. `EmailSender` already speaks
   SMTP, so this is configuration plus a bounce/complaint webhook. Move to
   Amazon SES (pay per use) if it's outgrown.
6. **Hosting: one Hetzner CX23 VM (decided).** 2 vCPU, 4 GB RAM, €5.49/month at
   last check, running a production compose file with Caddy for automatic TLS.
   - Staging runs as a second compose project on the same VM.
   - Temporal stays on its dev-server binary with its SQLite file: single-node,
     fine at this scale, and schedules rebuild themselves on startup.
   - Postgres is self-hosted on the VM with nightly `pg_dump` to a free-tier
     object store.

---

## 3. Phases

Each phase ships as one or more pull requests. Each must keep the full suite
green and adds its own tests (listed per phase). A phase is done only when its
exit criteria pass.

### Phase 1: Multi-user data model and lockdown

**Build**
- `Subscriber` table: `email` (unique, case-insensitive), `confirmed_at`,
  `unsubscribed_at`, `token_version`, `created_at`, `last_manage_link_sent_at`.
- `Subscription` references `subscriber_id` instead of holding `email`, and
  gains `label`. The unique constraint becomes `(topic_id, subscriber_id)`.
- `Topic` gains `normalized_query` (unique). Name uniqueness is dropped.
- Migration: create a subscriber per distinct existing email, marked confirmed
  (that's you). Existing topics get a normalized query; merge any duplicates.
- `list_active_topic_ids` returns only topics with at least one active
  subscription from a confirmed, not-unsubscribed subscriber.
  `list_due_subscriptions` applies the same filter.
- The existing JSON API moves behind an `X-Admin-Token` header (constant-time
  comparison). The endpoint that lists subscriber emails becomes admin-only.
- Limits as settings: max topics per subscriber (default 5), max active topics
  globally (default about 200, depending on budget), max query length (200
  characters), allowed characters.

**Tests**
- Migration: upgrade from the current head on a seeded copy of today's data, then
  downgrade, then upgrade again. Data is preserved; duplicate topics merge.
- Normalization (unit): `"RLHF"`, `" rlhf "`, `'"RLHF"'` all map to one topic;
  `cat:cs.LG` passes through unchanged.
- Workflow: an unconfirmed or unsubscribed subscriber is never emailed, and
  their topic isn't fetched unless someone else needs it.
- API: every admin route returns 401 without a token or with a wrong one;
  nothing public returns an email address.

**Exit:** the existing suite passes on the new model, and the current
deployment migrates with no data loss.

### Phase 2: Signup, confirmation and self-service (backend)

**Build**
- Tokens: `SECRET_KEY` setting, `itsdangerous` (or stdlib HMAC). Purposes:
  - `confirm`: expires in 48h.
  - `manage`: long-lived, invalidated by bumping `token_version`.
  - `unsubscribe`: per subscription, never expires, so old emails' links keep
    working.
- `POST /signup` (email, topics, cadence, max_papers):
  - Creates or updates a pending subscriber and its subscriptions, then sends a
    confirmation email via a small Temporal workflow (reuses the retry policies).
  - Enumeration-safe: the response is identical whether or not the email exists.
  - An already-confirmed address gets a manage link instead of a second account.
- `GET /confirm?token=`: activates the subscriber. The first digest arrives on
  their first cadence boundary, the same rule as today.
- Manage page (`/manage?token=`): edit topics, labels, cadence and cap; pause;
  "delete my data", which cascades.
- Unsubscribe:
  - `POST /unsubscribe?token=` implements one-click unsubscribe (RFC 8058); it
    must work with no cookies and no confirmation step.
  - `GET` shows a confirmation page with a re-subscribe option.
- Digest emails get `List-Unsubscribe` and `List-Unsubscribe-Post` headers, plus
  a footer with manage and unsubscribe links and the postal address.
- Topic preview, `GET /preview?q=`: the most recent ~5 papers the query would
  match. It goes through the global arXiv throttle and is cached for 1h per
  normalized query. A query with no matches in 30 days shows a warning before
  signup, not after the first empty email.
- Abuse controls:
  - Cloudflare Turnstile on signup.
  - Rate limits, Postgres-backed so they survive restarts and work across
    multiple app workers: per IP (e.g. 5 signups/hour), and per email for
    confirmation and manage-link resends (e.g. 3/day).

**Tests**
- Tokens (unit): round-trip; tampered signature, expired token, wrong purpose,
  stale `token_version` and a token for a deleted subscriber are all rejected.
- Flows (integration, TestClient + real Postgres + fake email sender):
  - signup → confirmation email captured → confirm → active
  - manage edits
  - one-click `POST /unsubscribe` with no session
  - delete data removes all rows
  - re-signup after unsubscribe
- Security:
  - Signup returns the same body, status and similar timing for new and existing
    emails.
  - Rate limits return 429.
  - CSRF: form POSTs without a valid token or same-origin header are rejected.
  - Headers and labels: newlines in input can't inject email headers, and HTML
    in labels is escaped in pages and emails.
- Workflow: a confirmation email that fails to send is retried; a digest email
  carries both unsubscribe headers with a working link.

**Exit:** a test can go from signup to first digest to unsubscribe without
touching the admin API.

### Phase 3: The website

**Build**
- Pages:
  - Landing page: what it is, a sample digest, arXiv attribution.
  - Signup: email, up to 5 topics each with a live preview, cadence, papers per
    topic, and a short explanation of how papers are ranked.
  - "Check your inbox", confirmed, manage, unsubscribed, privacy, terms.
- Plain semantic HTML, a classless CSS base, works without JavaScript except
  the preview. Mobile first, light and dark.

**Tests**
- Page tests (TestClient): every page renders 200 with expected content and
  escapes user input; form validation errors render inline.
- End-to-end (Playwright, Python) against the full compose stack:
  - Fill the signup form, then fetch the confirmation email through Mailpit's
    HTTP API and click its link.
  - Trigger a send with fake arXiv and LLM data, then follow the email's
    unsubscribe link.
  - Run on every PR to `main` in CI; it takes a few minutes.
- Accessibility: axe-core scan via Playwright on each page; zero serious or
  critical issues. Keyboard-only signup works.
- Phone-width viewport: no horizontal scroll, and the form is usable.

**Exit:** the end-to-end suite passes in CI, and a manual pass on a real phone.

### Phase 4: Pipeline hardening for many users

**Build**
- **Global arXiv rate limit.**
  - Move `fetch_arxiv` (and the preview's fetches) to a dedicated `arxiv` task
    queue, served by a worker with `max_concurrent_activities=1` that keeps the
    3s spacing.
  - The daily refresh then takes about topics × 3s: roughly 10 minutes for 200
    topics, well within the gap before the send.
- **Bounded fan-out.** `RunAllTopicDigestsWorkflow` runs children in batches
  rather than all at once.
- **Laptop LLM worker** (see §2, decision 4).
  - Split the LLM activities onto the `llm` task queue as pure functions; a
    server-side activity persists their results.
  - Digests complete once papers are ingested; summarization runs separately,
    with no start timeout, so tasks wait however long the Mac is away.
  - `gather_digest_content` holds unsummarized papers up to `LLM_HOLD_DAYS`.
  - Tailscale on the VM and the Mac; a launchd plist for the worker.
  - Admin stats show the LLM queue's backlog and oldest waiting task.
- **One overview per topic per send instead of per daily run.** Fewer calls,
  and one coherent paragraph instead of up to four stacked ones.
- **Email at volume.**
  - Provider SMTP credentials.
  - Webhook endpoint for bounces and complaints that deactivates the subscriber
    (verifying the provider's signature).
  - Plain-text and HTML parts already exist.
- **LLM throughput check.** Measure papers per hour on your Mac under the
  production mix, and set the global topic cap from it.

**Tests**
- Workflow (time-skipping):
  - 50 topics: record arXiv call timestamps through a fake and assert at most
    one in flight with at least 3s between calls.
  - Fan-out batches stay within the concurrency bound.
- Laptop worker (time-skipping):
  - With no `llm` worker running, a digest still completes and its papers are
    held from the email.
  - Starting the worker later drains the queue, and the next email includes
    those papers with summaries.
  - A paper held past `LLM_HOLD_DAYS` is sent without a summary.
  - The LLM activity never opens a DB session (the test asserts this).
- Webhook: a signed bounce deactivates the subscriber; a bad signature returns
  401.
- Relevance eval: a small labeled set (~40 topic/paper pairs, rated by you as
  central, related or tangential) run against the local model.
  - Report band accuracy.
  - Gate prompt or model changes on it not regressing.
  - Free to run; run manually, not in CI.
- Load and cost rehearsal (staging, fakes for external calls):
  - Seed 1,000 subscribers and 200 topics; run refresh and send end to end.
  - Record wall-clock per stage, DB query counts and peak memory.
  - Then one run with the real local model on 20 topics, to measure Mac time
    per topic per day.

**Exit:** rehearsal finishes inside the window between refresh and send, and a
topic cap is set from measured Mac throughput.

### Phase 5: Production infrastructure and operations

**Build**
- `docker-compose.prod.yml`:
  - Only Caddy publishes ports (80/443). Postgres, Temporal, its UI and the
    worker stay on the internal network; reach the Temporal UI over an SSH
    tunnel.
  - No `--reload`, no source volume mounts; images are built in CI. Several
    uvicorn workers.
  - Secrets live in a server-side env file (never committed): Postgres
    password, `SECRET_KEY`, admin token, SMTP credentials,
    Turnstile secret.
- Domain and DNS: A/AAAA records, SPF, DKIM, DMARC (start at `p=none` with
  reports, tighten after two clean weeks), and a dedicated sending subdomain.
- Backups: nightly `pg_dump` to off-site object storage, 30-day retention.
  Temporal's SQLite volume is backed up too, though it's less critical since
  schedules recreate themselves and digests are rebuilt from Postgres.
- Deploys:
  - GitHub Actions: tests → build image → push to GHCR → deploy to staging →
    smoke test → manual approval → production.
  - Migrations run as a one-off step before the new containers start.
- Staging: a second, cheaper VM (or the same VM under a different compose
  project) on a `staging.` subdomain, with Mailpit instead of real email.
- Observability:
  - Structured JSON logs; Sentry (or similar) for exceptions.
  - Uptime check on `/health`, extended to check DB and Temporal connectivity.
  - Alerts: digest failures, send failures, LLM queue backing up, bounce
    rate over 2%, no successful send by 09:00 on send days.
  - A small admin stats page: subscribers, topics, emails sent, LLM queue depth.

**Tests**
- A post-deploy smoke script (run by CI against staging, then production):
  - `/health` is green.
  - The signup page renders.
  - On staging: signup → confirm → admin-triggered send to a Mailpit inbox.
- Restore drill before launch, then quarterly: restore last night's dump into a
  scratch database, run migrations, run read-only checks. It counts only if it
  actually restores.
- Deliverability, manual before launch:
  - mail-tester.com score of 9/10 or better.
  - Gmail and Outlook inbox placement for a test digest.
  - SPF, DKIM and DMARC all pass in raw headers.
- Port scan of the production host: only 22 (key-only), 80 and 443 open.

**Exit:** staging and production deploy from CI; restore drill done; alerts
proven by tripping each once on staging.

### Phase 6: Legal, privacy and launch

**Build**
- Privacy policy:
  - What's stored: email, topics, cadence, send history.
  - Processors: host, email provider. Summaries come from a model running on
    our own hardware, so no third-party AI provider sees any data.
  - Retention, deletion.
- Terms: no warranty, acceptable use.
- Postal address in the email footer (CAN-SPAM). A PO box or virtual mailbox
  works.
- arXiv attribution ("Thank you to arXiv for use of its open access
  interoperability") and no implied endorsement. Credit OpenAlex and Semantic
  Scholar.
- Closed beta: 10–20 people for two weeks. Watch bounces, spam complaints, cost
  per topic, and whether the ranking feels right. Then open signup with the
  global topic cap in place.

**Exit:** beta with no unresolved deliverability or cost surprises; launch
checklist (§6) signed off.

---

## 4. Testing strategy (cross-cutting)

| Layer | Tooling | Covers | Runs |
|---|---|---|---|
| Unit | pytest | Tokens, normalization, ranking, parsers, rate-limit math, email rendering | Every push |
| Integration | pytest + real Postgres (migrations-built, as today) | CRUD, migrations, signup/manage/unsubscribe flows, webhooks, admin auth | Every push |
| Workflow | Temporal time-skipping server + fakes (as today) | Fan-out, arXiv spacing, retries, budget guard, unsubscribe headers | Every push |
| Page | FastAPI TestClient | Rendering, validation, escaping, CSRF | Every push |
| End-to-end | Playwright + compose stack + Mailpit API | Real browser signup → email link → digest → unsubscribe | PRs to `main` |
| Accessibility | axe-core via Playwright | Every public page | PRs to `main` |
| Security | pytest + a manual checklist | Enumeration, rate limits, token misuse, header injection, admin auth, exposed ports | Every push (automated parts); pre-launch (manual) |
| LLM eval | Labeled relevance set against the real model | Rating quality on gemma3:12b; prompt or model changes | Manual, on change |
| Load and cost | Seeded staging + fakes; one real-LLM sample | Stage timings, DB load, Mac time per paper | Pre-launch, then before raising caps |
| Deploy smoke | Script in CI | Staging and production right after each deploy | Every deploy |
| Recovery | Restore drill | Backups actually restore | Pre-launch, quarterly |
| Deliverability | mail-tester, inbox checks, DMARC reports | Spam placement | Pre-launch, then weekly via DMARC reports |

Ground rules:
- All existing tests stay green throughout.
- No test reaches a real external service except the opt-in LLM eval and the
  real-model throughput sample.
- Each bug fixed during beta gets a regression test (the existing suite already
  follows this pattern).

---

## 5. Cost model

Summaries, ratings and overviews run on a local Gemma model on your Mac (§2,
decision 4), so the LLM has no per-call cost. Monthly costs are:
- the VM (about €5.49),
- the domain (about $10–20/year),
- the virtual mailbox (typically about $10/month; see §7),
- email (Brevo free tier).

The scarce resource is Mac time, not money: about 4s per paper on
`gemma3:12b`, roughly 900 papers per hour the Mac is awake. Users don't drive
that load; distinct active topics do, which is why dedup, fetching only
subscribed topics, and the global topic cap matter.

Levers if the queue backs up: `gemma3:4b` (faster, somewhat weaker summaries),
overviews once per send instead of per daily run (Phase 4), and a lower
`ARXIV_MAX_RESULTS`.

Infrastructure: a small VM for app, worker, Temporal and Postgres, a second
small VM or shared host for staging, a domain, off-site backup storage, and an
email provider. Most providers' free or entry tiers cover launch volume; check
current pricing when choosing.

---

## 6. Launch checklist

- [ ] All phase exit criteria met
- [ ] Admin token, `SECRET_KEY` and database password rotated from any value
      used in development
- [ ] Port scan clean; Temporal UI not public
- [ ] Restore drill passed within the last 7 days
- [ ] SPF, DKIM and DMARC pass; mail-tester score of 9 or better
- [ ] LLM queue-backlog alert set; alert tested
- [ ] Global topic cap set from the budget
- [ ] Privacy policy, terms and postal address live
- [ ] Beta feedback triaged; no open P0/P1 issues

---

## 7. Decisions

| # | Decision | Status |
|---|---|---|
| 1 | Budget | Minimal. No paid LLM; the topic cap is set by Mac throughput |
| 2 | Name and domain | Open: choosing from a shortlist |
| 3 | LLM | Local Ollama on your Mac as a queue-fed worker (§2, decision 4) |
| 4 | Hosting | Hetzner CX23, self-hosted Postgres with off-site dumps |
| 5 | Email | Brevo free tier over SMTP; SES if outgrown |
| 6 | Postal address | Virtual mailbox (a commercial mail receiving agency address) |
| 7 | Topics | Free text, with a live preview and limits; `cat:` prefix still supported |
