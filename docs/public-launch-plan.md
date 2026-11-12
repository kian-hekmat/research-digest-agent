# Public launch plan

Turning the research digest agent into a small public service for US users:
anyone visits the site, enters their email, up to five topics and how many
papers they want per topic, confirms by email, and gets a twice-weekly digest.
Built on the existing pipeline (FastAPI, Postgres, Temporal, the arXiv → LLM
rating/summary → ranked email flow), not a rewrite.

Status: v2, 2026-11-12. Replaces the 2026-10-06 draft, which assumed
summaries ran on local Gemma on the owner's Mac. That version is in git
history.

---

## 0. What changed from v1, and why

| Area | v1 | v2 | Why |
|---|---|---|---|
| LLM | Gemma on the Mac, fed by a task queue (Tailscale, launchd, `LLM_HOLD_DAYS`) | OpenAI `gpt-5.4-mini` on the server (shipped 2026-11-05). No Mac anywhere in production | Already moved. Better ratings and 2–3× lower latency (README eval table) |
| Embeddings | Ollama on the Mac (not addressed) | An OpenAI embedding model, called from the server | Removes the last dependency on the laptop |
| Budget | None ("no paid LLM") | **$20/month hard cap** on OpenAI, enforced in the app | Per-token cost grows with the number of distinct topics |
| Cost visibility | None: token usage isn't recorded | Every OpenAI call logged with its tokens and cost | The cap and the topic limit need real numbers |
| Full text | Fetched for every new paper | Only for papers that make it into an email (two passes, §3.5) | At 200 topics, v1 meant about 4h a day of arXiv requests at 1 per 3s. Also saves tokens |
| Cadence | Each subscriber chooses | **Twice-weekly for everyone** at launch | One overview per topic per send; simpler signup |
| Send time | Mon/Thu 08:00 Pacific (when the laptop was awake) | **Mon/Thu 08:00 Eastern** | US-only audience; the server is always on |
| Audience | Unspecified (worldwide) | **US residents** (terms plus a signup checkbox) | Only US law applies |
| Legal | CAN-SPAM postal address via a virtual mailbox | No postal address. Privacy policy that meets CalOPPA; CAN-SPAM practices followed voluntarily | A free, ad-free digest is very likely not "commercial" email. Saves about $120/year |
| Topic dedup | Exact match on the normalized string | Normalized string, plus a suggestion to join a similar existing topic, found by embedding | Synonyms would each cost money every day |
| Email | Brevo free tier, "about 300 recipients" | Brevo free tier with a **200-subscriber cap** and a waitlist | The 300/day limit includes confirmation emails, and every subscriber gets mail on both send days |
| Staging | Same VM (§2) or a second VM (Phase 5) | No standing staging. CI runs end-to-end tests on a throwaway stack; production gets a smoke test and rollback | Lean ops on a 4 GB host |
| Phase order | Pipeline hardening was Phase 4 | Pipeline and cost work is Phase 2 | Cost per topic is the unknown most likely to change the launch; measure it first |
| Beta | 10–20 people, 2 weeks | About 20 people, **3 weeks** | About 6 send days of cost data; doubles as the sending domain's warm-up |
| Workflow | One or more pull requests per phase | Commits straight to `main`, CI green on each | The owner's normal workflow |

---

## 1. Constraints everything else derives from

| Constraint | Value | Where it comes from |
|---|---|---|
| OpenAI spend | $20/month, hard | Owner's budget |
| Subscribers | 200 maximum; a waitlist after that | Brevo's free 300/day: 200 digests on a send day leaves about 100 for confirmation and manage-link emails |
| Topics per subscriber | 5 | v1 default, kept |
| Papers per topic per email | 1–10, default 5 | Today it's uncapped. A bounded number bounds full-text cost |
| Global active topics | Set from measured cost (below); placeholder 100 | Budget |
| Cadence | Twice-weekly: Mon and Thu 08:00 `America/New_York` | §0 |
| Audience | US residents, age 13 or older | Terms |
| Host | One Hetzner VM in a US region, about 2 vCPU / 4 GB | §3.8 |

