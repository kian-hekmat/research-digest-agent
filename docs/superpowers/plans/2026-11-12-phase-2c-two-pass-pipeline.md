# Phase 2c: Two-Pass Pipeline — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Restructure the pipeline into three passes on Eastern time:
1. a cheap daily **ingest** (fetch, embed, rate);
2. a twice-weekly **send prep** that summarizes only each topic's top K papers and writes one overview per topic, stored as an *edition*;
3. a **send** that assembles emails from stored editions, with no LLM, arXiv or h-index calls.

Also: exactly one arXiv request in flight, bounded fan-out, full text off by default, and an alert when every LLM call in a run fails.

**Architecture:**
- **Editions.** A new `topic_editions` table holds one row per topic per send slot. Each row records:
  - the window of ingested papers it covers;
  - their ranking, in `edition_papers`;
  - the overview.
- **Windows chain.** Each edition starts where the topic's previous one ended, so no paper is skipped or sent twice. Everyone on a topic gets the same edition.
- **Workflows.**
  - Ingest is the existing `DigestWorkflow`, slimmed.
  - Prep is new (`PrepEditionsWorkflow` → `PrepTopicEditionWorkflow`).
  - Send (`SendDigestEmailsWorkflow`) reads editions. If a topic has none for the slot, it builds one without AI text rather than hold the email back.
- **arXiv queue.** Requests go through a dedicated `arxiv` task queue, served by a worker that runs one activity at a time.

**Tech Stack:** Python 3.12, SQLAlchemy 2.0, Alembic, Postgres 16, Temporal (time-skipping test server; dev server for schedules), pytest. No new dependencies.

**Spec:** `docs/public-launch-plan.md` (v2.1), §3.5 (two-pass pipeline, send windows, arXiv load), §3.6 ("after the reset" re-rating), §3.4 (`_MAX_CONCURRENT_SUMMARIES` re-tuning), Phase 2's "Two passes", "One arXiv process", "Bounded fan-out" items and tests. Read the "Carried forward" sections of the 2a and 2b plans.

**Not in this plan:**
- The picks-first email, the rewritten prompts, and an overview that reads up to 25 abstracts and returns picks and citations (2d). 2c keeps today's prompts and email layout.
- The preview task queue. Its only user is Phase 3's signup preview, so it arrives with it.
- The cost and load rehearsal (2e).

## Global Constraints

