# Phase 2c+: OpenAlex as a Second Paper Source — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** The daily ingest also fetches each phrase topic's new papers from OpenAlex: journal articles, other preprint servers and conference papers. They are merged with arXiv's (the arXiv copy wins), then ranked, summarized and emailed like any paper. Non-arXiv papers link to their own page; paywalled ones are marked.

**Architecture:**
- **Source-neutral identity.** A paper's identity becomes a source-neutral `papers.key`:
  - the arXiv id for arXiv papers, so existing rows keep their values;
  - `openalex:W…` for works only OpenAlex has.

  `arxiv_id` becomes nullable and arXiv-only. New columns: `doi`, `openalex_id`, `url` and `open_access`.
- **Where each id is used.**
  - Code that uses the id as an identity switches to `key`: dedupe maps, ordering, editions, email, retrieval.
  - Code that needs arXiv specifically keeps `arxiv_id` and skips papers without one: full text, related-work citations, arXiv-DOI h-index.
- **Ingest.** It gains a `fetch_openalex` activity on the main queue. A failure only costs that run's OpenAlex half. `merge_results` dedupes by key, then DOI, then normalized title.

**Tech Stack:** Python 3.12, SQLAlchemy 2.0, Alembic, Postgres 16, Temporal, httpx, respx, pytest. No new dependencies.

**Spec:** `docs/public-launch-plan.md` v2.2:
- Phase 2, "A second source: OpenAlex";
- decisions 21–24;
- Phase 0, "OpenAlex API key".

Read the 2c plan's Outcome section first.

**Not in this plan:**
- **Signup's "about 25 papers a day" preview count across both sources** (decision 23). Phase 3 builds the preview; Task 6 adds the requirement to Phase 3's text.
- **Full text for non-arXiv papers.** Full text stays arXiv-only and off (decision 5).
- **OpenAlex `search.semantic`.**
- **Tracking OpenAlex spend in `llm_usage`.** It's about $0.001 a request against a free $1/day key; 2e measures volume.

## Global Constraints

- **Git and tests.**
  - Commit straight to `main` with the repo's git user, in the history's message style, with no Claude attribution. Push after each task.
  - Run tests with `venv/bin/python -m pytest`. Baseline 521 passed. Alembic head is `d4e5f6a7b8ca`.
- **Build window** (lesson from 2c, ruling R3): the live worker runs this working tree, and Temporal's sandbox re-reads workflow files.
  - Before Task 1: confirm nothing is running, pause `daily-topic-digests`, `send-prep` and `weekly-digest-emails`, and `docker-compose stop worker`.
  - Unpause all three at the Task 6 switch-over.
  - Start only after 2c's first cycle is verified (Mon 2026-11-16 08:00 ET). Aim to switch over before Thu 01:00 ET prep.
- **Which works are kept** (decision 21): types `article`, `preprint` and `conference-paper` only, with an abstract. No datasets, software or anything else.
- **The query.** OpenAlex gets only plain phrase topics. A query matching `app.services.arxiv.FIELD_PREFIX` (`cat:`, `ti:`, …) is never sent. The filter is `title_and_abstract.search:"<phrase>"`.
- **Requests.**
  - At most 1 OpenAlex request per second, process-wide. That covers search and the h-index lookup.
  - `api_key=<OPENALEX_API_KEY>` goes on every request when it's set. No `mailto`, and never the owner's email.
- **Settings, with exact defaults:**
  - `openalex_enabled = True`;
  - `openalex_api_key = ""`;
  - `openalex_lookback_days = 3`;
  - `openalex_max_results = 200`;
  - `openalex_min_interval = 1.0` (seconds);
  - `openalex_api_url = "https://api.openalex.org/works"`.
