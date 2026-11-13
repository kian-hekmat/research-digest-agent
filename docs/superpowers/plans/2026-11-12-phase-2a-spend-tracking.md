# Phase 2a: Spend Tracking and the Spend Guard — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Record every OpenAI call's tokens and cost, enforce the $20/month cap in the app (alert at 80%, stop AI text at 100%), treat OpenAI's own hard spend limit as "budget exhausted", and give the owner a per-topic cost view to measure from.

**Architecture:** A DB-free module (`app/services/spend.py`) owns prices, cost arithmetic, the `SpendLedger` protocol and the `BudgetExhausted` signal. `Summarizer` takes an optional ledger and a `topic_id`; before each OpenAI call it asks the ledger whether the budget allows it, and after each call it records usage. A DB-backed ledger (`app/spend_ledger.py`) writes `llm_usage` rows in its own session (so spend is recorded even if the activity later fails), computes month-to-date spend in UTC, and fires deduplicated alerts (`app/alerts.py`). An admin endpoint reports the month by purpose and by topic.

**Tech Stack:** Python 3.12, FastAPI, SQLAlchemy 2.0, Alembic, Postgres 16, Temporal, httpx + respx, pytest. No new dependencies.

**Spec:** `docs/public-launch-plan.md` (v2.1): §1 "Cost model", §3.6 "Cost control", §5, and Phase 2's "Usage log and spend guard" build items and tests. Phase 2 is split into five plans: **2a spend tracking (this one)**, 2b OpenAI embeddings, 2c two-pass pipeline, 2d picks-first email and prompts, 2e cost and load rehearsal. Read the Phase 1 plan's "Carried forward" section too.