**Cost model.** Sends happen 2 × 52 / 12 ≈ 8.7 times a month.

```
monthly OpenAI ≈ active_topics × (30 × ingest_cost_per_topic_day
                                  + 8.7 × prep_cost_per_topic_send)
                 + signup costs (query embeddings for previews; negligible)
```

- `ingest_cost_per_topic_day`: embedding and rating each new paper on its abstract.
- `prep_cost_per_topic_send`: full-text chunk embeddings and summaries for that
  topic's top K papers (K = the largest papers-per-topic among its subscribers),
  plus one overview.

Both are measured in Phase 2 and again in the beta. The global topic cap is then
`floor(0.8 × $20 / measured monthly cost per topic)`, which leaves 20% headroom
for previews, retries and variance. If that number comes out below about 50, revisit
the levers in §5 before launch rather than after.

---

## 2. Where we are: gap audit

The pipeline is solid for one trusted user on a laptop. These are the parts that
assume that, and have to change before strangers can use it:

| Area | Today | Needed for public |
|---|---|---|
| Access control | Every endpoint is open. Anyone can delete topics or trigger digests, and `GET /topics/{id}/subscriptions` returns subscribers' emails | Public pages only expose signup and self-service. The existing JSON API becomes admin-only |
| Identity | A subscription is just an email string. Nothing proves the person owns it | Double opt-in confirmation; signed links to manage or unsubscribe; no passwords |
| Unsubscribe | None. Emails have no unsubscribe link | One-click unsubscribe (a link plus `List-Unsubscribe` headers) and a manage link in every email. Probably not legally required for a free, ad-free digest (§7), but mailbox providers expect it, and it's what keeps spam complaints down |
| Topics | Global, unique by name, created through the admin API | Created on demand from user input. Deduplicated by normalized query, with similar existing topics suggested. Each subscription keeps its own display label |
| What gets fetched | Every topic, every day | Only topics with at least one confirmed, active subscriber |
| arXiv politeness | A process-wide, thread-safe gate already spaces the worker's arXiv API and HTML requests 3s apart (`app/services/arxiv.py`) | Keep exactly one process making arXiv requests: previews run as activities on the worker, never in the web processes. Full text only for emailed papers, so the daily volume stays small |
| LLM | OpenAI `gpt-5.4-mini`, no record of spend | Token usage logged, a spend guard, rating split from summarizing (§3.5) |
| Embeddings | Ollama with `embeddinggemma`, running natively on the Mac | An OpenAI embedding model, so nothing runs on the Mac |
| Email | Mailpit locally | Brevo on your own domain with SPF/DKIM/DMARC, bounce and complaint handling, and a warm-up period |
| Hosting | docker-compose on a laptop. Postgres (with the default password), Temporal and its unauthenticated UI, and Mailpit are all published on every network interface | Always-on host. Only HTTPS is public; secrets come from the environment; backups run |
| Abuse | Nothing stops anyone from subscribing someone else's address or creating 1,000 topics, and each topic now costs money | CAPTCHA on signup, rate limits, per-subscriber and global topic caps, and the spend guard |
| Legal | None | US-only terms, a privacy policy that meets CalOPPA, self-serve deletion, arXiv attribution |

What carries over unchanged: digest workflows, retries, ranking, email
rendering, the email workflow's per-recipient failure isolation, retrieval
features, the eval harness, and the test harness (real Postgres + time-skipping
Temporal).

---

## 3. Target architecture