- **Merge order** (decision 21, "arXiv copy wins"):
  1. Same `key` (an OpenAlex work whose DOI is `10.48550/arXiv.<id>` gets key `<id>`): the arXiv result wins.
  2. Same DOI.
  3. Same normalized title (lowercase, alphanumerics and single spaces only).

  Among non-arXiv duplicates, prefer `article`, then `conference-paper`, then `preprint`; the first seen wins a tie.
- **The paywall mark** (decision 22): a paper whose `open_access` is False gets this exact copy after its link:
  - text: ` (publisher's page; may need a subscription)`;
  - HTML: `<small>publisher's page; may need a subscription</small>`.

  `open_access` None (arXiv, or unknown) shows nothing.
- **Venue.** A non-arXiv work's `journal_ref` is its primary location's source `display_name` when that source's type is `journal` or `conference`; otherwise None. `venue_score` is unchanged.

## Review Focus

1. **A paper both sources return, possibly on different days** (OpenAlex lags arXiv), is stored once, keyed by its arXiv id, and emailed once. Pinned in Tasks 2 and 3.
2. **An OpenAlex outage** (429, 5xx or timeout, after retries) still lets the arXiv half of the ingest complete, rate, and send no alert. Pinned in Task 3.
3. **An admin field-prefixed topic** (`cat:math.NA`) never reaches OpenAlex. Pinned in Task 1.
4. **A dataset, software record or work without an abstract** never reaches rating. Pinned in Task 1.
5. **A paywalled journal paper's entry** links to its landing page with the mark, and an arXiv paper's entry is byte-for-byte unchanged. Pinned in Task 4.

---

### Task 1: The OpenAlex client

**Files:**
- Create: `app/services/openalex.py`
- Modify: `app/config.py` (the six settings)
- Test: `tests/test_openalex.py` (new, `respx`)

**Interfaces:**
- Produces:
  - `openalex.OpenAlexResult`, a dataclass with these fields:
    - `key: str`;
    - `arxiv_id: str | None`;
    - `doi: str | None`, lowercased, without `https://doi.org/`;
    - `openalex_id: str` (`W…`);
    - `title: str`, `abstract: str`, `published_at: datetime | None`;
    - `url: str`;
    - `open_access: bool | None`;
    - `journal_ref: str | None`;
    - `work_type: str`.
  - `openalex.is_phrase_query(query: str) -> bool`: True unless `FIELD_PREFIX` matches.
  - `openalex.search_openalex(query: str, *, since: date, settings: Settings | None = None, client: httpx.Client | None = None) -> list[OpenAlexResult]`.
    - Returns `[]` without a request when `not settings.openalex_enabled` or `not is_phrase_query(query)`.
    - Filter: `title_and_abstract.search:"<phrase>",from_publication_date:<since>,type:article|preprint|conference-paper,has_abstract:true`.
    - Sorted `publication_date:desc`, cursor-paged at `per-page=100` up to `openalex_max_results`.
    - Selected fields: `id,doi,title,abstract_inverted_index,publication_date,type,primary_location,best_oa_location,open_access`.
    - Each request goes through `openalex.throttle(settings.openalex_min_interval)`, a process-wide gate like `arxiv.throttle`.
    - It drops works whose rebuilt abstract is empty or whose type isn't one of the three.
    - `key` is `doi.removeprefix("10.48550/arxiv.")` for arXiv DOIs (case-insensitive, `arxiv_id` set to the same), else `openalex:<W id>`.
    - `url`: `best_oa_location.landing_page_url`, else `primary_location.landing_page_url`, else `https://doi.org/<doi>`, else the OpenAlex id URL. For arXiv works it's `https://arxiv.org/abs/<id>`.
    - `open_access` comes from `open_access.is_oa`, and is None for arXiv works.
    - Left to raise on HTTP errors; the activity's retry policy governs.
  - `openalex.rebuild_abstract(inverted: dict[str, list[int]] | None) -> str`.
  - `openalex.normalize_title(title: str) -> str`.

- [ ] **Step 1: Write the failing tests:**

