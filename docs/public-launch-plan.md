# Public launch plan

Turning the research digest agent into a small public service for US users:
anyone visits the site, enters their email, up to five topics and how many
papers they want per topic, confirms by email, and gets a twice-weekly digest.
Built on the existing pipeline (FastAPI, Postgres, Temporal, the arXiv → LLM
rating/summary → ranked email flow), not a rewrite.

Status: v2.2, 2026-11-12. v2.2 adds OpenAlex as a second paper source
(Phase 2, "2c+"; decisions 21–24). v2.1 folds in an audit of v2 against the code,
the live data and current vendor terms (changes listed in §0.1). v2 replaced
the 2026-10-06 draft, which assumed summaries ran on local Gemma on the
owner's Mac. Both are in git history.

---

## Who it's for and what success means

**The reader.** Someone with a real tie to a field: they work in it now, or
used to. They don't want to read everything. They want to stay connected
without watching arXiv themselves.

**What every email has to do**, in order of importance:
1. **Show where the topic is going.** The overview is the heart of the email,
   not garnish. Someone who reads only the overview should still learn
   something real about the period's research.
2. **Sell the papers worth reading.** The overview and each paper's short
   summary should make the one or two papers that matter to this reader stand
   out, so they actually open them.
3. **Leave the reader feeling they learned something.** That is what keeps
   them subscribed.

**Anti-goal.** Emails that feel generic or interchangeable: the reader isn't
interested in the overview, no paper stands out, and they quietly stop
opening them. An email that is technically correct but forgettable is a
failure.

**How the overview mentions papers.** Freely. There is no required list of
papers to name. When the overview states a specific finding, it cites the
paper it came from, wherever that paper ranks, and the citation is a link to
arXiv. So the pointer works even when that paper isn't in this reader's list.

**Persuasive, never inflated.** "Selling" means making real findings vivid
and saying why a paper might matter. It never means claiming more than the
abstract supports. The eval harness checks both (Phase 2).

---

## 0. What changed from v1, and why

| Area | v1 | v2 | Why |
|---|---|---|---|
| LLM | Gemma on the Mac, fed by a task queue (Tailscale, launchd, `LLM_HOLD_DAYS`) | OpenAI `gpt-5.4-mini` on the server (shipped 2026-11-05). No Mac anywhere in production | Already moved. Better ratings and 2–3× lower latency (README eval table) |
| Embeddings | Ollama on the Mac (not addressed) | An OpenAI embedding model, called from the server | Removes the last dependency on the laptop |
| Budget | None ("no paid LLM") | **$20/month hard cap** on OpenAI, enforced in the app | Per-token cost grows with the number of distinct topics |
| Cost visibility | None: token usage isn't recorded | Every OpenAI call logged with its tokens and cost | The cap and the topic limit need real numbers |
| Full text | Fetched for every new paper | Only for papers that make it into an email (two passes, §3.5). *Superseded in v2.1: off at launch (§0.1)* | At 200 topics, v1 meant about 4h a day of arXiv requests at 1 per 3s. Also saves tokens |
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

### 0.1 What changed from v2 to v2.1 (audit, 2026-11-12)