- Commit straight to `main`; repo's git user; history's message style; no Claude attribution. Push after each task.
- Tests: `venv/bin/python -m pytest`. Baseline 468 passed. Alembic head `c3d4e5f6a7b9`.
- **Time zone `America/New_York`.** Ingest runs at 21:30 daily, prep at 01:00 Mon and Thu, send at 08:00 Mon and Thu. A *slot* is a send time, Mon or Thu 08:00 Eastern.
- Schedule ids: ingest keeps `daily-topic-digests`; send keeps `weekly-digest-emails`; prep is new, `send-prep`. All three use overlap policy SKIP and are created or updated idempotently, as today.
- **K** for a topic is the largest `max_papers` among its active subscriptions (an uncapped NULL counts as 10), and at least 5.
- **Prep summaries use the evaluated combined prompt** (`Summarizer.summarize_and_rate`). Only the summary is stored; the ingest rating stands. See the ruling note in Task 3.
- `RAG_FULLTEXT` defaults to `false` (arXiv's terms; §0.1). When on, full text is fetched in prep for the top K only.
- Settings, with exact defaults:
  - `llm_max_concurrency = 8`: LLM activities in flight per workflow. Replaces `_MAX_CONCURRENT_SUMMARIES = 4`.
  - `fanout_batch_size = 10`: child workflows started at once.
  - `worker_activity_threads = 20`.
- **Alerts.** These keys go through the existing `send_alert`; the Phase 2a deferred minor about `ALERT_EMAIL` applies:
  - `ratings-failing:{YYYY-MM-DD}`: an ingest run where at least 3 ratings were attempted and every one raised;
  - `summaries-failing:{YYYY-MM-DD}`: a prep where at least 1 summary was attempted and every one raised.
  - A call the budget skipped (it returns None) is not a failure.

## Review Focus

1. **A topic whose prep never ran** (worker down at 01:00, or the topic was created after prep) still gets an email at the slot, built from its window without AI text. Pinned in Task 4.
2. **Two consecutive editions** share a boundary exactly. A paper ingested at the boundary instant lands in exactly one of them. Pinned in Task 1.
3. **A slot that fires twice**, or prep run twice for the same slot, sends once and keeps one edition per topic and slot (the second prep is a no-op). Pinned in Tasks 3 and 4.
4. **Fifty topics' ingest** never has more than one arXiv request in flight, and requests stay at least `arxiv_page_delay` apart. Pinned in Task 2.
5. **Daylight saving:** slots stay at 08:00 Eastern wall-clock across the November and March changes. Pinned in Task 5.

---

### Task 1: Editions and their windows

**Files:**
- Create: `alembic/versions/d4e5f6a7b8ca_topic_editions.py` (`down_revision = "c3d4e5f6a7b9"`)
- Modify: `app/models.py` (`TopicEdition`, `edition_papers`), `app/crud.py`, `app/cadence.py` (`next_slot`)
- Test: `tests/test_editions.py` (new), `tests/test_cadence.py`

**Interfaces:**
- Produces:
  - `models.TopicEdition` (table `topic_editions`):
    - `id` UUID;
    - `topic_id` FK `topics.id`, cascade;
    - `slot`, `window_start`, `window_end`: timestamptz, not null;
    - `status`: String(16), not null, check `IN ('ready', 'fallback')`;
    - `overview`: Text, nullable;
    - `k`: Integer, not null;
    - `prepared_at`: timestamptz, server default now;
    - unique `uq_topic_editions_topic_slot` (`topic_id`, `slot`).
  - `models.edition_papers` association: `edition_id` (cascade), `paper_id` (cascade), `rank` Integer not null (1 = best); PK (`edition_id`, `paper_id`). Relationship `TopicEdition.papers` ordered by rank.
  - `cadence.next_slot(now: datetime) -> datetime`: the first send slot strictly after `now`, in `SEND_TIME_ZONE`.
  - `crud.edition_window_start(db, topic_id, window_end) -> datetime`. In order of preference:
    1. the topic's latest edition's `window_end`;
    2. else the earliest `last_sent_at` among its active subscriptions (`active_subscriptions_query`);
    3. else `window_end - 7 days`.
  - `crud.window_papers(db, topic_id, start, end) -> list[models.Paper]`: papers linked to the topic's digests whose `completed_at` is in `(start, end]`, de-duplicated, ordered by `arxiv_id`.
  - `crud.get_edition(db, topic_id, slot) -> TopicEdition | None`; `crud.save_edition(db, *, topic_id, slot, window_start, window_end, status, k, overview, ranked_paper_ids: list[str]) -> TopicEdition`. `save_edition` is idempotent: if the topic already has an edition for that slot it returns it unchanged, so a second prep is a no-op.

- [ ] **Step 1: Write the failing tests** in `tests/test_editions.py`, plus cadence tests:

```python
def test_next_slot_is_strictly_after(): ...           # Mon 08:00 ET exactly -> Thu 08:00 ET; Mon 07:59 -> Mon 08:00; Fri -> Mon
def test_window_start_chains_from_the_previous_edition(db_session): ...
def test_window_start_falls_back_to_earliest_active_last_sent(db_session): ...
def test_window_start_defaults_to_a_week(db_session): ...
def test_window_papers_bounds_are_open_closed(db_session): ...
    # digests completed at start (excluded), start+1s, end (included), end+1s (excluded)
def test_consecutive_windows_share_a_boundary_without_overlap(db_session): ...   # Review Focus 2
def test_save_edition_is_idempotent_per_topic_and_slot(db_session): ...         # Review Focus 3 (data side)
def test_edition_papers_keep_rank_order(db_session): ...
def test_deleting_a_topic_deletes_its_editions(db_session): ...
```

- [ ] **Step 2: Run to verify they fail.**
- [ ] **Step 3: Implement** the migration, models, crud and `next_slot`.
- [ ] **Step 4: Run the full suite** — Expected: all pass.
- [ ] **Step 5: Commit** — `git commit -m "added editions: one stored send per topic and slot, with chained windows"`

---

### Task 2: A cheap daily ingest, and one arXiv request at a time

**Files:**
- Modify:
  - `app/temporal/workflows.py`: `DigestWorkflow` becomes ingest only; `RunAllTopicDigestsWorkflow` gets batched fan-out;
  - `app/temporal/activities.py`;
  - `app/worker.py`: a second `Worker` on the arXiv queue;
  - `app/config.py`: `rag_fulltext=False`, plus `llm_max_concurrency`, `fanout_batch_size` and `worker_activity_threads`;
  - `docker-compose.yml`: `RAG_FULLTEXT` default `false`;
  - `tests/test_temporal_workflows.py`: the harness runs both queues;
  - `.env.example`.
- Test: `tests/test_temporal_workflows.py`, `tests/test_ingest_pass.py` (new)

**Interfaces:**
- Produces:
  - `workflows.ARXIV_TASK_QUEUE_SUFFIX = "-arxiv"`, and the helper `arxiv_queue(task_queue: str) -> str`. `fetch_arxiv` (and, in Task 3, `fetch_fulltext`) are executed with `task_queue=arxiv_queue(workflow.info().task_queue)`. Their start-to-close timeouts no longer include queue time, because the activity only starts when the one-slot worker picks it up.
  - `app/worker.py`:
    - the main `Worker` (workflows and every other activity) uses an executor of `worker_activity_threads`;
    - an arXiv `Worker` on `arxiv_queue(...)` runs `[fetch_arxiv, fetch_fulltext]` with `max_concurrent_activities=1`;
    - both run concurrently in the one worker process, via `asyncio.gather` over their `run()` calls.
  - `DigestWorkflow` (ingest) runs, in order: context → fetch (arXiv queue) → ingest → embed → **rate every new paper** with the existing `rate_relevance` activity, up to `llm_max_concurrency` at once → finalize → advance watermark.
    - No summaries, no overview, no full text.
    - Papers that already have a summary are rated the same way.
    - A history-changing restructure: deploy only with no ingest workflows running (Task 5 checks).
  - New activity `send_pipeline_alert(input: PipelineAlertInput) -> bool`, where `PipelineAlertInput(key: str, subject: str, body: str)`. It calls `send_alert` and never raises (it logs instead).
  - The ingest workflow counts ratings that raised. If at least 3 were attempted and all raised, it sends `ratings-failing:{date}`, with the first error's message in the body.
  - `RunAllTopicDigestsWorkflow`: creates and runs children in batches of `fanout_batch_size`, waiting for each batch before starting the next.

- [ ] **Step 1: Write the failing tests:**

```python
async def test_ingest_rates_and_embeds_but_writes_no_summary_or_overview(...): ...
async def test_ingest_alerts_once_when_every_rating_fails(...): ...      # 3 papers, rater raises; one alert, key ratings-failing:<today>
async def test_ingest_budget_skipped_ratings_are_not_failures(...): ...  # rater returns None; no alert
async def test_fanout_runs_children_in_batches(...): ...                # 25 topics, batch 10: max concurrently-running children observed <= 10
async def test_arxiv_requests_one_in_flight_and_spaced(...): ...       # Review Focus 4
    # 50 topics; settings.arxiv_page_delay = 0.05; fake search_arxiv goes through app.services.arxiv.throttle,
    # records (start, end) per call; assert max overlap == 1 and every gap >= 0.05
def test_rag_fulltext_defaults_off(): ...
```

- [ ] **Step 2: Run to verify they fail.**
- [ ] **Step 3: Implement.** Update the test harness's `temporal_client` fixture so it runs the arXiv worker too. Fix existing workflow tests that asserted summaries or overviews from `DigestWorkflow`: they now belong to prep (Task 3), so move or rewrite them there and ledger each one. Don't delete coverage.
- [ ] **Step 4: Run the full suite** — Expected: all pass.
- [ ] **Step 5: Commit** — `git commit -m "made the daily run a cheap ingest, with one arXiv request at a time"`

---

### Task 3: Send prep, one edition per topic

**Files:**
- Modify: `app/temporal/workflows.py` (`PrepEditionsWorkflow`, `PrepTopicEditionWorkflow`), `app/temporal/activities.py`, `app/temporal/types.py`, `app/crud.py` (`recent_overviews` reads editions too), `app/services/email.py` (`build_topic_section(..., order=...)`), `app/worker.py` (register)
- Test: `tests/test_prep_pass.py` (new), `tests/test_email.py`, `tests/test_overview_history.py`

**Interfaces:**
- Produces:
  - `PrepEditionsWorkflow.run(slot_iso: str | None = None) -> int`:
    - the slot defaults to `cadence.next_slot(workflow.now())`;
    - it lists topics with active subscriptions (`list_active_topic_ids`) and runs `PrepTopicEditionWorkflow` children in batches of `fanout_batch_size`, with id `prep-{topic_id}-{slot:%Y%m%dT%H%M}`;
    - it returns how many editions were written.
  - `PrepTopicEditionWorkflow.run(input: PrepTopicInput(topic_id: str, slot: datetime)) -> str` (`"written"`, `"exists"` or `"empty"`):
    1. `plan_edition` activity → `EditionPlan(topic_id, topic_name, topic_query, slot, window_start, window_end, k, paper_ids, unrated_ids, exists: bool)`, with `window_end = workflow.now()` at plan time. If `exists`, return `"exists"`.
    2. Re-rate `unrated_ids` with `rate_relevance` (up to `llm_max_concurrency`). This is the §3.6 "after the reset" re-rating: budget-skipped papers come back.
    3. `enrich_h_index_for_papers(paper_ids)`, reworked from `enrich_author_h_index`; failure degrades, as today.
    4. `rank_edition(plan) -> list[str]`: paper ids best first, using `build_topic_section`'s scoring and the calibration's novelty anchors.
    5. If `RAG_FULLTEXT` is on: `fetch_fulltext` for the top K, on the arXiv queue.
    6. For each of the top K without a summary: `summarize_for_edition(SummarizePaperInput)`, up to `llm_max_concurrency`. It calls `Summarizer.summarize_and_rate` (with excerpts when full text is on) and stores **only** the summary; the ingest rating stands. Ruling for the plan: the combined prompt is the one evaluated (§5.8); 2d replaces it.
    7. `write_overview` over the top K's summaries, history-aware as today. History now comes from `crud.recent_overviews`, which reads editions first and tops up from old digests, so overview history carries over.
    8. `store_edition` → `crud.save_edition(status="ready", ...)` with the full ranking.
    9. If at least 1 summary was attempted and all raised, send `summaries-failing:{date}`.
    - A window with no papers stores nothing and returns `"empty"`.
  - `build_topic_section(..., order: list[str] | None = None)`: when given (arXiv ids, best first), it uses that order instead of scoring, then caps at `max_papers` as today.

- [ ] **Step 1: Write the failing tests:**

```python
async def test_prep_summarizes_only_the_top_k(...): ...               # 12 window papers, subscriptions max 5 and 7 -> K 7; 7 summaries, overview over them
async def test_prep_keeps_the_ingest_rating(...): ...                 # summarizer returns relevance 2; topic_paper.relevance unchanged
async def test_prep_rerates_papers_left_unrated(...): ...             # one paper relevance NULL -> rated in prep
async def test_prep_for_the_same_slot_twice_is_a_noop(...): ...       # Review Focus 3
async def test_prep_with_an_empty_window_stores_nothing(...): ...
async def test_prep_fetches_full_text_only_for_top_k_when_enabled(...): ...
async def test_prep_alerts_when_every_summary_fails(...): ...
async def test_prep_fans_out_in_batches(...): ...
def test_build_topic_section_respects_a_given_order(): ...
def test_overview_history_spans_editions_and_old_digests(db_session): ...
```

- [ ] **Step 2: Run to verify they fail.**
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run the full suite** — Expected: all pass.
- [ ] **Step 5: Commit** — `git commit -m "added send prep: one edition per topic, summaries for the top K only"`

---

### Task 4: Send from editions

**Files:**
- Modify:
  - `app/temporal/workflows.py`: `SendDigestEmailsWorkflow` no longer refreshes digests, waits for pending ones, or looks up h-indices;
  - `app/temporal/activities.py`: `gather_digest_content` reads editions; new `ensure_edition`;
  - `app/temporal/types.py`: `EditionKey`; `TopicWindow` swaps `since` for `edition_id`;
  - remove `count_pending_digests` (activity and crud) if nothing else uses it.
- Test: `tests/test_send_pass.py` (new), `tests/test_temporal_workflows.py`

**Interfaces:**
- Produces:
  - `SendDigestEmailsWorkflow.run(slot_iso: str | None = None) -> int`. The slot is `cadence.latest_slot(workflow.now())`, the most recent slot at or before now.
    1. `list_due_subscriptions`, unchanged.
    2. For each distinct topic: `ensure_edition(EditionKey(topic_id: str, slot: datetime)) -> str` (the edition id). If an edition exists, it returns its id. Otherwise it builds a `"fallback"` edition from the topic's window, ranked with no LLM, arXiv or h-index call, and `overview=None`. A topic whose prep never ran is never held back.
    3. Per recipient: `gather_digest_content(GatherContentInput)`, whose `TopicWindow` is now `(subscription_id, topic_id, topic_name, edition_id, max_papers)` with no `since`, builds each section with `build_topic_section(topic_name, [DigestForEmail(edition)], max_papers, order=edition ranking)`, then sends, then advances `last_sent_at` exactly as today. A topic whose edition has no papers is left out of the email, and its subscription isn't marked sent.
  - A subscriber who confirms or joins mid-window gets the full current edition. One whose last send failed gets the current edition next time, not a backlog.

- [ ] **Step 1: Write the failing tests:**

```python
async def test_send_uses_the_stored_edition_and_makes_no_llm_arxiv_or_hindex_calls(...): ...
    # fakes for Summarizer, search_arxiv, fetch_max_author_h_index all raise if called
async def test_topic_without_an_edition_gets_a_fallback_without_ai_text(...): ...   # Review Focus 1
async def test_new_subscriber_mid_window_gets_the_whole_edition(...): ...
async def test_failed_send_gets_the_current_edition_next_time_not_a_backlog(...): ...
async def test_slot_firing_twice_sends_once(...): ...                               # Review Focus 3
async def test_each_subscriber_gets_their_own_cap_from_one_edition(...): ...         # max 5 and max 10 on one topic
```

- [ ] **Step 2: Run to verify they fail.**
- [ ] **Step 3: Implement.** Rewrite or move existing send tests that relied on the refresh, the pending wait or send-time h-index; ledger each one.
- [ ] **Step 4: Run the full suite** — Expected: all pass.
- [ ] **Step 5: Commit** — `git commit -m "sent emails from stored editions"`

---

### Task 5: Eastern-time schedules, docs, and the switch-over

**Files:**
- Modify: `app/cadence.py` (`SEND_TIME_ZONE_NAME = "America/New_York"`), `app/temporal/schedule.py` (three schedules), `app/worker.py` (ensure all three), `README.md` ("How it works", the schedule times, "Testing"), `docs/public-launch-plan.md` (mark Phase 2's two-pass items done)
- Test: `tests/test_cadence.py`, `tests/test_temporal_workflows.py` (schedule tests, dev server)

**Interfaces:**
- Produces:
  - `schedule.INGEST_CRON = "30 21 * * *"`, `schedule.PREP_CRON = "0 1 * * 1,4"`, and `schedule.EMAIL_CRON = cadence.email_cron()` (`"0 8 * * 1,4"`). All use `time_zone_name="America/New_York"`.
  - `ensure_prep_schedule(client)` (id `send-prep` → `PrepEditionsWorkflow.run`). `ensure_daily_schedule` and `ensure_email_schedule` keep their ids and update in place, because the cron note includes the time zone.

- [ ] **Step 1: Write the failing tests:**

```python
def test_slots_stay_at_8am_eastern_across_dst(): ...     # Review Focus 5: latest_slot on 2026-11-02 and 2027-03-15 are 08:00 local, 13:00Z and 12:00Z
def test_crons_and_time_zone(): ...
async def test_ensure_prep_schedule_is_idempotent(temporal_env_only): ...
async def test_existing_schedules_move_to_eastern_in_place(temporal_env_only): ...   # created with the old Pacific note -> "updated", same id
```

- [ ] **Step 2: Run to verify they fail; then implement; run the full suite; commit and push.** Commit message: `git commit -m "moved the schedules to Eastern time and added send prep"`.
- [ ] **Step 3: Switch over live**, away from scheduled runs (today's ingest is at 10:00 PT until the switch; after it, ingest is at 18:30 PT).
  1. `temporal workflow list --query "ExecutionStatus='Running'"` inside the temporal container. Expected: nothing running; if something is, wait for it.
  2. `pg_dump` live.
  3. `docker-compose stop worker`.
  4. `alembic upgrade head`.
  5. `docker-compose up -d --build api worker`.

  Expected:
  - the worker logs three schedules: ingest "updated", prep "created", email "updated";
  - `temporal schedule describe` shows the Eastern crons.
- [ ] **Step 4: Verify through the first cycle.**
  - After the next 21:30 ET ingest: new papers carry ratings and no summaries, and `llm_usage` shows only `rating` and `embedding` rows.
  - After Monday's 01:00 ET prep: each topic has a `ready` edition whose window starts at the topic's last send, and `llm_usage` shows `summary` and `overview` rows.
  - After 08:00 ET: one email arrives with each topic's edition.
  - Record all of this, with the first per-topic costs from `/admin/usage`, in this plan's outcome.