```python
def test_phrase_topics_only(): ...                          # Review Focus 3: "cat:math.NA" -> [] and respx route not called
def test_request_filters_and_api_key(respx_mock): ...       # exact filter string above; api_key param present iff settings has one; no mailto
def test_keeps_articles_preprints_and_conference_papers_with_abstracts(respx_mock): ...  # Review Focus 4: dataset, software, empty abstract dropped
def test_an_arxiv_work_is_keyed_by_its_arxiv_id(respx_mock): ...   # doi 10.48550/arXiv.2611.01234 -> key/arxiv_id "2611.01234", url arxiv abs, open_access None
def test_other_works_are_keyed_by_openalex_id_with_landing_url(respx_mock): ...  # key "openalex:W1", url = best_oa landing page, open_access from is_oa, journal_ref = journal source name
def test_pages_with_a_cursor_up_to_max_results(respx_mock): ...    # 2 pages of 100, max 150 -> 150 results, 2 requests
def test_requests_are_spaced_by_min_interval(): ...         # two calls through throttle(0.05) are >= 0.05 s apart
def test_rebuild_abstract_and_normalize_title(): ...        # {"b":[1],"a":[0]} -> "a b"; "Deep  Learning: A Survey!" -> "deep learning a survey"
def test_disabled_makes_no_request(respx_mock): ...
```

- [ ] **Step 2: Run to verify they fail** — `venv/bin/python -m pytest tests/test_openalex.py -q`. Expected: ImportError.
- [ ] **Step 3: Implement** the module and settings.
- [ ] **Step 4: Run the full suite** — Expected: all pass.
- [ ] **Step 5: Commit** — `git commit -m "added an OpenAlex client for phrase topics"`

---

### Task 2: Source-neutral paper identity, and merging two sources

**Files:**
- Create: `alembic/versions/f7a8b9c0d1eb_paper_identity.py` (`down_revision = "d4e5f6a7b8ca"`), `app/services/sources.py`
- Modify: `app/models.py` (`Paper`), `app/crud.py`, `app/temporal/types.py` (`PaperResult`), `app/schemas.py` (`PaperOut`)
- Test: `tests/test_paper_identity.py` (new), `tests/test_sources.py` (new)

**Interfaces:**
- Produces:
  - **The migration.**
    - Upgrade:
      1. add `key` (String) and fill it with `arxiv_id`;
      2. make `key` not null and unique (`uq_papers_key`);
      3. make `arxiv_id` nullable, keeping it unique;
      4. add `doi` (String, partial unique index `uq_papers_doi` where not null);
      5. add `openalex_id` (String, partial unique `uq_papers_openalex_id`);
      6. add `url` (String) and `open_access` (Boolean), both nullable.
    - Downgrade: delete papers whose `arxiv_id` is null (their links cascade), then reverse.
  - **`models.Paper`.**
    - The new columns above.
    - A `before_insert` default: when `key` is None, set it to `arxiv_id`. Existing code and tests that build `Paper(arxiv_id=...)` keep working.
    - A property `link -> str`: `url`, or `https://arxiv.org/abs/<arxiv_id>`.
  - **`types.PaperResult`.**
    - Becomes `(key: str, title: str, abstract: str, published_at: datetime | None, arxiv_id: str | None = None, comment: str | None = None, journal_ref: str | None = None, doi: str | None = None, openalex_id: str | None = None, url: str | None = None, open_access: bool | None = None, work_type: str = "preprint")`.
    - `fetch_arxiv` sets `key=arxiv_id=r.arxiv_id`. `key` is a new required first field, so update every constructor.
  - **`sources.merge_results(arxiv: list[PaperResult], openalex: list[PaperResult]) -> list[PaperResult]`**: arXiv results first, then the OpenAlex ones not already present, by the Global Constraints' merge order.
  - **crud.**
    - `crud.get_or_create_paper(db, result: PaperResult, *, topic=None) -> models.Paper` replaces the keyword form.
      - It looks up by `key`, then by `doi`, and creates otherwise.
      - It refreshes `comment`/`journal_ref` as today.
      - It fills `doi`, `openalex_id`, `url` and `open_access` where they're missing.
      - Update `ingest_papers`, `app/import_relevance_labels.py` and test callers.
    - `crud.keys_linked_to_topic(db, topic_id, keys) -> set[str]` replaces `arxiv_ids_linked_to_topic`.
    - `crud.window_papers` orders by `Paper.key`.
  - **`PaperOut`** gains `key: str`, `url: str | None` and `open_access: bool | None`; `arxiv_id` becomes `Optional[str]`.

