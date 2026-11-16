# Phase 2d Round 1: Prompts and Evals Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Write the new paper-blurb and overview-and-picks prompts with structured, validated output, plus an eval script that produces the owner's review files.

**Architecture:**
- **Structured output.** `Summarizer._complete` gains an optional `response_format`. Two new `Summarizer` methods return small dataclasses parsed and checked by pure functions in `app/services/summarize.py`.
- **Eval script.** A new `evals/email_prompts.py` rebuilds Monday-to-Monday windows from stored digests, re-ranks them with production's ranking, runs both prompts, and writes the review files.
- **Prep is untouched.** Nothing in prep, the email or the database schema changes in this round.

**Tech Stack:** Python, httpx and respx (tests), SQLAlchemy, OpenAI `/chat/completions` (`gpt-5.4-mini`).

**Spec:** `docs/superpowers/specs/2026-11-16-phase-2d-picks-first-email-design.md` (Round 1). Background: `docs/public-launch-plan.md` §3.9 and `docs/rag-plan.md` §5.6.

## Global Constraints

- **Abstracts only.** Model `gpt-5.4-mini`. Every call goes through `Summarizer._complete` with a ledger, and the `tests/test_spend_coverage.py` AST check covers `evals/` too.
- **Purposes:** blurbs bill as `"summary"`, the overview-and-picks call as `"overview"`. No new purposes.
- **Non-OpenAI backends and blocked calls.** When `summary_backend` isn't `openai`, or the ledger blocks the call, both new methods return `None`.
- **Blurb:** `hook`, one line, at most about 20 words, saying why it's worth reading. `what`, one or two concrete sentences on what it did and found. No topic is mentioned, there's no rating, and there are no "The paper addresses / This work proposes" openers.
- **Overview:** 80–120 words with inline `[arXiv ID]` citations wherever a specific paper's finding is stated. `picks` holds 0–2 `{arxiv_id, hook}` items, from the top 5 only. The `avoid_repeats` history rules apply (`_OVERVIEW_AVOID_REPEATS_SYSTEM`'s wording on previous digests).
- **Code validation:** an arXiv ID not offered is removed from the text; a pick not in the top 5, a duplicate pick, or a pick with an empty hook is dropped; invalid JSON gives `None`.
- **Eval:**
  - Use every rebuilt weekly window with at least 5 papers, about 17. No padding.
  - Make about 20 blind blurb pairs. The "old" side is `summarize_and_rate(...).summary` re-run now; the "new" side is `hook + "\n" + what`.
  - The script reads the database and writes nothing to it except `llm_usage` rows. Spend is estimated well under $1.
- **Existing behavior stays as it is:** `summarize_paper`, `summarize_and_rate`, `write_overview`, prep, the email and the related-work notes.
- **Git:** commit straight to `main`, lowercase past-tense messages, as the owner, with no AI attribution. No workflow or model changes, so the live stack needn't be paused; the worker only picks up `summarize.py` on its next restart, and the change is purely additive.

## Review Focus

1. **A citation with a version suffix** (`[2611.12345v2]`) should count as the offered `2611.12345` and stay. Task 3 has the test.
2. **A bare ID in the prose that wasn't offered** (`2611.99999`, with no brackets) would be turned into a link in Round 2, so it must be removed like a bracketed one. A bare offered ID is kept. Task 3 has the test.
3. **JSON wrapped in a code fence** (` ```json … ``` `) should still parse. Tasks 2 and 3 have the test.
4. **Removing every citation** must leave clean prose: no empty `[]` and no doubled spaces or space before punctuation. Task 3 has the test.
5. **A window paper with an empty abstract** is still offered to the overview call, with its title, rather than skipped. Task 3 has the test.

---

### Task 1: Structured output in `Summarizer._complete`, plus the JSON probe

**Files:**
- Modify: `app/services/summarize.py` (`_complete`, `_complete_openai`, `_complete_ollama` callers)
- Test: `tests/test_summarize_usage.py`

**Interfaces:**
- Produces: `Summarizer._complete(*, system: str, user: str, max_tokens: int, purpose: str, response_format: dict | None = None) -> str | None`.
  - With a `response_format`, the OpenAI request body carries `"response_format": response_format`.
  - With a non-OpenAI backend, the call returns `None` without making any HTTP call.
  - Without one, the request is byte-for-byte what it is today.