```
Browser ──HTTPS──▶ Caddy (TLS) ──▶ FastAPI ──▶ Postgres (+ pgvector)
                                     │  ├─ public site (Jinja2 + htmx pages)
                                     │  ├─ token endpoints (confirm / manage / unsubscribe)
                                     │  ├─ Brevo webhooks (bounces, complaints)
                                     │  └─ admin JSON API + stats page (token-protected)
                                     ▼
                                  Temporal ◀── Worker (one process) ──▶ arXiv API + HTML (shared gate, 1 request / 3 s)
                                                        ├──▶ OpenAI (ratings, summaries, overviews, embeddings)
                                                        ├──▶ OpenAlex / Semantic Scholar (h-index)
                                                        └──▶ Brevo (SMTP)
```

### Key decisions

1. **Frontend: server-rendered pages in the existing FastAPI app (Jinja2 +
   htmx), not a separate SPA.** About six forms and status pages. One deployable,
   no CORS, one test client. htmx covers the live topic preview and the
   similar-topic suggestions.

2. **Identity: the email address is the account, with no passwords.**
   - Signup sends a confirmation link.
   - Every digest carries a signed manage link, and the site has a "send me my
     manage link" form.
   - Links are HMAC-signed tokens bound to a purpose (confirm, manage,
     unsubscribe), with expiry where it makes sense.
   - Revoking means bumping a per-subscriber token version.