- [ ] **Step 1: Write the failing tests:**

```python
# tests/test_paper_identity.py
def test_existing_papers_keep_their_arxiv_id_as_key(db_session): ...      # Paper(arxiv_id="2611.00001") -> key "2611.00001"; link = arxiv abs
def test_a_non_arxiv_paper_has_a_key_and_its_own_link(db_session): ...    # key "openalex:W1", arxiv_id None, link = url
def test_get_or_create_finds_a_paper_by_doi_under_another_key(db_session): ...
def test_get_or_create_fills_missing_identifiers(db_session): ...         # arXiv paper later seen via OpenAlex gains doi/openalex_id, keeps url None -> arxiv link
def test_migration_round_trip_keeps_arxiv_papers(db_session): ...        # upgrade, insert one of each, downgrade to d4e5f6a7b8ca, upgrade: arXiv row survives with key restored

# tests/test_sources.py
def test_arxiv_copy_wins_over_its_openalex_copy(): ...      # Review Focus 1 (merge side)
def test_doi_duplicates_merge(): ...
def test_title_duplicates_merge_preferring_the_journal_article(): ...   # Zenodo "preprint" + "article" same normalized title -> article kept
def test_distinct_papers_are_all_kept_in_order(): ...
```

- [ ] **Step 2: Run to verify they fail.**
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run the full suite** — Expected: all pass (existing tests that build `Paper(arxiv_id=...)` unchanged).
- [ ] **Step 5: Commit** — `git commit -m "gave papers a source-neutral key and merged results from two sources"`

---

### Task 3: Ingest from both sources

**Files:**
- Modify:
  - `app/temporal/activities.py`: `fetch_openalex`, `ingest_papers`, `fetch_fulltext` (arXiv papers only), `_related_papers` (only neighbours with an `arxiv_id`);
  - `app/temporal/workflows.py` (`DigestWorkflow`);
  - `app/worker.py` (register).
- Test: `tests/test_ingest_pass.py`

**Interfaces:**
- Consumes: Task 1's `search_openalex`, and Task 2's `merge_results`, `PaperResult`, `get_or_create_paper` and `keys_linked_to_topic`.
- Produces:
  - **`fetch_openalex(ctx: DigestContext) -> list[PaperResult]`**, on the main queue.
    - `since = (ctx.since or now - 7 days) - openalex_lookback_days`, as a date.
    - Maps each `OpenAlexResult` to a `PaperResult`.
  - **`DigestWorkflow`.**
    - Runs `fetch_arxiv` (unchanged) and then `fetch_openalex` (start-to-close 120 s, `_SCHOLAR_RETRY`).
    - An `ActivityError` from `fetch_openalex` is logged and treated as `[]`.
    - Then `ingest_papers(IngestPapersInput(..., results=merge_results(arxiv, openalex)))`.
    - An arXiv fetch failure still fails the digest as today, even when OpenAlex succeeded. Ruling for the plan: arXiv is the primary source, and the alert path for it exists.
  - **`ingest_papers`** dedupes against the topic by `keys_linked_to_topic`. A paper either source already gave the topic is skipped.

- [ ] **Step 1: Write the failing tests:**