- [ ] **Step 1: Probe (manual, costs fractions of a cent).** Run this from the host:
  `venv/bin/python -c "..."`, building `Summarizer(ledger=DbLedger())` and calling the private `_complete_openai` path with:
  - `response_format={"type": "json_schema", "json_schema": {"name": "probe", "strict": True, "schema": {"type": "object", "properties": {"ok": {"type": "string"}}, "required": ["ok"], "additionalProperties": False}}}`
  - a user message of "say ok".

  Expected: a 200 with JSON content.
  - If you get a 400 that names `response_format` or `json_schema`, rerun with `{"type": "json_object"}`.
  - Record which form works in the ledger as a ruling. Tasks 2 and 3 use it (`_JSON_FORMAT(name, schema)` returns the working form).
  - Never print the key.
- [ ] **Step 2: Write the failing tests** in `tests/test_summarize_usage.py`:
  - `test_response_format_is_sent_when_given`: with respx, `_summarizer(FakeLedger())._complete(system="s", user="u", max_tokens=50, purpose="summary", response_format={"type": "json_object"})`. The captured request JSON has `response_format == {"type": "json_object"}`, and the return value is the reply text.
  - `test_no_response_format_key_by_default`: the same call without the argument; the captured JSON has no `"response_format"` key.
  - `test_response_format_on_ollama_returns_none_without_http`: `_summarizer(FakeLedger(), summary_backend="ollama")._complete(..., response_format={"type": "json_object"})` is `None`, and respx saw no calls.
- [ ] **Step 3: Run them and see them fail:** `venv/bin/python -m pytest tests/test_summarize_usage.py -q -p no:cacheprovider`. Expected: the 3 new tests fail (unexpected keyword).
- [ ] **Step 4: Implement** the `_complete` signature above. Thread `response_format` to `_complete_openai`'s body only when it isn't None, and return None before the Ollama branch when it's given. Add `_JSON_FORMAT(name: str, schema: dict) -> dict`, built per the probe's ruling.
- [ ] **Step 5: Run them and see them pass,** with the same command. Expected: all pass.
- [ ] **Step 6: Commit:** "added structured json output to the summarizer's openai calls".

### Task 2: The paper blurb

**Files:**
- Modify: `app/services/summarize.py`
- Test: `tests/test_paper_blurb.py` (new; respx and the FakeLedger pattern from `test_summarize_usage.py`)

**Interfaces:**
- Consumes: `_complete(..., response_format=)` and `_JSON_FORMAT` from Task 1.
- Produces:
  - `@dataclass(frozen=True) class PaperBlurb: hook: str; what: str`
  - `parse_paper_blurb(text: str) -> PaperBlurb | None`
  - `Summarizer.write_blurb(title: str, abstract: str) -> PaperBlurb | None`, with purpose `"summary"`, max_tokens 300, system prompt `_BLURB_SYSTEM`, schema `_BLURB_SCHEMA` (`hook` and `what`, both required strings).

- [ ] **Step 1: Write the failing tests:**
  - `test_parse_blurb_reads_both_fields`: `parse_paper_blurb('{"hook": " Halves  the cost ", "what": "They did X. It got 12%."}') == PaperBlurb("Halves the cost", "They did X. It got 12%.")`. Whitespace runs collapse to one space, and the ends are trimmed.
  - `test_parse_blurb_accepts_a_code_fence`: the same JSON wrapped in ```` ```json\n…\n``` ```` parses the same way.
  - `test_parse_blurb_rejects_missing_or_empty_fields`: `'{"hook": "x"}'`, `'{"hook": "", "what": "y"}'`, `'not json'` and `'[]'` all give `None`.
  - `test_write_blurb_sends_title_and_abstract_and_records_summary`: with a respx reply of valid JSON, the result is a `PaperBlurb`. The request's user message contains the title and abstract. Its system prompt contains neither "Relevance" nor "topic". The ledger recorded exactly one `"summary"` row.
  - `test_write_blurb_none_when_blocked_or_not_openai`: `FakeLedger(allow=False)` gives `None` with no HTTP call, and `summary_backend="ollama"` gives `None`.