| Area | v2 | v2.1 | Why |
|---|---|---|---|
| Full text | Fetched for each topic's top K at send prep | **Off at launch: summaries and overviews use abstracts only.** The full-text code stays behind `RAG_FULLTEXT` (default off). The owner asks arXiv support (Phase 0); if they allow it, turning it back on is a setting plus an eval re-run | arXiv's API terms allow retrieving, storing and using e-print content "for your own personal use, or for research purposes". A public service is a grey area. Metadata, abstracts included, is CC0 |
| Host | Hetzner, US region, "about €5–7" | **Hetzner EU (Nuremberg or Helsinki), CX23, 2 vCPU / 4 GB, about €6/month.** Fallback: Hetzner's EU ARM plan (CI then builds ARM images) | The cheap CX line isn't sold in the US. The cheapest US 4 GB plan (CPX21) is €32.49/month. Server location doesn't matter for an email service. Running on the Mac was considered and rejected: unsubscribe links, confirmation links, Brevo webhooks and the 08:00 ET send all need an always-on host |
| Papers per topic | 1–10, default 5 | **5–10, default 5** | Owner's call: few enough to stay focused on the highest-value papers |
| Overview | Written from the top K's summaries | Written from the abstracts of the period's highest-ranked papers (up to 25), cites papers by arXiv ID wherever a specific finding is stated, citations become links | The overview should reflect the whole period and point to whichever paper a finding came from, not just the reader's top K |
| Summary and overview prompts | Unchanged | Rewritten to sell papers (why it's worth reading; vivid, concrete findings) without overclaiming, checked by the evals | The reader-facing goal above |
| Topic queries | Free text, `cat:` prefix supported | **Phrase topics only for the public.** Field-prefixed queries (`cat:`, `ti:`, ...) are admin-only. **Phrases matching more than about 25 papers a day are refused at signup** with a prompt to narrow them | Anything over the 25-results-per-fetch limit silently drops papers (`cat:math.NA` returned exactly 25 on three days in a row), and costs two to three times as much per topic. Broad phrases like "large language models" have the same problem |
| Cost estimate | Unknown; placeholder topic cap 100 | Rough estimate about $0.15–0.20 per topic per month abstract-only at K = 5 (below). The placeholder stays until Phase 2 measures it | Estimated from the real prompt sizes, an average abstract of ~1,300 characters, and `gpt-5.4-mini` at $0.75 / $4.50 per million input / output tokens |
| OpenAI backstop | "Check whether the project budget hard-stops" | Projects support a hard monthly limit (since July 2026) that returns 429 `project_spend_limit_exceeded`. Set it slightly above the app's cap; the app treats that error as "budget exhausted", never retries it | Verified against OpenAI's spend-limits docs |
| Embeddings | New vector width, migration | `text-embedding-3-small` with `dimensions=768` fits the existing `vector(768)` column: no width migration | The model supports shortened embeddings |
| Recalibration | The 0.75 similar-paper cutoff | Also `rag_min_similarity` (0.72), `rag_near_duplicate_similarity` (0.9) and the ranking novelty anchors (0.85 / 0.45 in `app/services/ranking.py`). EmbeddingGemma's task prompts (`document_text`, `query_text`) are not applied to OpenAI | All of them were tuned to EmbeddingGemma's similarity scale |
| arXiv gate | "Background fetches are awaited one at a time" | A dedicated arXiv task queue whose worker runs one activity at a time, and a separate preview task queue with its own threads | Today every topic's fetch starts at once; `fetch_arxiv`'s 60s timeout counts time spent waiting on the gate, so beyond about 20 topics fetches time out and retry. The gate's lock also has no ordering, so a preview could wait indefinitely |
| Send windows | Per-subscriber `last_sent_at`; "the send window is the same for everyone" | Windows are defined by send slots: each send covers papers ingested since the previous slot. `last_sent_at` is set at confirmation, not at signup | Per-subscriber watermarks made windows differ (new, paused or failed-send subscribers), which breaks one shared overview per topic |
| Images | `temporalio/admin-tools:latest`, `axllent/mailpit:latest` | Pinned versions in production | Reproducible deploys and rollbacks |

---

## 1. Constraints everything else derives from

| Constraint | Value | Where it comes from |
|---|---|---|
| OpenAI spend | $20/month, hard | Owner's budget |
| Subscribers | 200 maximum; a waitlist after that | Brevo's free 300/day: 200 digests on a send day leaves about 100 for confirmation and manage-link emails |
| Topics per subscriber | 5 | v1 default, kept |
| Papers per topic per email | 5–10, default 5 | Owner's call. Today it's uncapped; a bounded number bounds summary cost. The minimum of 5 guarantees every reader gets a topic's picks (§3.9) |
| Global active topics | Set from measured cost (below); placeholder 100 | Budget |
| Topic queries (public) | Phrases only; field-prefixed queries (`cat:`, `ti:`, ...) admin-only | §0.1 |
| Cadence | Twice-weekly: Mon and Thu 08:00 `America/New_York` | §0 |
| Audience | US residents, age 13 or older | Terms |
| Host | One Hetzner CX23 (2 vCPU / 4 GB) in the EU, about €6/month | §3.8 |
| arXiv content | Metadata and abstracts only at launch; no full-text fetching until arXiv says it's allowed | §0.1 |

**Cost model.** Sends happen 2 × 52 / 12 ≈ 8.7 times a month.

```
monthly OpenAI ≈ active_topics × (30 × ingest_cost_per_topic_day
                                  + 8.7 × prep_cost_per_topic_send)
                 + signup costs (query embeddings for previews; negligible)
```

- `ingest_cost_per_topic_day`: embedding and rating each new paper on its abstract.
- `prep_cost_per_topic_send`: abstract-only summaries for that topic's top K
  papers (K = the largest papers-per-topic among its subscribers), plus one
  call that writes the overview and chooses the picks.

**Rough estimate (v2.1, before measurement).** A rating call is about 400
input tokens and a few output tokens: about $0.0003 per paper. A phrase topic
gets about 3–5 new papers a day: about $0.04 a month. An abstract-only summary
is about 500 input and 170 output tokens: about $0.0011. An overview reading up
to 25 abstracts is about 9,000 input and 300 output tokens: about $0.008. At
K = 5 that is about $0.014 per send, or about $0.12 a month. Total: roughly
**$0.15–0.20 per topic per month** at K = 5, nearer $0.25 at K = 10. That would
put the cap around 65–100 topics. Retries, previews and rate-only calls aren't
in this estimate.

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
| arXiv politeness | A process-wide, thread-safe gate already spaces the worker's arXiv API and HTML requests 3s apart (`app/services/arxiv.py`). But every topic's fetch starts at once, and `fetch_arxiv`'s 60s timeout counts time spent waiting on the gate | Keep exactly one process making arXiv requests: a dedicated arXiv task queue run one activity at a time, plus a preview queue; never from the web processes. No full-text fetching at launch, so arXiv traffic is one search per active topic per day |
| arXiv terms | `app/services/fulltext.py` fetches full text under the "personal use" permission, with a `User-Agent` saying "personal research digest" | Abstract-only at launch (full text off). Update the `User-Agent` to describe the public service with a contact address. Nothing in the name or pages implies arXiv endorsement |
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
     A wrong suggestion costs little (the user declines), so a rough threshold
     from the few existing topics is enough to launch; tune it from beta data.
   - **Public queries are phrases only.** Normalization (trim, lowercase,
     collapse whitespace and quotes) applies to phrases. Field-prefixed queries
     (`cat:`, `ti:`, `au:`, ...) are rejected on public input and accepted
     unchanged, never lowercased, through the admin API.
   - A publicly created topic's `name` is its normalized query, since `name`
     goes into the rating and overview prompts. The user's wording lives only
     in their subscription's `label`.

4. **LLM and embeddings: OpenAI, all on the server.**
   - Summaries, ratings and overviews stay on `gpt-5.4-mini`.
   - Embeddings move to `text-embedding-3-small`, requested with
     `dimensions=768` so they fit the existing `vector(768)` column (no width
     migration). That means:
     - an OpenAI embedder that applies no EmbeddingGemma task prompts
       (`document_text` / `query_text` stay for the Ollama backend only);
     - a backfill, using the existing side-by-side `EMBEDDING_MODEL` support
       (rows are keyed by model), then a cut-over;
     - a re-run of the retrieval evals to recalibrate every threshold tuned to
       EmbeddingGemma's scale: the 0.75 similar-paper cutoff,
       `rag_min_similarity` (0.72), `rag_near_duplicate_similarity` (0.9) and
       the ranking's novelty anchors (0.85 / 0.45), and to confirm the
       neighbours-share-a-topic rate (89% on `embeddinggemma`).
   - Ollama stays available as a local development backend only.
   - `_MAX_CONCURRENT_SUMMARIES` (4, set for a local Ollama server) gets
     re-tuned for OpenAI's rate limits.
   - **Full text is off at launch** (`RAG_FULLTEXT=false` by default). The
     code stays; if arXiv allows it, prep fetches full text for the top K only,
     and the full-text evals are re-run before it's switched on.

5. **Two-pass pipeline: cheap every day, the expensive calls only for emailed
   papers.**
   - **Daily ingest (21:30 ET, after arXiv's ~20:00 ET announcement).** For each
     active topic: fetch new papers from the arXiv API, embed their abstracts,
     and rate relevance from the abstract. Rating is its own short call (the
     existing `rate_relevance`), split from summarizing. It must not fall below
     today's ρ = 0.77 against the reference labels (re-run the ranking eval).
   - **Send windows are defined by send slots.** A send's window for a topic is
     every paper the topic ingested since the previous send slot. Everyone on
     a topic gets the same window, so one overview and one set of picks per
     topic is correct for all of them. `last_sent_at` is set when a subscriber
     confirms (not at signup) and is still advanced on each successful send;
     it's used only to skip a subscriber who was already sent this slot. A
     subscriber who misses a slot (failed send, paused) gets the current
     slot's content next time, not a backlog.
   - **Send prep (Mon and Thu 01:00 ET).** For each topic with active
     subscribers:
     1. Look up author h-indices for the window's papers (moved here from send
        time) and rank them with the existing ranking.
     2. Write abstract-only summaries for the top K, where K is the largest
        papers-per-topic among its subscribers (5–10).
     3. One call writes the overview and chooses the picks (§3.9). It reads
        the abstracts of up to the 25 highest-ranked papers in the window, plus
        the topic's recent overviews (history-aware, `avoid_repeats`, as today).
        It returns the overview, with arXiv IDs cited wherever it states a
        specific paper's finding, and up to two picks from the top 5 with a
        one-line hook each.
     4. Store all of it, keyed to the topic and the slot.
   - **Send (Mon and Thu 08:00 ET).** Assemble each subscriber's email from
     stored content, with no LLM, arXiv or h-index calls at send time. A topic
     whose prep failed is sent with whatever was stored (ranked papers without
     AI text in the worst case), never held back.
   - **arXiv load.** Ingest is one search per active topic: about 5 minutes at
     100 topics. Prep makes no arXiv requests while full text is off.
   - **One arXiv request at a time, previews never stuck behind it.** The worker
     process runs a dedicated `arxiv` task queue with one activity slot for all
     background fetches, and a separate `preview` task queue with its own small
     thread pool for signup previews. Both go through the existing process-wide
     gate, so at most one background request plus any previews are ever
     waiting. Fetch timeouts are set so time spent in the queue never counts as
     a failed attempt. A preview waits at most about 6s plus its own fetch.
     Preview results are cached for 1h per normalized query in Postgres (the web
     app runs several processes, so an in-memory cache wouldn't be shared).

6. **Cost control.**
   - **Usage log.** Every OpenAI call writes an `llm_usage` row: model, purpose
     (rating / summary / overview / embedding / preview), topic, input and
     output tokens, and computed cost. Per-model prices are settings, not code
     constants.
   - **Spend guard**, on month-to-date cost:
     - At **80%** of the cap, new topics go to a waitlist. Signups that only use
       existing topics still go through. You get an alert.
     - At **100%**, chat-model calls (ratings, summaries, overviews and picks)
       stop. Embeddings keep running: they cost about a thousandth as much, and
       ranking, previews and similar-topic suggestions depend on them. Signups
       and previews keep working; new topics stay waitlisted. Digests still
       send, ranked without AI text, until the 1st of the month. You get an
       alert. *(Builder's call, v2.1.)*
     - **After the reset**, the next prep first rates any paper in its window
       that was ingested unrated, then proceeds as normal. That's a few days of
       ratings at once, a few cents per topic. *(Builder's call, v2.1.)*
     - Raising the cap is a single setting.
   - **OpenAI backstop.** A dedicated production project and key with an
     enforced hard monthly spend limit a little above the app's cap (for
     example $22). When it trips, OpenAI returns 429
     `project_spend_limit_exceeded`; the app treats that as "budget exhausted"
     (the 100% behaviour), marks it non-retryable, and alerts. The guard in the
     app is still the real stop.
   - **Topic cap** derived from measured cost (§1).

7. **Email: Brevo free tier over SMTP.**
   - 200-subscriber cap with a waitlist.
   - Confirmation and manage-link emails rate-limited per address.
   - Move to Amazon SES (pay per use) when the waitlist starts to matter.
   - Check Brevo's sending policy for footer requirements before Phase 3's
     templates are final. Third-party reviews say the free plan adds a Brevo
     logo to every email; launch with it and revisit (removal is a paid add-on
     of roughly $10–12/month).

8. **Hosting: one Hetzner CX23 in the EU (Nuremberg or Helsinki), about
   €6/month.**
   - The cheap CX line isn't sold in Hetzner's US regions, where the cheapest
     4 GB plan is €32.49/month. Server location doesn't matter for an email
     service; the privacy policy says where data is stored.
   - CX capacity is limited. If a CX23 can't be ordered, use Hetzner's EU ARM
     plan with 4 GB and have CI build `linux/arm64` images.
   - With embeddings off the box, no full text and no staging stack, 4 GB
     should be enough; confirm it in Phase 2's rehearsal.
   - Running on the owner's Mac was considered and rejected: unsubscribe and
     confirmation links, Brevo webhooks and the 08:00 ET send all need a host
     that is always on.
   - Production compose file with Caddy for automatic TLS. No standing staging
     (§0). Every image is pinned to a version (no `:latest`).
   - Temporal stays on its dev-server binary with its SQLite file: single-node,
     fine at this scale, and schedules rebuild themselves on startup.
   - Postgres is self-hosted, with a nightly `pg_dump` off-site.

9. **The email: picks first, then the overview, then the rest.** Chosen from
   mockups on 2026-11-12. For each topic section, in this order:
   - **Start here:** up to two picks chosen by the model from the topic's top 5
     (so every reader has them, since the minimum is 5). Each has a one-line
     hook saying why it's worth reading, and a link. A pick can be any of the
     top 5, not necessarily #1 by score.
   - **The period in brief:** the overview, about 80–120 words, stating concrete
     findings. Whenever it states a specific paper's finding it cites that paper,
     and the citation renders as an arXiv link, even if the paper isn't in this
     reader's list. There is no required number of citations.
   - **Also new:** the rest of the reader's top K, each with a one-line "why
     read it" and one or two concrete sentences on what it did. No "The paper
     addresses..." openers.
   - A header line per section: the topic label, the date range, and "N new
     papers this period, your top K below".
   - Footer: manage link, unsubscribe link, the arXiv thank-you, and the AI
     disclaimer.
   - With several topics, each section has its own picks. There is no
     cross-topic "start here".
   - **Prompts.** The summary prompt and the overview-and-picks prompt are
     rewritten to sell: concrete, vivid, and specific about why a paper might
     matter, never claiming more than the abstract supports. Picks and
     citations come back as structured output (arXiv IDs), and any ID not in the
     input is dropped, like the existing related-work citation check.
   - **Eval gates** (Phase 2), against the current prompts on the same papers:
     overview factual errors no worse than today's 1 in 25; every citation
     points to a paper whose abstract supports the claim; summaries judged more
     informative in blind pairwise review; no overclaiming beyond the abstract.
     The ranking eval is unaffected.

10. **The signup page: one page** (chosen from mockups on 2026-11-12). Email;
    up to 5 topics, each with its live preview, similar-topic suggestion,
    label and papers per email (5–10, default 5); the residency checkbox with
    links to the terms and privacy policy; one submit button; "Digests arrive
    Monday and Thursday mornings."

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
- **OpenAI.** A production project with its own key and an enforced hard
  monthly spend limit a little above $20 (§3.6).
- **Hetzner.** Create the account and confirm in the console that a CX23 can be
  ordered in Nuremberg or Helsinki (fallback: the EU ARM plan, §3.8).
- **Cloudflare.** A Turnstile site key for the domain.
- **arXiv.** Write to arXiv support: describe the free public digest and ask
  whether it may fetch arXiv's HTML full text to write summaries, given that
  only the summaries and links to arXiv are emailed and no e-print content is
  served. Full text stays off until they say yes. Claude can draft the message.
- **Sentry and backup storage.** Free-tier accounts (no lead time, needed by
  Phase 5).
- **OpenAlex API key.** Create a free key at openalex.org/settings/api and put
  it in `.env` as `OPENALEX_API_KEY`. Without one, every OpenAlex request from
  the server's IP shares $0.10 a day, at $0.001 per list request. The free key
  gives its own $1 a day. The existing h-index lookup already calls OpenAlex
  without a key, so the server needs this even before 2c+.
- **Name.** Nothing in the name or branding may imply arXiv endorses the
  service (arXiv's terms).

### Phase 1: Multi-user data model and lockdown

**Build**
- `Subscriber` table: `email` (unique, case-insensitive), `confirmed_at`,
  `unsubscribed_at`, `token_version`, `created_at`, `last_manage_link_sent_at`,
  `us_resident_attested_at`, `waitlisted_at` (nullable; §Phase 3 waitlist).
- `Subscription` references `subscriber_id` instead of holding `email`, and
  gains `label` and `waitlisted_at`. The unique constraint becomes
  `(topic_id, subscriber_id)`. Active-only filtering skips waitlisted rows.
- `Topic` gains `normalized_query` (unique). Name uniqueness is dropped.
  `description` stays admin-only.
- **Cadence:**
  - The migration sets every subscription, including the owner's, to
    `twice_weekly`.
  - Public schemas don't accept a cadence; the admin API still can.
  - The column stays, so cadence choice can come back later without a
    migration.
- `max_papers` is bounded to 5–10 (default 5) on public input. The migration
  sets existing NULL (uncapped) subscriptions to 10.
- Public topic input accepts phrases only (§3.3); a publicly created topic's
  `name` is its normalized query. The admin API still accepts field-prefixed
  queries such as the owner's `cat:math.NA`.
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
  through the admin API, `cat:cs.LG` passes through unchanged; on public input,
  any field-prefixed query is rejected.
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
  - an OpenAI embedder (`dimensions=768`, no task prompts) and the backfill;
  - re-run the retrieval evals and recalibrate every EmbeddingGemma-tuned
    threshold, the ranking's novelty anchors included;
  - topic-query embeddings, with a rough threshold for similar-topic
    suggestions.
- **Two passes** (§3.5) ✅ built in 2c (2026-11-13), except the overview-and-picks
  call (2d) and `last_sent_at` at confirmation (Phase 3):
  - Rating runs alone at ingest; re-run the ranking eval.
  - New schedules in `America/New_York`: ingest 21:30 daily, prep 01:00
    Mon/Thu, send 08:00 Mon/Thu. Update `SEND_TIME_ZONE_NAME` and the ingest cron
    (now `INGEST_CRON`).
  - Slot-based send windows; `last_sent_at` set at confirmation.
  - Prep: h-index lookup, ranking, top-K summaries, then one overview-and-picks
    call per topic, all stored per topic and slot.
  - The send workflow stops running its own digest pass; it assembles what prep
    stored.
  - Full text off by default (`RAG_FULLTEXT=false`); the full-text code path
    moves into prep, for the top K only, ready for when arXiv allows it.
- **The new email** (§3.9): the picks-first template (picks, the period in
  brief, also new), citations rendered as arXiv links, and the rewritten
  summary and overview-and-picks prompts.
- **One arXiv process** (§3.5) ✅ 2c, except the `preview` queue (Phase 3): the `arxiv` task queue with one activity slot,
  the `preview` queue (used from Phase 3), and fetch timeouts that don't count
  queue time.
- **Bounded fan-out** ✅ 2c. `RunAllTopicDigestsWorkflow` and prep run their children
  in batches rather than all at once.
- **A second source: OpenAlex** (v2.2, its own plan "2c+", built after 2c and
  before 2d, so the new email and the rehearsal both include it):
  - **Why.** arXiv alone leaves phrase topics short, and fields that aren't on
    arXiv get nothing. For the week to 2026-11-12:

    | Topic | arXiv, as ingested | OpenAlex, all sources |
    |---|---|---|
    | "reinforcement learning from human feedback" | 6 papers | 16 |
    | "physics-informed neural networks" | 17 papers | 136 |

    At two sends a week, the first topic gives 3 papers per email, under the
    default of 5.
  - **What's fetched.**
    - The query is the topic phrase, via `title_and_abstract.search`, which
      matches stemmed words.
    - Kept: types `article`, `preprint` and `conference-paper` only (decision
      21); works with an abstract only, the abstract rebuilt from OpenAlex's
      word-position index.
    - Window: a short `from_publication_date` lookback, about 3 days, with
      already-seen works skipped. That catches backdated works without the paid
      `from_created_date` filter; OpenAlex listed new works within a day or two
      of publication.
    - `search.semantic` (which would match "RLHF" to the spelled-out phrase)
      isn't used: it returns at most 50 results, ranked rather than by date.
  - **Merging.**
    - An arXiv paper's OpenAlex copy carries the DOI `10.48550/arXiv.<id>`; the
      arXiv copy wins.
    - Other duplicates, such as a Zenodo copy of a journal article (23 of 100
      results in a sample), are matched by DOI, then by normalized title. The
      existing near-duplicate fold catches the rest.
  - **Paper identity** (a migration):
    - `papers.arxiv_id` becomes nullable;
    - `doi`, `openalex_id` and `url` are added, each unique where not null;
    - a paper needs at least one id.
    - Everything that builds an arxiv.org link or cites `[arXiv id]` (email
      links, related-work notes, overview citations) moves to the paper's `url`
      and a source-neutral citation key.
  - **Per-source behaviour.**
    - The arXiv lock and queue stay arXiv-only; OpenAlex requests go through
      the main queue at 1 request per second at most.
    - h-index comes from the work's own authorships (no separate lookup);
      venue comes from its source.
    - Full text stays arXiv-only and off (decision 5). Non-arXiv papers are
      summarized from abstracts.
  - **Paywalled papers** are included and ranked like any other paper. The
    email marks a paper whose OpenAlex record isn't open access (decision 22).
  - **Signup's "about 25 a day" check** counts both sources (decision 23).
  - **Cost.** About one OpenAlex list request ($0.001) per topic per day, so
    the $1/day free key covers several hundred topics. Rating cost grows with
    the extra papers; 2e measures it.

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
- Prep summarizes only each topic's top K papers, where K comes from its
  subscribers' settings; with full text off it makes no arXiv requests.
- Picks and citations: an ID the model returns that wasn't in its input is
  dropped; picks always come from the top 5; every subscriber of a topic gets
  the same picks and overview.
- Send windows: a subscriber who confirmed mid-window gets the current slot's
  content; one whose last send failed gets the current slot's content next
  time, not a backlog; a slot that fires twice sends once.
- Send makes no LLM, arXiv or h-index calls (the fakes assert it). A topic
  whose prep failed is still sent, without AI text.
- OpenAlex (2c+), with `respx` fakes:
  - a paper in both sources is stored once, as the arXiv copy;
  - a Zenodo duplicate of a journal article is folded in;
  - datasets, software and works without an abstract never reach rating;
  - a backdated work found by the lookback lands in the next edition exactly
    once;
  - a non-arXiv paper's email link is its `url`, with the paywall mark when it
    isn't open access;
  - no OpenAlex request ever goes through the arXiv queue;
  - an OpenAlex outage leaves the arXiv half of the ingest working.
- Email rendering: section order (picks, overview, also new), citations render
  as working arXiv links, HTML in labels and model output is escaped.
- Evals (manual; they cost money): the ranking eval stays at ρ ≥ 0.77 with the
  rating call alone; the retrieval evals hold on the new embedding model; the
  new prompts pass the §3.9 gates (factual errors, citation support, blind
  pairwise preference, no overclaiming).
- **Who judges the §3.9 gates** (owner's call, v2.1): the owner, by hand, as
  with past reviews. The eval harness produces about 25 overviews with their
  picks and citations, and about 20 blind summary pairs (old prompt vs new),
  in the existing blind-review file format: roughly an hour of review. If a
  gate fails, the prompts are revised and reviewed again. **Launch waits for
  the gates to pass.** There is no fallback to the old email format, because
  the old prompts can't produce picks or citations. Phases 3 and 4 can be built
  on the new template meanwhile; only the launch is blocked.
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
  - **Waitlist** *(builder's call, v2.1)*:
    - Waitlisted people still confirm their email first, so the waitlist holds
      only real, consenting addresses.
    - At the subscriber cap, the whole signup is waitlisted.
    - Under the subscriber cap, only the topics that are blocked are
      waitlisted: a new topic that would pass the topic cap or the 80% spend
      line. The rest of the signup goes through. A topic with no current
      subscribers counts as new, since rejoining it brings its cost back.
    - Waitlisted subscribers and subscriptions carry a `waitlisted_at`
      timestamp (Phase 1 adds the columns). A daily job admits them oldest
      first whenever there's room, after the month resets or when a place
      opens. Each admitted person gets one short "you're in" email, counted
      against the daily email budget. The admin API can also admit by hand.
    - The response and the confirmation page say plainly what was waitlisted
      and why.
- `GET /confirm?token=`: activates the subscriber and sets `last_sent_at` to
  the confirmation time. The first digest arrives at the next Mon/Thu 08:00 ET
  send and covers that slot's whole window (§3.5).
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
  - **Too-broad phrases are refused** (owner's call, v2.1). The preview shows
    roughly how many papers a day the phrase matches. Above about 25 a day
    (the per-fetch limit), signup asks the person to narrow it ("this matches
    about 120 papers a day; try something more specific") instead of accepting
    it, because a sample of the newest 25 can't honestly be "the best papers of
    the period". The threshold is a setting. Signup checks it again
    server-side, so it doesn't rely on the preview having run.
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
  - Signup, all on one page (§3.10): email; up to 5 topics, each with a live
    preview, similar-topic suggestions and a label; papers per topic (5–10);
    the residency checkbox; links to the terms and privacy policy. No cadence
    picker: "Digests arrive Monday and Thursday mornings".
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
  - No `--reload`, no source volume mounts; images are built in CI (also for
    `linux/arm64` if the ARM fallback is used). Every image pinned to a
    version. Several uvicorn workers; exactly one worker process (§3.5).
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
    (sees topic queries and paper abstracts, never email addresses), Cloudflare
    (Turnstile), Sentry, and the backup storage provider;
  - where data is stored: Hetzner's data centre in Germany or Finland;
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
1. Order prompts so the fixed instructions come first and long enough to hit
   OpenAI's automatic prompt caching (cached input is about a tenth of the
   price).
2. Lower the overview's input from 25 abstracts to fewer.
3. Use OpenAI's Batch API (half price) for ingest ratings on days whose
   ratings aren't needed by the next prep. Its 24h completion window is too
   slow for the Sunday and Wednesday ingests.
4. Lower `ARXIV_MAX_RESULTS` for ingest (fewer abstracts rated per topic).
5. Lower the maximum papers per topic (10 → 8); the minimum of 5 stays.
6. Turn off overviews for topics with fewer than N subscribers.
7. Raise the budget.

Turning full text on later (if arXiv allows it) raises the cost per topic;
re-measure before switching it on.

Fixed monthly costs besides OpenAI: the VM (about €6), the domain (about
$10–20/year), off-site backup storage (free tier), Brevo (free, with its logo
in emails), Sentry (free).

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
- [ ] Full text off in production unless arXiv has said yes in writing
- [ ] Email prompts passed the §3.9 eval gates
- [ ] Beta feedback triaged; no open P0/P1 issues

---

## 7. Decisions

| # | Decision | Status |
|---|---|---|
| 1 | Goal | A small real service: public, deliberately capped, lean ops (decided 2026-11-12) |
| 2 | Budget | $20/month hard cap on OpenAI; spend guard closes new topics at 80% and drops AI text at 100% (decided 2026-11-12) |
| 3 | LLM | OpenAI `gpt-5.4-mini` for ratings, summaries and overviews (decided 2026-11-05) |
| 4 | Embeddings | `text-embedding-3-small` at 768 dimensions; every EmbeddingGemma-tuned threshold recalibrated by eval (decided 2026-11-12) |
| 5 | Full text | Off at launch; abstract-only summaries. The owner asks arXiv; if allowed, full text for the top K only (decided 2026-11-12, v2.1) |
| 6 | Topics | Phrase topics with live preview, similar-topic suggestions by embedding, limits. Field-prefixed queries (`cat:` etc.) admin-only (decided 2026-11-12, v2.1) |
| 7 | Cadence | Twice-weekly for everyone at launch; per-subscriber cadence deferred (decided 2026-11-12) |
| 8 | Send time | Mon and Thu 08:00 Eastern, one slot for everyone (decided 2026-11-12) |
| 9 | Audience | US residents only, through the terms plus a signup checkbox; no IP geo-blocking (decided 2026-11-12) |
| 10 | Legal | No postal address or virtual mailbox; CalOPPA-compliant privacy policy; CAN-SPAM practices followed voluntarily (decided 2026-11-12; not legal advice) |
| 11 | Email | Brevo free tier over SMTP, 200-subscriber cap with a waitlist; SES if outgrown |
| 12 | Hosting | One Hetzner CX23 in the EU (about €6/month), self-hosted Postgres with off-site dumps; no standing staging. The Mac was considered and rejected (decided 2026-11-12, v2.1) |
| 13 | Beta | About 20 people for 3 weeks, doubling as the domain warm-up (decided 2026-11-12) |
| 14 | Name and domain | **Open**: owner picks from the shortlist this week (Phase 0) |
| 15 | Reader and success | People with a tie to a field who want to stay connected; success is learning something from every email and finding a paper worth opening (decided 2026-11-12, v2.1) |
| 16 | Papers per topic | 5–10, default 5 (decided 2026-11-12, v2.1) |
| 17 | Email format | Picks first, per topic: up to two "start here" picks with hooks, a short overview citing papers as links, then the rest (decided 2026-11-12, v2.1) |
| 18 | Signup page | One page (decided 2026-11-12, v2.1) |
| 19 | Broad topics | Refused at signup above about 25 matching papers a day, with a prompt to narrow the phrase (decided 2026-11-12, v2.1) |
| 20 | Prompt eval gates | Judged by the owner by hand; launch waits until they pass; no fallback to the old email format (decided 2026-11-12, v2.1) |
| 21 | Second source | OpenAlex alongside arXiv: journal articles, preprints from any server and conference papers, with abstracts; no datasets or software (decided 2026-11-12, v2.2) |
| 22 | Paywalled papers | Included and ranked normally; the email marks them (decided 2026-11-12, v2.2) |
| 23 | Broad topics, two sources | The ~25 papers/day signup limit counts arXiv and OpenAlex together (decided 2026-11-12, v2.2) |
| 24 | OpenAlex timing | Its own plan, 2c+, after 2c and before 2d (decided 2026-11-12, v2.2) |