```python
async def test_ingest_merges_both_sources(...): ...                         # arXiv 2 + OpenAlex 3 (1 arXiv dup) -> 4 papers rated, one row per paper
async def test_a_paper_seen_later_in_openalex_is_not_ingested_again(...): ... # Review Focus 1: day 1 arXiv "2611.00001"; day 2 OpenAlex same arXiv DOI -> not linked to the second digest
async def test_openalex_outage_leaves_the_arxiv_half_working(...): ...      # Review Focus 2: search_openalex raises -> digest completed, arXiv papers rated, no alert
async def test_category_topics_skip_openalex(...): ...                      # cat:math.NA -> search_openalex returns [] without a request (respx not called)
async def test_full_text_skips_non_arxiv_papers(...): ...                   # prep with RAG_FULLTEXT on: index_fulltext never called for an openalex: key
def test_openalex_requests_never_use_the_arxiv_queue(): ...                 # fetch_openalex in app.worker.ACTIVITIES, not in ARXIV_ACTIVITIES
```

- [ ] **Step 2: Run to verify they fail.**
- [ ] **Step 3: Implement.** The `temporal_client` fixture patches `temporal_activities.search_openalex` to return `[]` by default, so no test reaches OpenAlex.
- [ ] **Step 4: Run the full suite** — Expected: all pass.
- [ ] **Step 5: Commit** — `git commit -m "ingested papers from OpenAlex alongside arXiv"`

---

### Task 4: Email, ranking and retrieval on keys and links

**Files:**
- Modify: `app/services/email.py`, `app/services/retrieval.py`, `app/temporal/activities.py` (`_paper_for_email`, `_rank_window`, `gather_digest_content`), `app/crud.py` (`similar_earlier_papers` orders by key)
- Test: `tests/test_email.py`, `tests/test_related_work.py`, `tests/test_ranking.py`, `tests/test_prep_pass.py` (rename the `arxiv_id=` keyword of `PaperForEmail`/`_ep` to `key=`)

**Interfaces:**
- Produces:
  - **`PaperForEmail`**: `arxiv_id` becomes `key: str`. It gains `url: str` (default `""`, which renders as `https://arxiv.org/abs/<key>`) and `open_access: bool | None = None`. Fields:
    - `near_duplicate_of`: holds a key;
    - `similar`: a list of `(url, title)`;
    - `see_also`: unchanged.
  - **`build_topic_section(..., order=...)`** takes keys. Everything that deduped or sorted by `arxiv_id` uses `key`.
  - **Links.** Every link the email renders comes from `url`: the paper's, "Similar earlier paper" and "Also (near-duplicate)". `_linkify_arxiv_ids` stays for related notes only. The paywall mark follows the Global Constraints copy.
  - `_paper_for_email` sets `key=p.key`, `url=p.link` and `open_access=p.open_access`; `_rank_window` maps by `key`.
  - **`retrieval.Neighbour`**: `arxiv_id` becomes `key: str`, and it gains `arxiv_id: str | None` and `url: str`. The related-work offer (`_related_papers`) passes only neighbours with an `arxiv_id`, citing it.

- [ ] **Step 1: Write the failing tests:**

```python
def test_a_paywalled_paper_links_to_its_page_with_the_mark(): ...      # Review Focus 5: text and HTML carry the exact copy; url used
def test_an_open_non_arxiv_paper_links_without_the_mark(): ...
def test_an_arxiv_paper_renders_exactly_as_before(): ...               # Review Focus 5: snapshot of one arXiv entry, text and HTML, equals today's output
def test_similar_and_near_duplicate_links_use_each_papers_url(): ...
def test_related_work_offers_only_arxiv_papers(db_session, ...): ...
```

- [ ] **Step 2: Run to verify they fail.** Then rename `arxiv_id=` to `key=` in the listed tests' `PaperForEmail`/`_ep` calls, and confirm only the new tests fail.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run the full suite** — Expected: all pass.
- [ ] **Step 5: Commit** — `git commit -m "keyed emails and retrieval by source-neutral ids and linked each paper to its own page"`