- [ ] **Step 2: Run them and see them fail:** `venv/bin/python -m pytest tests/test_paper_blurb.py -q -p no:cacheprovider`. Expected: ImportError on `PaperBlurb`.
- [ ] **Step 3: Implement** the dataclass, the parser, `_BLURB_SCHEMA`, `_BLURB_SYSTEM` (wording per Global Constraints: sell through specifics, never beyond the abstract, numbers when the abstract has them, no openers, no LaTeX or markdown, no topic) and `write_blurb`. The user message is `f"Title: {title}\n\nAbstract: {abstract}"`.
- [ ] **Step 4: Run them and see them pass,** with the same command.
- [ ] **Step 5: Commit:** "added the paper blurb prompt: a why-read-it hook and what the paper did, as structured output".

### Task 3: Overview and picks

**Files:**
- Modify: `app/services/summarize.py`
- Test: `tests/test_overview_and_picks.py` (new)

**Interfaces:**
- Consumes: Task 1's `_complete` and `_JSON_FORMAT`, and the existing `_clip`, `_HISTORY_OVERVIEW_CHARS` and `_ARXIV_ID`.
- Produces:
  - `@dataclass(frozen=True) class OverviewPaper: arxiv_id: str; title: str; abstract: str`
  - `@dataclass(frozen=True) class Pick: arxiv_id: str; hook: str`
  - `@dataclass class OverviewAndPicks: overview: str; picks: list[Pick]; dropped_citations: list[str]; dropped_picks: list[str]`
  - `parse_overview_and_picks(text: str, offered_ids: list[str], top_ids: list[str]) -> OverviewAndPicks | None`
  - `Summarizer.write_overview_and_picks(topic_name: str, papers: list[OverviewPaper], previous: list[str] = ()) -> OverviewAndPicks | None`
    - `papers` is best first. Only the first 25 are offered, and the first 5 are the pick pool.
    - Purpose `"overview"`, max_tokens 700, system `_OVERVIEW_AND_PICKS_SYSTEM`, schema `_OVERVIEW_AND_PICKS_SCHEMA`.

- [ ] **Step 1: Write the failing tests** (IDs `A=2611.00001` through `F=2611.00006`, offered `[A..F]`, top `[A..E]`):
  - `test_offered_citations_kept_unknown_removed`: overview `"X rose [2611.00001]. Y fell [2611.99999]."` gives `"X rose [2611.00001]. Y fell."`, with `dropped_citations == ["2611.99999"]`.
  - `test_version_suffix_counts_as_offered`: `"[2611.00002v3]"` is kept and normalized to `[2611.00002]`.
  - `test_bare_unknown_id_removed_bare_offered_kept`: `"See 2611.99999 and 2611.00003."` gives `"See and 2611.00003."`. That's acceptable prose; the point is the unknown ID is gone. `dropped_citations == ["2611.99999"]`.
  - `test_removing_all_citations_leaves_clean_prose`: `"A gain [2611.99998], and [2611.99999] a loss ."` gives `"A gain, and a loss."`. There's no `[]`, no double space and no space before punctuation.
  - `test_picks_only_from_top_deduped_capped_at_two`: picks `[F, A, A, B, C]`, each with a hook, give `[A, B]`. `F` (not in the top 5) is in `dropped_picks`.
  - `test_pick_with_empty_hook_dropped`.
  - `test_fenced_json_parses` and `test_invalid_or_missing_overview_is_none`: `'nope'`, `'{"picks": []}'` and `'{"overview": "", "picks": []}'` all give `None`.
  - `test_write_overview_and_picks_request_and_usage` (respx). The user message:
    - lists every offered paper as `[ID] title` with its abstract;
    - marks the first 5 IDs as the pick pool;
    - includes each `previous` overview, clipped;
    - offers a 26th paper never.

    An empty-abstract paper still appears with its title. One `"overview"` row is recorded, and the parsed result comes back.
  - `test_write_overview_and_picks_none_when_blocked_or_not_openai`.