**Not in this plan:**
- Recording embedding calls (2b, when the OpenAI embedder exists).
- Re-rating papers that came in unrated during a 100% stretch, once the month resets (2c's prep step).
- Waitlisting new topics at 80% (Phase 3's signup, which reads `spend_state`).
- The admin stats page and other alerts (Phase 5).

Until 2c lands, papers ingested while the budget is exhausted keep no summary or rating.

## Global Constraints

- Commit straight to `main`; repo's git user; message style like the history (lowercase, past tense); no Claude attribution. Push after each task's review.
- Run tests as `venv/bin/python -m pytest`. Baseline: 355 passed. Alembic head: `a1b2c3d4e5f7`.
- Budget: `openai_monthly_budget_usd = 20.0`; alert line `spend_alert_fraction = 0.8`. "Month" is the UTC calendar month (OpenAI bills in UTC).
- Prices, USD per 1M tokens, as a settings dict `openai_prices` (overridable as JSON in the env), default: `{"gpt-5.4-mini": {"input": 0.75, "cached_input": 0.075, "output": 4.50}, "text-embedding-3-small": {"input": 0.02, "cached_input": 0.02, "output": 0.0}}`.
- Purposes (exact strings): `rating`, `summary`, `overview`, `embedding`, `preview`. 2a uses the first three; 2b and Phase 3 use the rest.
- Behaviour at 100%: chat-model calls (rating, summary, overview) are skipped and return no text, exactly like `SUMMARY_BACKEND=none`; nothing raises. Embeddings are never blocked (2b relies on this). The 80% state is exposed for Phase 3's signup and alerts the owner; it blocks nothing in 2a.
- OpenAI 429 with error code `project_spend_limit_exceeded` or `organization_spend_limit_exceeded`: no retry, treated like 100%, one alert per month.
- Alerts go by email to `alert_email` (new setting, default empty) through the existing `EmailSender`. If empty, log a warning and record nothing, so it alerts once the address is set. Each alert is sent at most once per key (e.g. `spend-80:2026-11`).
- Ollama calls cost nothing and are not recorded.

## Review Focus

1. **Spend recorded even when the reply is unusable:** a 200 response whose content fails to parse still writes its usage row first. Pinned in Task 3.
2. **Two calls cross 80% at once:** exactly one alert email. Pinned in Task 2.
3. **Month boundary:** usage at 23:59 UTC on 31 Oct does not count toward November, even when the server's local time is still Oct 31 in the evening. Pinned in Task 2.
4. **OpenAI hard limit:** a 429 `project_spend_limit_exceeded` returns no text without raising, so Temporal doesn't retry it three times. Pinned in Task 3.
5. **A model with no configured price** fails before any HTTP request, so spend can never go unrecorded. Pinned in Tasks 1 and 3.

---

### Task 1: Prices, cost arithmetic and the ledger contract

**Files:**
- Create: `app/services/spend.py`
- Modify: `app/config.py` (settings block "spend": `openai_monthly_budget_usd`, `spend_alert_fraction`, `openai_prices`, `alert_email`)
- Test: `tests/test_spend.py`

**Interfaces:**
- Produces (all in `app/services/spend.py`, no DB imports):
  - `@dataclass(frozen=True) class Usage: input_tokens: int; cached_input_tokens: int; output_tokens: int; estimated: bool = False`
  - `usage_from_openai(body: dict) -> Usage` — reads `usage.prompt_tokens`, `usage.completion_tokens`, `usage.prompt_tokens_details.cached_tokens` (missing → 0). If `usage` is absent, raises `ValueError`; the caller estimates instead (Task 3).
  - `cost_usd(model: str, usage: Usage, prices: dict[str, dict[str, float]]) -> Decimal` — `((input - cached) * input_price + cached * cached_price + output * output_price) / 1_000_000`, quantized to 6 decimal places with `ROUND_HALF_UP` (Python's default half-even would give `0.000322` for the tiny-call test). Raises `UnknownModelPrice(model)` (subclass of `RuntimeError`) if the model is missing.
  - `is_spend_limit_error(response: httpx.Response) -> bool` — True for status 429 whose JSON `error.code` is `project_spend_limit_exceeded` or `organization_spend_limit_exceeded`. Tolerates non-JSON bodies.
  - `class BudgetExhausted(Exception)`.
  - `class SpendLedger(Protocol)`: `def allow(self, purpose: str) -> bool`, `def record(self, *, model: str, purpose: str, topic_id: str | None, usage: Usage) -> None`, `def hard_limit_hit(self) -> None`.
  - `PURPOSES = ("rating", "summary", "overview", "embedding", "preview")`.

- [ ] **Step 1: Write the failing tests**

```python
from decimal import Decimal
import httpx, pytest
from app.config import Settings
from app.services.spend import Usage, usage_from_openai, cost_usd, is_spend_limit_error, UnknownModelPrice

PRICES = Settings().openai_prices

def test_default_prices_and_budget():
    s = Settings()
    assert s.openai_monthly_budget_usd == 20.0 and s.spend_alert_fraction == 0.8 and s.alert_email == ""
    assert PRICES["gpt-5.4-mini"] == {"input": 0.75, "cached_input": 0.075, "output": 4.50}
    assert PRICES["text-embedding-3-small"]["input"] == 0.02

def test_cost_splits_cached_input():
    u = Usage(input_tokens=1_000_000, cached_input_tokens=400_000, output_tokens=100_000)
    # 600k * 0.75 + 400k * 0.075 + 100k * 4.50, per 1M
    assert cost_usd("gpt-5.4-mini", u, PRICES) == Decimal("0.930000")

def test_tiny_call_keeps_precision():
    assert cost_usd("gpt-5.4-mini", Usage(400, 0, 5), PRICES) == Decimal("0.000323")

def test_unknown_model_raises():
    with pytest.raises(UnknownModelPrice):
        cost_usd("gpt-9", Usage(1, 0, 1), PRICES)

def test_usage_from_openai_reads_cached_tokens():
    body = {"usage": {"prompt_tokens": 500, "completion_tokens": 40, "prompt_tokens_details": {"cached_tokens": 128}}}
    assert usage_from_openai(body) == Usage(500, 128, 40)

def test_usage_from_openai_without_details():
    assert usage_from_openai({"usage": {"prompt_tokens": 9, "completion_tokens": 1}}) == Usage(9, 0, 1)

def test_usage_from_openai_missing_usage_raises():
    with pytest.raises(ValueError):
        usage_from_openai({"choices": []})

@pytest.mark.parametrize("code,expected", [("project_spend_limit_exceeded", True), ("organization_spend_limit_exceeded", True), ("rate_limit_exceeded", False)])
def test_spend_limit_detection(code, expected):
    r = httpx.Response(429, json={"error": {"code": code, "message": "x"}})
    assert is_spend_limit_error(r) is expected

def test_spend_limit_detection_tolerates_non_json():
    assert is_spend_limit_error(httpx.Response(429, text="Too Many Requests")) is False

def test_prices_overridable_from_env(monkeypatch):
    monkeypatch.setenv("OPENAI_PRICES", '{"m": {"input": 1, "cached_input": 1, "output": 2}}')
    assert Settings().openai_prices == {"m": {"input": 1.0, "cached_input": 1.0, "output": 2.0}}
```

- [ ] **Step 2: Run to verify they fail** — `venv/bin/python -m pytest tests/test_spend.py -v` — Expected: FAIL (`ModuleNotFoundError: app.services.spend`).
- [ ] **Step 3: Implement** the settings and `app/services/spend.py` per Interfaces. Use `Decimal` throughout the arithmetic (convert prices with `Decimal(str(p))`), not floats.
- [ ] **Step 4: Run the new tests and the full suite** — Expected: all pass.
- [ ] **Step 5: Commit** — `git commit -m "added spend settings, prices and cost arithmetic"`

---

### Task 2: Usage table, month-to-date spend, and alerts

**Files:**
- Create: `alembic/versions/b2c3d4e5f6a8_llm_usage_and_sent_alerts.py` (`down_revision = "a1b2c3d4e5f7"`), `app/spend_ledger.py`, `app/alerts.py`
- Modify: `app/models.py` (`LlmUsage`, `SentAlert`)
- Test: `tests/test_spend_ledger.py`, `tests/test_alerts.py`

**Interfaces:**
- Consumes: Task 1's `Usage`, `cost_usd`, `SpendLedger`, settings.
- Produces:
  - `models.LlmUsage` (table `llm_usage`): `id` BigInteger identity pk; `created_at` timestamptz not null, server default now, indexed; `model` String not null; `purpose` String(16) not null with check constraint `purpose IN (...PURPOSES)`; `topic_id` UUID FK `topics.id` `ondelete="SET NULL"`, nullable, indexed; `input_tokens`, `cached_input_tokens`, `output_tokens` Integer not null; `cost_usd` Numeric(12, 6) not null; `estimated` Boolean not null default false.
  - `models.SentAlert` (table `sent_alerts`): `key` String pk; `sent_at` timestamptz not null default now.
  - `app/spend_ledger.py`:
    - `month_start(now: datetime) -> datetime` — first instant of `now`'s UTC month.
    - `month_to_date_usd(db, now: datetime) -> Decimal` — sum of `cost_usd` since `month_start(now)`.
    - `spend_state(db, now: datetime, settings=None) -> Literal["ok", "soft", "hard"]` — `hard` when MTD ≥ budget, `soft` when ≥ budget × `spend_alert_fraction`, else `ok`. Phase 3's signup will call this.
    - `class DbLedger` implementing `SpendLedger`, constructor `DbLedger(settings=None, now: Callable[[], datetime] = utcnow, alert=send_alert)`. Each method opens and closes its own `SessionLocal()` and commits.
      - `allow(purpose)`: True for `embedding`; otherwise `spend_state(...) != "hard"`.
      - `record(...)`: inserts the row (cost via `cost_usd`), commits, then checks thresholds. If the post-insert MTD is at or over 80% it calls `alert(db, f"spend-80:{YYYY-MM}", ...)`; at or over 100%, `alert(db, f"spend-100:{YYYY-MM}", ...)`. Both may fire from one record.
      - `hard_limit_hit()`: `alert(db, f"openai-hard-limit:{YYYY-MM}", ...)`.
  - `app/alerts.py`: `send_alert(db, key: str, subject: str, body: str, *, settings=None, sender=None) -> bool`. If `alert_email` is empty: log a warning, return False, record nothing. Otherwise `INSERT INTO sent_alerts (key) ... ON CONFLICT DO NOTHING RETURNING key`. If a row came back, send a plain email (`DigestEmailContent(subject, body, "<pre>"+escape(body)+"</pre>")`) to `alert_email`, commit, and return True. If no row came back (already sent), return False. If the send raises, roll back the insert and re-raise, so the next crossing retries.
  - `DbLedger` catches and logs any exception from `alert(...)`: a failed alert email must never fail the summary call that triggered it. The usage row is already committed by then.
  - Alert copy: subject `"Research digest: OpenAI spend at {pct}% of ${budget}"`, body with month-to-date dollars, the budget, and what happens next ("new topics now wait" at 80%; "AI summaries are paused until the 1st" at 100%; "OpenAI's own spend limit was hit; AI summaries are paused" for the hard limit).

- [ ] **Step 1: Write the failing tests** (DB tests use `db_session`; build ledgers with an injected `now`, and a fake sender that records sends):

```python
NOV = datetime(2026, 11, 12, 15, tzinfo=timezone.utc)

def _usage_row(db, when, cost, purpose="summary", topic_id=None): ...  # inserts LlmUsage directly

def test_month_to_date_counts_only_this_utc_month(db_session):
    _usage_row(db_session, datetime(2026, 10, 31, 23, 59, tzinfo=timezone.utc), "5")
    _usage_row(db_session, datetime(2026, 11, 1, 0, 0, tzinfo=timezone.utc), "2")
    assert month_to_date_usd(db_session, NOV) == Decimal("2")

@pytest.mark.parametrize("spent,state", [("15.99", "ok"), ("16", "soft"), ("19.99", "soft"), ("20", "hard"), ("25", "hard")])
def test_spend_state_thresholds(db_session, spent, state):
    _usage_row(db_session, NOV, spent)
    assert spend_state(db_session, NOV) == state

def test_record_writes_one_row_with_cost_and_topic(db_session): ...
    # DbLedger(now=lambda: NOV).record(model="gpt-5.4-mini", purpose="rating", topic_id=t.id, usage=Usage(400, 0, 5))
    # one LlmUsage row: purpose "rating", topic_id t.id, cost Decimal("0.000323"), estimated False

def test_allow_blocks_chat_purposes_at_100_but_never_embeddings(db_session): ...
    # seed $20 this month: allow("summary") is False, allow("rating") False, allow("embedding") True
    # with now = 2026-12-01T00:00Z (new month): allow("summary") True

def test_existing_work_still_runs_at_80(db_session): ...
    # seed $16 this month: spend_state == "soft", and allow("summary"), allow("rating"), allow("overview") are all True

def test_crossing_80_sends_one_alert_even_when_recorded_twice(db_session): ...
    # alert_email set; seed $15.99; two records of $0.01 each → exactly one email, key "spend-80:2026-11"

def test_crossing_100_sends_both_alerts_once(db_session): ...

def test_hard_limit_hit_alerts_once_per_month(db_session): ...

def test_no_alert_email_logs_and_records_nothing(db_session, caplog): ...
    # send_alert returns False, sent_alerts empty, a warning mentions ALERT_EMAIL

def test_failed_send_does_not_mark_alert_sent(db_session): ...
    # sender raises → send_alert raises or returns False, and sent_alerts stays empty
```

  Also: `test_failed_alert_never_fails_record` — the sender raises; `DbLedger.record` returns normally, the usage row exists, and `sent_alerts` is empty.

- [ ] **Step 2: Run to verify they fail.**
- [ ] **Step 3: Implement** the migration, models, `app/spend_ledger.py` and `app/alerts.py`. The migration's downgrade drops both tables.
- [ ] **Step 4: Run the new tests and the full suite** — Expected: all pass (the migration round trip runs via `db_session` teardown).
- [ ] **Step 5: Commit** — `git commit -m "added the llm usage table, month-to-date spend and alerts"`

---

### Task 3: The summarizer records usage and respects the budget

**Files:**
- Modify: `app/services/summarize.py`, `app/temporal/activities.py` (`summarize_paper`, `rate_relevance`, `write_overview`), `app/backfill_summaries.py`, `evals/rag.py`, `evals/overviews.py`, `evals/fulltext.py`
- Test: `tests/test_summarize_usage.py` (new), `tests/test_temporal_workflows.py` (one workflow test), `tests/test_spend_coverage.py` (new)

**Interfaces:**
- Consumes: Task 1's `spend` module; Task 2's `DbLedger`.
- Produces:
  - `Summarizer(settings=None, http_client=None, *, ledger: SpendLedger | None = None, topic_id: str | None = None)`. With `ledger=None` nothing is recorded or checked: tests and Ollama runs use this.
  - `_complete(*, system, user, max_tokens, purpose: str) -> str | None`. Every caller passes its purpose:
    - `summarize_paper` and the main `summarize_and_rate` call → `"summary"`;
    - `rate_relevance`, including the separate rating inside `_rate_separately_if` → `"rating"`;
    - `write_overview` → `"overview"`.
  - OpenAI path order, in this order:
    1. `cost_usd` precheck that the model has a price (raises `UnknownModelPrice`, no HTTP).
    2. `ledger.allow(purpose)` False → return None (no HTTP).
    3. POST.
    4. If `is_spend_limit_error(resp)` → `ledger.hard_limit_hit()`, return None.
    5. `raise_for_status()`.
    6. `ledger.record(...)` with `usage_from_openai(body)`. If usage is missing: `Usage(len(system + user) // 3, 0, max_tokens, estimated=True)` and log a warning.
    7. Parse content.
  - Every public method treats a `None` from `_complete` exactly like summaries-disabled: `summarize_and_rate` returns `RatedSummary(summary=None, relevance=None)`, `rate_relevance` returns None, `write_overview` returns None. `parse_*` never sees None.
  - Activities construct `Summarizer(ledger=DbLedger(), topic_id=input.topic_id or None)`; `write_overview` passes `input.topic_id`. `backfill_summaries` and the three evals pass `ledger=DbLedger()` (eval spend counts toward the cap; that's intended).

- [ ] **Step 1: Write the failing tests.** `tests/test_summarize_usage.py` uses respx and a `FakeLedger` that records `allow`/`record`/`hard_limit_hit` calls (no DB):

```python
OK = {"choices": [{"message": {"content": "Relevance: 8\n\nA summary."}}],
      "usage": {"prompt_tokens": 500, "completion_tokens": 40, "prompt_tokens_details": {"cached_tokens": 0}}}

@pytest.mark.parametrize("call,purposes", [
    (lambda s: s.summarize_paper("T", "A"), ["summary"]),
    (lambda s: s.summarize_and_rate("T", "A", "RLHF", "rlhf"), ["summary"]),
    (lambda s: s.summarize_and_rate("T", "A", "RLHF", "rlhf", excerpts=["x"]), ["summary", "rating"]),
    (lambda s: s.rate_relevance("T", "A", "RLHF", "rlhf"), ["rating"]),
    (lambda s: s.write_overview("RLHF", ["s1", "s2"]), ["overview"]),
])
def test_every_openai_call_records_exactly_one_row_with_its_purpose(call, purposes): ...
    # assert [r.purpose for r in ledger.records] == purposes; each Usage == Usage(500, 0, 40); topic_id == "t1"

def test_blocked_purpose_makes_no_http_call_and_returns_no_text(): ...
    # FakeLedger(allow=False); respx route asserts not called; summarize_and_rate → RatedSummary(None, None)

def test_spend_limit_429_returns_none_without_raising_and_reports_once(): ...
    # respx 429 {"error": {"code": "project_spend_limit_exceeded"}} → rate_relevance returns None; hard_limit_hit called once; nothing recorded

def test_other_429_still_raises(): ...
    # 429 rate_limit_exceeded → httpx.HTTPStatusError (Temporal retries as before)

def test_usage_recorded_even_if_content_is_malformed(): ...
    # body with usage but "choices": [] → raises on parse, AND one record exists

def test_missing_usage_is_estimated(): ...
    # body without "usage" → one record with estimated=True, output_tokens == max_tokens of that call

def test_unknown_model_fails_before_http(): ...
    # Settings(openai_model="gpt-9") → UnknownModelPrice, route not called

def test_no_ledger_records_nothing_and_still_works(): ...
```

  `tests/test_spend_coverage.py` is an AST check over `app/` and `evals/`. Every `Summarizer(...)` call outside `app/services/summarize.py` must pass a `ledger=` keyword, so a new call path can't spend without recording.

  Workflow test in `tests/test_temporal_workflows.py`. Seed $20 of `llm_usage` dated now. Switch the backend for this test only with `monkeypatch.setattr(get_settings(), "summary_backend", "openai")` and a dummy `openai_api_key` (conftest forces `none`; activities read the cached settings). Mount an OpenAI respx route that must not be called. Run `DigestWorkflow` for a topic with one fetched paper (existing arXiv fakes). Then `SendDigestEmailsWorkflow`. Expect: the digest completes; the paper has no summary or relevance; one email is sent, with the paper and no AI text.

- [ ] **Step 2: Run to verify they fail.**
- [ ] **Step 3: Implement** per Interfaces; wire activities, backfill and evals.
- [ ] **Step 4: Run the full suite** — Expected: all pass. Existing respx-based summarizer tests keep passing unchanged, because their response bodies may lack `usage` and they pass no ledger.
- [ ] **Step 5: Commit** — `git commit -m "recorded every OpenAI call and stopped AI text at the budget"`

---

### Task 4: Admin usage report

**Files:**
- Create: `app/routers/admin.py`
- Modify: `app/main.py` (include it), `app/schemas.py` (`UsageReport`), `.env.example` (`ALERT_EMAIL`, `OPENAI_MONTHLY_BUDGET_USD`, `OPENAI_PRICES` commented), `README.md` (Configuration rows for the three settings; API row for `GET /admin/usage`)
- Test: `tests/test_admin_usage.py`

**Interfaces:**
- Consumes: Task 2's `month_to_date_usd`, `spend_state`, `LlmUsage`.
- Produces: `GET /admin/usage?month=YYYY-MM` (default: the current UTC month), behind `require_admin` like every router. Response `UsageReport`:
  - `month: str`, `budget_usd: float`, `spent_usd: float` (6 dp), `state: "ok" | "soft" | "hard"` (state only for the current month; `null` for past months);
  - `by_purpose: dict[str, {calls: int, input_tokens: int, cached_input_tokens: int, output_tokens: int, cost_usd: float}]`;
  - `by_topic: list[{topic_id: str | None, topic_name: str | None, calls: int, cost_usd: float}]`, most expensive first. Rows with a deleted topic group under `topic_id: null`.
  - An invalid `month` returns 422.

- [ ] **Step 1: Write the failing tests:** seed rows across two topics, three purposes and two months. Assert the totals per purpose and per topic, the ordering, the `month=2026-10` filter, the 422 on `month=2026-13`, and `state` for the current month. The Phase 1 route sweep in `tests/test_admin_auth.py` automatically checks the 401.
- [ ] **Step 2: Run to verify they fail.**
- [ ] **Step 3: Implement**, with one grouped SQL query per section.
- [ ] **Step 4: Run the full suite** — Expected: all pass.
- [ ] **Step 5: Commit** — `git commit -m "added an admin usage report by purpose and topic"`

---

### Task 5: Deploy and start measuring

No code.

- [ ] **Step 1: Back up and migrate live.** `pg_dump` to `~/research-digest-backups/`. Then `docker-compose stop worker`, `docker exec research-digest-agent-api-1 alembic upgrade head`, and `docker-compose up -d --build api worker`. Run it away from 10:00 PT daily and Mon/Thu 08:00 PT.
- [ ] **Step 2: Ask the owner for the alert address** and set `ALERT_EMAIL` in `.env` (gitignored), then restart `worker`. Until it's set, alerts only log.
- [ ] **Step 3: Verify.** After the next daily run, `GET /admin/usage` (with the admin token) shows rows for `rating`, `summary` and `overview` against the owner's topics, with non-zero cost. `llm_usage` row count equals the number of OpenAI calls the worker logged.
- [ ] **Step 4: Record the outcome** at the end of this plan (commits, live migration time, first day's cost per topic) and push.

---

## Outcome (2026-11-12)

Built inline (executing-plans), one whole-plan review (verdict: ready to merge; all five Review Focus behaviours verified, including by concurrent and non-UTC probes). Commits `531ecb5`, `2532c79`, `99bbeb1`, `ef54dd7`, `647ee20`, `d44e62f` (review fixes). 422 tests pass. Live database migrated to `b2c3d4e5f6a8` at 15:17 PT after a `pg_dump` to `~/research-digest-backups/`; worker schedules unchanged; `GET /admin/usage` live.

**Deviations, each a ruling:**
- The price precheck runs only when a ledger is given. Existing ledger-less tests use an unpriced model, and the AST coverage test forbids ledger-less callers in `app/` and `evals/`.
- `alembic/env.py` now keeps existing loggers (`disable_existing_loggers=False`). In-process migrations in the test suite had silenced every app logger.
- Review fixes beyond the plan:
  - `insufficient_quota` (out of credits) is treated like the hard spend limit.
  - The 80% alert no longer claims new topics wait, since that isn't true until Phase 3.
  - The backfill counts budget-blocked calls as skipped, not written.
  - Concurrency and non-UTC tests now pin Review Focus 2 and 3.

**Still to do from Task 5:**
- ~~Set `ALERT_EMAIL`~~ Done 2026-11-12 19:52 PT: set in `.env`, worker recreated. Verified without sending: the worker sees the address, and the SMTP relay accepts a TLS login.
- After the next daily run, check that `/admin/usage` shows rating, summary and overview rows with non-zero cost per topic, and record the first day's cost here.

**Carried forward**
- Phase 3: `spend_state` doesn't see OpenAI's own hard limit or out-of-credits. Signup reads it to waitlist new topics, so it needs a marker that works even without `ALERT_EMAIL`.
- 2b: `OPENAI_PRICES` isn't validated, and an override replaces the whole dict, so it can drop the `text-embedding-3-small` price the embedder needs. Validate or merge it.
- Phase 5 (before the Hetzner image deploy): pass `ALERT_EMAIL`, `OPENAI_MONTHLY_BUDGET_USD` and `OPENAI_PRICES` through `docker-compose.yml`. Today they only work through the bind-mounted `.env`.
- Small, when the files are next touched:
  - The purpose list is duplicated in three places with no equality test.
  - `BudgetExhausted` and `PURPOSES` are unused.
  - Evals keep running when the budget blocks them.
  - The alert send holds a row lock during a slow SMTP send.
  - `month_to_date_usd`'s `<= now` bound can hide clock-skewed rows briefly.
  - The admin `spent_usd` is summed from rounded floats.
  - Backfill and eval spend has no topic.
  - The AST check can be dodged with `ledger=None`.