---

### Task 5: h-index for non-arXiv papers

**Files:**
- Modify: `app/services/scholar.py`, `app/temporal/activities.py` (`enrich_h_index_for_papers`)
- Test: `tests/test_scholar.py` (or the existing scholar tests' file)

**Interfaces:**
- Produces:
  - `scholar.PaperRef(key: str, arxiv_id: str | None, doi: str | None, openalex_id: str | None)`.
  - `scholar.fetch_max_author_h_index(refs: list[PaperRef], *, client=None) -> dict[str, int]`, keyed by `key`. Still exactly one source per call:
    - **Semantic Scholar:** ids are `arXiv:<id>`, else `DOI:<doi>`; refs with neither are skipped.
    - **OpenAlex:** works are filtered by `openalex:<W…>` when known, else by `doi:`; `api_key` when set; through `openalex.throttle`.
  - `enrich_h_index_for_papers` builds the refs from `Paper` rows.

- [ ] **Step 1: Write the failing tests:**

```python
def test_openalex_source_looks_up_non_arxiv_papers_by_openalex_id(respx_mock): ...
def test_semantic_scholar_source_uses_doi_for_non_arxiv_papers(respx_mock): ...
def test_results_are_keyed_by_paper_key(respx_mock): ...
def test_openalex_requests_carry_the_api_key_when_set(respx_mock): ...
```

- [ ] **Step 2: Run to verify they fail.** Update existing scholar tests from arXiv-id lists to `PaperRef`s.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run the full suite** — Expected: all pass.
- [ ] **Step 5: Commit** — `git commit -m "looked up author h-index for papers from either source"`

---

### Task 6: Config, docs and the switch-over

**Files:**
- Modify:
  - `docker-compose.yml`: `OPENALEX_API_KEY: ${OPENALEX_API_KEY:-}` and `OPENALEX_ENABLED: ${OPENALEX_ENABLED:-true}` for api and worker;
  - `.env.example`;
  - `app/worker.py` (`check_config`);
  - `README.md`: "How it works" step 1, the settings table, attribution;
  - `docs/public-launch-plan.md`:
    - mark 2c+ built;
    - Phase 3's preview text: the count is over both sources (decision 23);
    - the Phase 6 attribution: OpenAlex data is CC0, credit it.
- Test: `tests/test_docker_compose.py`, `tests/test_worker.py` (or wherever `check_config` is tested)

- [ ] **Step 1: Write the failing tests.**
  - `test_compose_passes_openalex_settings_to_the_worker`.
  - `test_check_config_warns_without_an_openalex_key`: a logged warning naming `OPENALEX_API_KEY` and the shared $0.10/day keyless budget. It does not exit.
- [ ] **Step 2: Run to verify they fail; implement; run the full suite; commit and push.** Commit message: `git commit -m "configured OpenAlex and documented the second source"`.
- [ ] **Step 3: Switch over live:**
  1. Confirm with `temporal workflow list` that nothing is running.
  2. `pg_dump` to `~/research-digest-backups/`.
  3. Run `docker-compose run --rm worker alembic upgrade head` (expect `f7a8b9c0d1eb`).
  4. Run `docker-compose up -d --build api worker`.
  5. Unpause `daily-topic-digests`, `send-prep` and `weekly-digest-emails`.

  Expected: the worker logs the three schedules as "unchanged", and the keyless warning only if the owner hasn't added `OPENALEX_API_KEY` yet.
- [ ] **Step 4: Verify through the first ingest and send.**
  - New `openalex:` papers appear for the phrase topics (PINNs, RLHF) and none for `cat:math.NA`.
  - No paper is stored twice.
  - The next email links non-arXiv papers to their pages, with the mark where paywalled.
  - Record per-topic paper counts by source, and the change in rating spend, in this plan's outcome.