- [ ] **Step 2: Run them and see them fail:** `venv/bin/python -m pytest tests/test_overview_and_picks.py -q -p no:cacheprovider`. Expected: ImportError.
- [ ] **Step 3: Implement.**
  - **Citations:** match `\[?(\d{4}\.\d{4,5})(v\d+)?\]?` anywhere in the overview. Offered IDs are rewritten as `[ID]` when bracketed and left as is when bare. Unknown IDs are removed with their brackets. Then tidy: remove empty `[]`, collapse spaces, and drop spaces before `,.;:`.
  - **Picks:** trim the hook, keep the first two valid ones, in the model's order.
  - **The system prompt** follows Global Constraints and the avoid_repeats rules. Previous overviews are clipped to `_HISTORY_OVERVIEW_CHARS`. Clip each abstract to 1,500 characters (builder's call per the spec).
- [ ] **Step 4: Run them and see them pass,** with the same command.
- [ ] **Step 5: Commit:** "added the overview-and-picks prompt: cited overview and up to two picks from the top 5, validated against the papers offered".

### Task 4: The eval script

**Files:**
- Create: `evals/email_prompts.py`
- Test: `tests/test_eval_email_prompts.py` (new, uses `db_session` and the `test_editions.py` style of digest seeding)

**Interfaces:**
- Consumes: `Summarizer.write_blurb`, `write_overview_and_picks`, `summarize_and_rate`, `OverviewPaper`, `metrics.unsupported_numbers`, `crud.window_papers`, `app.temporal.activities._rank_window`, and `app.cadence.SEND_TIME_ZONE` and `PREP_LEAD`.
- Produces:
  - `@dataclass class EvalWindow: topic_id: str; topic_name: str; topic_query: str; start: datetime; end: datetime; paper_ids: list[str]`. `paper_ids` is ranked best first.
  - `weekly_windows(db, min_papers: int = 5) -> list[EvalWindow]`
  - `overview_checks(result: OverviewAndPicks, papers: list[OverviewPaper]) -> dict`
  - `blind_pairs(items: list[dict], seed: int) -> tuple[list[dict], dict[str, str]]`
  - `main(argv=None)`. The CLI is `venv/bin/python -m evals.email_prompts [--pairs 20] [--seed 1]`.

- [ ] **Step 1: Write the failing tests:**
  - `test_weekly_windows_are_monday_to_monday_and_skip_small_ones`.
    - **Setup:** seed one topic with digests completed Tue 2026-11-03 (3 papers), Thu 2026-11-05 (3 papers) and Wed 2026-11-11 (2 papers).
    - **Window boundaries:** each Monday 08:00 ET minus `PREP_LEAD`, so Monday 01:00 ET.
    - **Expected:** exactly one window, `(2026-11-02 01:00 ET, 2026-11-09 01:00 ET]`, with 6 paper IDs. The week of 11-09 has only 2 papers, so it's skipped. Windows come oldest first, and only windows that have ended count.
  - `test_overview_checks_counts`. For a result with a 100-word overview, one dropped citation, one dropped pick, and a number in the overview that isn't in any abstract, it returns `{"words": 100, "dropped_citations": 1, "dropped_picks": 1, "unsupported_numbers": ["…"]}`.
  - `test_blind_pairs_is_seeded_and_keyed`. `items = [{"paper": "p1", "old": "o1", "new": "n1"}, …]`. The same seed gives the same A/B order. Each blind item has `A` and `B` and no `old` or `new`. The key maps the item index to `"A=new"` or `"A=old"`, and both orders occur over 10 items with seed 1.
- [ ] **Step 2: Run them and see them fail:** `venv/bin/python -m pytest tests/test_eval_email_prompts.py -q -p no:cacheprovider`. Expected: ModuleNotFoundError.
- [ ] **Step 3: Implement.**
  - **`weekly_windows`:** loop over topics with completed digests, from the first digest's week to the latest Monday boundary at or before now. Rank with `_rank_window(db, topic_id, papers, end)` and keep its first return value.
  - **`main`:**
    - **Overviews, oldest first per topic:** for each window, build `OverviewPaper`s from the ranked papers. `previous` is the last 2 new-prompt overviews already generated for that topic in this run.
    - **Blurb pairs:** sample `--pairs` distinct papers, seeded, from the windows' top 5s. For each, run `summarize_and_rate(title, abstract, topic_name, topic_query).summary` (old) and `write_blurb` (new, rendered `hook + "\n" + what`).
    - **Ledger:** use `Summarizer(ledger=DbLedger())`, with no topic.
    - **Output** goes to `evals/results/<stamp>_email_prompts.json`: a summary of the counts, every overview with its inputs and checks, the blurb items with both texts, and the blind key. Two more files are written alongside:
      - `<stamp>_email_prompts_overviews_review.json`: per window, the topic, dates, offered papers (ID, title, abstract), the top 5, the overview and the picks.
      - `<stamp>_email_prompts_blurbs_blind.json`.
    - **Console output:** print the summary and the file names.
- [ ] **Step 4: Run them and see them pass,** with the same command. Then run the full suite: `venv/bin/python -m pytest -q -p no:cacheprovider`. Expected: everything passes. That includes `test_spend_coverage.py` over the new module.
- [ ] **Step 5: Commit:** "added the email prompts eval: weekly windows rebuilt from digests, overviews with picks, and blind blurb pairs".

### Task 5: Run the eval and hand over the review

- [ ] **Step 1: Run** `venv/bin/python -m evals.email_prompts`. Expected: about 17 overviews and 20 pairs, with the summary printed. Check the `llm_usage` rows the run added: they total well under $1. If they don't, stop and tell the owner.
- [ ] **Step 2: Sanity-read the automatic checks:**
  - overview word counts are mostly 80–120;
  - few dropped citations;
  - few or no dropped picks;
  - unsupported numbers are listed.

  Don't judge the gates; those are the owner's.
- [ ] **Step 3: Record** a new `docs/rag-plan.md` section, "5.11 Phase 2d prompts, round 1 (2026-11-16)": the command, files, cost, automatic checks, and "owner review pending" with the four gates.
- [ ] **Step 4: Commit and push:** "ran the email prompts eval for owner review". Commit the result files too, as earlier evals did.

---

## Outcome (2026-11-16)

Built inline (executing-plans), with one whole-change review on the most capable model. Commits `1203b06` (structured output), `6d58f6e` (blurb), `377c8f1` (overview and picks), `63e0a43` (eval), `db49e7a` (eval run and rag-plan §5.11), plus the review fixes. 528 → 553 tests.

- **Probe:** `gpt-5.4-mini` accepts strict `json_schema` on `/chat/completions`, at a cost of $0.00008.
- **Eval run:** 17 overviews and 20 blind blurb pairs, $0.148. The automatic checks are in `docs/rag-plan.md` §5.11. **The owner's review of the four gates is pending.**

**Rulings:**
- Worked on `main`, per the owner's standing instruction.
- The JSON probe ran after Task 1's code: it needs `response_format` to go through the ledger.
- Corrected one new test's own expectation before it ever passed. `_clip` cuts at a word boundary, so the history line ends `Last week:…`.

**Review fixes** (each test failed first, then passed; full suite 553 passed):
- **Bracket groups** (`[A, B]`, `[A; unknown]`, `(unknown)`, `[arXiv:…]`) are now rebuilt from their offered IDs or removed cleanly. Before, `[A, B]` came out as `[A], B`, and unknown groups left `,.` or `()` debris.
- **Bare numbers** count as citations only when they have an arXiv ID's shape: a valid YYMM from 0704 on, with 4 or 5 digits. Prose like `1024.5000` is no longer deleted.
- **Eval windows** now use `_rank_window`'s second value (near-duplicates folded away), as prep does. A check showed none of the 17 windows' top 25 changed, so this run's review files stand.

**Deferred minors (from the review):**
- `parse_overview_and_picks` raises on a non-list `picks`. Strict schema prevents this.
- A non-string pick ID is recorded as `""`.
- Pick hooks aren't run through the citation check. They must be before Round 2 turns IDs into links.
- The old side of the blurb pairs runs without `rating_context`, a small bias against it. Production passes the topic description.
- The eval's avoid-repeats history only includes windows with at least 5 papers, whereas production sees every earlier edition.
- The eval hard-codes 25 and 5 instead of `_OVERVIEW_MAX_PAPERS` and `_PICK_POOL`.
- `previous: list[str] = ()` doesn't match its type hint.

**For Round 2 (from the review):**
- Prep's overview input and pick pool must come from `rank_edition`'s `top` (deduped), not `paper_ids`.
- The Round 2 linkifier should link only offered IDs.
- Old-style IDs (`hep-th/…`) aren't validated.