3. **Topics are shared, labels are personal.**
   - `Topic` gets a unique `normalized_query`: trimmed, lowercased, whitespace
     and quotes collapsed.
   - Each `Subscription` gets a user-facing `label`.
   - Only the normalized query reaches the LLM, never a user-chosen label.
   - Topic descriptions, which feed the rating prompt, are admin-only. Users
     never write text that ends up in a shared prompt.
   - **Similar-topic suggestions:** each topic stores an embedding of its
     query. As someone types a new query, the signup form embeds it and offers
     existing topics above a calibrated cosine threshold ("Join the existing
     *RLHF* topic?"). Accepting reuses that topic and keeps the user's label.

4. **LLM and embeddings: OpenAI, all on the server.**
   - Summaries, ratings and overviews stay on `gpt-5.4-mini`.
   - Embeddings move to an OpenAI embedding model, chosen in Phase 2;
     `text-embedding-3-small` is the obvious candidate. That means:
     - a migration for the new vector width,
     - a backfill, using the existing side-by-side `EMBEDDING_MODEL` support,
     - a re-run of the retrieval evals to recalibrate the 0.75
       similar-paper cutoff and confirm the neighbours-share-a-topic rate
       (89% on `embeddinggemma`).
   - Ollama stays available as a local development backend only.
   - `_MAX_CONCURRENT_SUMMARIES` (4, set for a local Ollama server) gets
     re-tuned for OpenAI's rate limits.

5. **Two-pass pipeline: cheap every day, expensive only for emailed papers.**
   - **Daily ingest (21:30 ET, after arXiv's ~20:00 ET announcement).** For each
     active topic: fetch new papers from the arXiv API, embed their abstracts,
     and rate relevance from the abstract. Rating gets its own short call, split
     from summarizing. It must not fall below today's ρ = 0.77 against the
     reference labels (re-run the ranking eval).
   - **Send prep (Mon and Thu 01:00 ET).** For each topic, rank its papers in
     the send window and take the top K, where K is the largest papers-per-topic
     among its subscribers. For those papers only: fetch arXiv HTML, embed
     chunks, and write full-text summaries. Then write the topic's one overview,
     history-aware as today.
   - **Send (Mon and Thu 08:00 ET).** Assemble each subscriber's email from
     stored content, with no LLM or arXiv calls at send time. A paper whose
     full-text step failed falls back to an abstract-only summary.
   - **arXiv load.** Ingest takes about topics × pages × 3s, around 5 minutes at
     100 topics. Prep takes about topics × K × 3s, around 25 minutes at 100
     topics with K = 5, or 50 minutes at K = 10. Both fit well inside the 7-hour
     prep window.
   - **Previews never wait behind a backlog.** Background workflows await each
     arXiv request before scheduling the next, so the gate holds at most one
     background request plus any previews. A preview waits at most about 6s plus
     its own fetch. Results are cached for 1h per normalized query.

6. **Cost control.**
   - **Usage log.** Every OpenAI call writes an `llm_usage` row: model, purpose
     (rating / summary / overview / embedding / preview), topic, input and
     output tokens, and computed cost. Per-model prices are settings, not code
     constants.
   - **Spend guard**, on month-to-date cost:
     - At **80%** of the cap, new topics go to a waitlist. Signups that only use
       existing topics still go through. You get an alert.
     - At **100%**, LLM calls stop. Digests still send, ranked without AI text
       (the existing `SUMMARY_BACKEND=none` path), until the 1st of the month.
       You get an alert.
     - Raising the cap is a single setting.
   - **OpenAI backstop.** A dedicated production project and key, with its
     monthly budget set to $20. Check whether OpenAI's project budget hard-stops
     or only notifies. Either way, the guard in the app is the real stop.
   - **Topic cap** derived from measured cost (§1).

7. **Email: Brevo free tier over SMTP.**
   - 200-subscriber cap with a waitlist.
   - Confirmation and manage-link emails rate-limited per address.
   - Move to Amazon SES (pay per use) when the waitlist starts to matter.
   - Check Brevo's sending policy for footer requirements before Phase 3's
     templates are final.

8. **Hosting: one Hetzner VM in a US region (Ashburn or Hillsboro).**
   - The CX23 may only be sold in EU regions. Pick the closest 2 vCPU / 4 GB
     plan Hetzner offers in the US. With embeddings off the box and no staging
     stack, 4 GB should be enough; confirm it in Phase 2's rehearsal.
   - Production compose file with Caddy for automatic TLS. No standing staging
     (§0).
   - Temporal stays on its dev-server binary with its SQLite file: single-node,
     fine at this scale, and schedules rebuild themselves on startup.
   - Postgres is self-hosted, with a nightly `pg_dump` off-site.

---

## 4. Phases

Each phase ships as one or more commits to `main`. Each keeps the full suite
green and adds its own tests (listed per phase). A phase is done only when its
exit criteria pass.

### Phase 0: Owner actions (this week, in parallel with Phase 1)

These have lead times, so start them now:
- **Name and domain.** Pick from the shortlist and register it.
- **Brevo.** Create the account, authenticate the sending subdomain (SPF,
  DKIM), and publish DMARC at `p=none` with reports. The beta doubles as the
  domain's warm-up: about 20 recipients, then a gradual ramp at launch.
- **OpenAI.** A production project with its own key and a $20 monthly budget.
- **Hetzner.** Create the account and confirm which US-region plan to use.
- **Cloudflare.** A Turnstile site key for the domain.

### Phase 1: Multi-user data model and lockdown

**Build**
- `Subscriber` table: `email` (unique, case-insensitive), `confirmed_at`,
  `unsubscribed_at`, `token_version`, `created_at`, `last_manage_link_sent_at`,
  `us_resident_attested_at`.
- `Subscription` references `subscriber_id` instead of holding `email`, and
  gains `label`. The unique constraint becomes `(topic_id, subscriber_id)`.
- `Topic` gains `normalized_query` (unique). Name uniqueness is dropped.
  `description` stays admin-only.
- **Cadence:**
  - The migration sets every subscription, including the owner's, to
    `twice_weekly`.
  - Public schemas don't accept a cadence; the admin API still can.
  - The column stays, so cadence choice can come back later without a
    migration.
- `max_papers` is bounded to 1–10 (default 5) on public input.
- **Data migration:** create a subscriber per distinct existing email, marked
  confirmed (that's you). Existing topics get a normalized query; merge any
  duplicates.
- **Active-only filtering:** `list_active_topic_ids` returns only topics with
  at least one active subscription from a confirmed, not-unsubscribed
  subscriber. `list_due_subscriptions` applies the same filter.
- The existing JSON API moves behind an `X-Admin-Token` header (constant-time
  comparison). The endpoint that lists subscriber emails becomes admin-only.
- **Limits as settings:**
  - max subscribers (200),
  - max topics per subscriber (5),
  - max active topics globally (placeholder 100 until Phase 2 measures cost),
  - max query length (200 characters),
  - allowed characters.

**Tests**
- Migration: upgrade from the current head on a seeded copy of today's data,
  then downgrade, then upgrade again. Data is preserved; duplicate topics
  merge; every subscription ends up `twice_weekly`.
- Normalization (unit): `"RLHF"`, `" rlhf "`, `'"RLHF"'` all map to one topic;
  `cat:cs.LG` passes through unchanged.
- Workflow: an unconfirmed or unsubscribed subscriber is never emailed, and
  their topic isn't fetched unless someone else needs it.
- API: every admin route returns 401 without a token or with a wrong one;
  nothing public returns an email address, a topic description, or accepts a
  cadence.

**Exit:** the existing suite passes on the new model, and the current
deployment migrates with no data loss.

### Phase 2: Pipeline rework and cost control

**Build**
- **Usage log and spend guard** (§3.6): the `llm_usage` table, recording in the
  one place every OpenAI call goes through (`_complete` and the embedder),
  month-to-date queries, the 80% and 100% behaviour, and alerts.
- **OpenAI embeddings** (§3.4):
  - an OpenAI embedder, the vector-width migration, and the backfill;
  - re-run the retrieval evals and recalibrate the thresholds;
  - topic-query embeddings, with a calibrated threshold for similar-topic
    suggestions.
- **Two passes** (§3.5):
  - Split rating from summarizing, then re-run the ranking eval.
  - New schedules in `America/New_York`: ingest 21:30 daily, prep 01:00
    Mon/Thu, send 08:00 Mon/Thu. Update `SEND_TIME_ZONE_NAME` and `DAILY_CRON`.
  - The send workflow stops running its own digest pass; it assembles what prep
    stored.
  - Full text is fetched only in prep, for the top K.
- **One arXiv process.**
  - Make sure exactly one worker process makes arXiv requests.
  - Previews (Phase 3) run as activities on that worker.
  - Background fetches are awaited one at a time.
- **Bounded fan-out.** `RunAllTopicDigestsWorkflow` and prep run their children
  in batches rather than all at once.
- **Overviews:** one per topic per send, written in prep. Every subscriber is on
  the same cadence, so the send window is the same for everyone.

**Tests**
- Usage log: every OpenAI call path, faked with `respx`, writes exactly one
  usage row with the right purpose and tokens.
- Spend guard (time-skipping):
  - at 80%, a new topic is waitlisted and an existing one still runs;
  - at 100%, the next send goes out with no LLM calls and no AI text;
  - on the 1st of the month, the next run makes LLM calls again.
- arXiv: across 50 topics' ingest and prep, recorded through a fake, there is
  never more than one request in flight and always at least 3s between
  requests. A preview issued mid-prep completes within about 10s of fake time.
- Prep fetches full text only for each topic's top K papers, where K comes
  from its subscribers' settings.
- Send makes no LLM or arXiv calls (the fakes assert it). A paper whose
  full-text step failed is sent with its abstract summary.
- Evals (manual; they cost money): the ranking eval stays at ρ ≥ 0.77 with the
  split rating call; the retrieval evals hold on the new embedding model.
- **Cost and load rehearsal**, run locally with fakes for every external call:
  - Seed 200 subscribers and 100 topics, then run ingest, prep and send end to
    end.
  - Record wall-clock time per stage, DB query counts and peak memory, and
    check memory fits a 4 GB host.
  - Then run once against real OpenAI on 10 of your own topics for a week, to
    measure `ingest_cost_per_topic_day` and `prep_cost_per_topic_send`.

**Exit:** the rehearsal fits the time windows and the memory budget; measured
per-topic cost gives a global topic cap of at least 50, or the §5 levers have
been applied until it does; the cap is set.

### Phase 3: Signup, confirmation and self-service (backend)

**Build**
- Tokens: `SECRET_KEY` setting, `itsdangerous` (or stdlib HMAC). Purposes:
  - `confirm`: expires in 48h.
  - `manage`: long-lived, invalidated by bumping `token_version`.
  - `unsubscribe`: per subscription, never expires, so old emails' links keep
    working.
- `POST /signup` (email, topics with labels, papers per topic, US-residency
  checkbox, Turnstile token):
  - Creates or updates a pending subscriber and its subscriptions, then sends a
    confirmation email through a small Temporal workflow (reusing the retry
    policies).
  - Enumeration-safe: the response is identical whether or not the email
    exists.
  - An already-confirmed address gets a manage link instead of a second account.
  - At the subscriber cap, or when a new topic would pass the topic cap or the
    80% spend line, the signup is waitlisted and the response says so.
- `GET /confirm?token=`: activates the subscriber. The first digest arrives at
  the next Mon/Thu 08:00 ET send.
- Manage page (`/manage?token=`): edit topics, labels and papers per topic;
  pause; "delete my data", which cascades to the subscriber's subscriptions
  but never to shared topics.
- Unsubscribe:
  - `POST /unsubscribe?token=` implements one-click unsubscribe (RFC 8058). It
    must work with no cookies and no confirmation step.
  - `GET` shows a confirmation page with a re-subscribe option.
- Digest emails get `List-Unsubscribe` and `List-Unsubscribe-Post` headers, plus
  a footer with manage and unsubscribe links.
- Topic preview, `GET /preview?q=`:
  - Shows the most recent ~5 papers the query would match, plus similar
    existing topics.
  - Runs on the worker through the arXiv gate (§3.5), and is cached for 1h per
    normalized query.
  - A query with no matches in 30 days shows a warning before signup, not after
    the first empty email.
- Abuse controls:
  - Cloudflare Turnstile on signup.
  - Rate limits, stored in Postgres so they survive restarts and work across app
    workers:
    - per IP: e.g. 5 signups/hour and 30 previews/hour (previews cost an
      embedding call);
    - per email, for confirmation and manage-link resends: e.g. 3/day.

**Tests**
- Tokens (unit): round-trip; tampered signature, expired token, wrong purpose,
  stale `token_version` and a token for a deleted subscriber are all rejected.
- Flows (integration, TestClient + real Postgres + fake email sender):
  - signup → confirmation email captured → confirm → active
  - signup without the residency checkbox is rejected
  - signup at the subscriber cap is waitlisted
  - manage edits
  - one-click `POST /unsubscribe` with no session
  - delete data removes all of the subscriber's rows and leaves shared topics
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

### Phase 4: The website

**Build**
- Pages:
  - Landing page: what it is, a sample digest, how papers are ranked, arXiv
    attribution, "for US residents".
  - Signup: email; up to 5 topics, each with a live preview, similar-topic
    suggestions and a label; papers per topic; the residency checkbox; links to
    the terms and privacy policy. No cadence picker: "Digests arrive Monday and
    Thursday mornings".
  - "Check your inbox", waitlisted, confirmed, manage, unsubscribed, privacy,
    terms.
- Plain semantic HTML, a classless CSS base, works without JavaScript except
  the preview. Mobile first, light and dark.

**Tests**
- Page tests (TestClient): every page renders 200 with expected content and
  escapes user input; form validation errors render inline.
- End-to-end (Playwright, Python) against a throwaway compose stack:
  - Fill the signup form, then fetch the confirmation email through Mailpit's
    HTTP API and click its link.
  - Trigger a send with fake arXiv and LLM data, then follow the email's
    unsubscribe link.
  - Runs in CI on every push to `main`; it takes a few minutes.
- Accessibility: an axe-core scan via Playwright on each page, with zero serious
  or critical issues. Keyboard-only signup works.
- Phone-width viewport: no horizontal scroll, and the form is usable.

**Exit:** the end-to-end suite passes in CI, and a manual pass on a real phone.

### Phase 5: Production infrastructure and operations

**Build**
- `docker-compose.prod.yml`:
  - Only Caddy publishes ports (80/443). Postgres, Temporal, its UI and the
    worker stay on the internal network; reach the Temporal UI over an SSH
    tunnel.
  - No `--reload`, no source volume mounts; images are built in CI. Several
    uvicorn workers; exactly one worker process (§3.5).
  - Secrets live in a server-side env file (never committed): Postgres
    password, `SECRET_KEY`, admin token, OpenAI key, SMTP credentials, Turnstile
    secret, Brevo webhook secret.
- DNS: A/AAAA records and the Brevo records from Phase 0. Tighten DMARC to
  `quarantine` after two clean weeks of reports.
- Backups: nightly `pg_dump` to off-site object storage, 30-day retention.
  Temporal's SQLite volume is backed up too, though it's less critical: schedules
  recreate themselves and digests are rebuilt from Postgres.
- Deploys (no standing staging):
  - GitHub Actions on push to `main`: tests → end-to-end on a throwaway stack →
    build image → push to GHCR.
  - Deploy to production with a manual `workflow_dispatch`. Migrations run as a
    one-off step before the new containers start.
  - The smoke test runs right after. On failure, roll back to the previous image
    tag with one command. Migrations must be backward-compatible with the
    previous image (expand, then contract) so rollback never needs a downgrade.
- Observability:
  - Structured JSON logs; Sentry's free tier for exceptions.
  - An uptime check on `/health`, extended to check DB and Temporal
    connectivity.
  - Alerts:
    - ingest or prep failures;
    - send failures, or no successful send by 09:00 ET on send days;
    - spend at 80% and 100%;
    - bounce rate over 2%;
    - subscriber count at 90% of the cap.
  - A small admin stats page: subscribers, waitlist, topics, emails sent,
    month-to-date spend and cost per topic.

**Tests**
- A post-deploy smoke script, run by CI against production:
  - `/health` is green.
  - The signup and landing pages render.
  - The admin stats endpoint responds with the admin token.
- Restore drill before launch, then quarterly: restore last night's dump into a
  scratch database, run migrations, run read-only checks. It counts only if it
  actually restores.
- Deliverability, manual, before launch:
  - mail-tester.com score of 9/10 or better.
  - Gmail and Outlook inbox placement for a test digest.
  - SPF, DKIM and DMARC all pass in raw headers.
- Port scan of the production host: only 22 (key-only), 80 and 443 open.
- Rollback rehearsed once: deploy a deliberately broken image and roll back.

**Exit:** production deploys from CI; rollback rehearsed; restore drill done;
each alert tripped once (on a local stack or production with test data).

### Phase 6: Legal, privacy and launch

**Build**
- **Terms of service:**
  - US residents only;
  - not for children under 13;
  - no warranty, and summaries are AI-generated and may be wrong;
  - acceptable use;
  - the service can be changed or shut down.
- **Privacy policy**, written to satisfy CalOPPA (California's law requiring a
  visible privacy policy on sites that collect personal information):
  - what's collected: email, topics, labels, papers per topic, send history,
    the IP addresses used for rate limiting and how long they're kept;
  - who it's shared with (processors): Hetzner (hosting), Brevo (email), OpenAI
    (sees topic queries and paper text, never email addresses), Cloudflare
    (Turnstile), Sentry, and the backup storage provider;
  - how to review, change or delete your data (the manage page);
  - how the site responds to Do Not Track signals;
  - an effective date, and how changes are announced.
- **Email practices.** No postal address; this rests on the digest being free
  and ad-free, which makes it very likely not "commercial" under CAN-SPAM. If
  sponsorship or promotion is ever added, add a postal address first.
  Unsubscribes take effect immediately.
- **Attribution:**
  - arXiv: "Thank you to arXiv for use of its open access interoperability",
    with no implied endorsement;
  - credit OpenAlex and Semantic Scholar;
  - check that the arXiv, OpenAI and Brevo terms are all followed.
- **Closed beta: about 20 people for 3 weeks.**
  - Watch bounces, spam complaints, cost per topic against the Phase 2
    estimate, and whether the ranking feels right.
  - The beta is also the domain's warm-up.
- **Launch.**
  - Open signup with the topic, subscriber and spend caps in place.
  - Announce in small batches rather than all at once, so sending volume ramps
    gradually.

**Exit:** a beta with no unresolved deliverability or cost surprises; the launch
checklist (§6) signed off.

---

## 5. Cost levers (if the topic cap comes out too low)

In rough order of how little each costs in quality:
1. Lower the default papers per topic (5 → 3); this shrinks K and the
   full-text work.
2. Lower `ARXIV_MAX_RESULTS` for ingest (fewer abstracts rated per topic).
3. Cap the full-text excerpt budget per summary (fewer input tokens).
4. Turn off overviews for topics with fewer than N subscribers.
5. Abstract-only summaries for the lowest-ranked of the top K.
6. Raise the budget.

Fixed monthly costs besides OpenAI: the VM (about €5–7), the domain (about
$10–20/year), off-site backup storage (free tier), Brevo (free), Sentry (free).

---

## 6. Launch checklist

- [ ] All phase exit criteria met
- [ ] Admin token, `SECRET_KEY`, database password and OpenAI key rotated from
      any value used in development
- [ ] Port scan clean; Temporal UI not public
- [ ] Restore drill passed within the last 7 days
- [ ] SPF, DKIM and DMARC pass; mail-tester score of 9 or better
- [ ] Spend guard tested at 80% and 100%; OpenAI project budget set
- [ ] Global topic cap set from measured cost per topic; subscriber cap at 200
- [ ] Terms (US-only, age 13+) and a CalOPPA-compliant privacy policy live
- [ ] Beta feedback triaged; no open P0/P1 issues

---

## 7. Decisions

| # | Decision | Status |
|---|---|---|
| 1 | Goal | A small real service: public, deliberately capped, lean ops (decided 2026-11-12) |
| 2 | Budget | $20/month hard cap on OpenAI; spend guard closes new topics at 80% and drops AI text at 100% (decided 2026-11-12) |
| 3 | LLM | OpenAI `gpt-5.4-mini` for ratings, summaries and overviews (decided 2026-11-05) |
| 4 | Embeddings | An OpenAI embedding model; thresholds recalibrated by eval (decided 2026-11-12) |
| 5 | Full text | Only for papers that rank into an email (two-pass pipeline) (decided 2026-11-12) |
| 6 | Topics | Free text with live preview, similar-topic suggestions by embedding, limits; `cat:` prefix still supported (decided 2026-11-12) |
| 7 | Cadence | Twice-weekly for everyone at launch; per-subscriber cadence deferred (decided 2026-11-12) |
| 8 | Send time | Mon and Thu 08:00 Eastern, one slot for everyone (decided 2026-11-12) |
| 9 | Audience | US residents only, through the terms plus a signup checkbox; no IP geo-blocking (decided 2026-11-12) |
| 10 | Legal | No postal address or virtual mailbox; CalOPPA-compliant privacy policy; CAN-SPAM practices followed voluntarily (decided 2026-11-12; not legal advice) |
| 11 | Email | Brevo free tier over SMTP, 200-subscriber cap with a waitlist; SES if outgrown |
| 12 | Hosting | One Hetzner VM in a US region, self-hosted Postgres with off-site dumps; no standing staging (decided 2026-11-12) |
| 13 | Beta | About 20 people for 3 weeks, doubling as the domain warm-up (decided 2026-11-12) |
| 14 | Name and domain | **Open**: owner picks from the shortlist this week (Phase 0) |
