# Phase 1: Multi-user Data Model and Lockdown — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace "a subscription is an email string" with real subscribers, make topics shared and deduplicated by normalized query, fetch and email only for confirmed subscribers, and lock the existing JSON API behind an admin token, without losing any of the live data.

**Architecture:** A new `subscribers` table owns the email and its lifecycle timestamps; `subscriptions` point at it. Topics gain a unique `normalized_query` computed by one pure module (`app/topic_query.py`) that later phases reuse for public input. One SQL filter (`crud.active_subscriptions_query`) defines "active" for fetching, sending and backfills. Every existing router gets an `X-Admin-Token` dependency that fails closed.

**Tech Stack:** Python 3.12, FastAPI, SQLAlchemy 2.0, Alembic, Postgres 16 + pgvector, Temporal (time-skipping test server), pytest. No new dependencies.

**Spec:** `docs/public-launch-plan.md` (v2.1), §3.2–3.3 and "Phase 1: Multi-user data model and lockdown". Read both.

## Global Constraints

- Commit straight to `main` and push after each task; author is the repo's configured git user; no Claude attribution or co-author lines. CI must stay green on each push.
- Run tests as `venv/bin/python -m pytest` (the venv's console scripts point at the project's old path; `venv/bin/python` works). Baseline: 292 passed.
- Tests build the schema with the real Alembic migrations against Postgres at `TEST_DATABASE_URL` (see `tests/conftest.py`); never `create_all`.
- Current Alembic head: `e1f2a3b4c5d6`. The new migration's `down_revision` is `e1f2a3b4c5d6`.
- Limits (settings, exact defaults): `max_subscribers=200`, `max_topics_per_subscriber=5`, `max_active_topics=100`, `max_query_length=200`, `public_papers_per_topic_min=5`, `public_papers_per_topic_max=10`, `public_papers_per_topic_default=5`.
- Cadence: the migration sets every subscription to `twice_weekly`. The column and the admin API's `cadence` field stay.
- Admin auth: header `X-Admin-Token`, compared with `hmac.compare_digest`; an empty configured token rejects everything (fail closed). `/health` stays public.
- Migrations must not import `app.*` (code changes later; migrations must not). Copy the normalization logic into the migration.

## Review Focus

1. **Email case:** `Me@Example.com` and `me@example.com` are one subscriber, in the migration and in `crud.get_or_create_subscriber`. Pinned in Task 2.
2. **Admin token unset:** with `ADMIN_TOKEN` empty, every admin route returns 401, even with an empty `X-Admin-Token` header. Pinned in Task 4.
3. **Partly-active topics:** a topic with one confirmed and one unconfirmed subscriber is fetched, and only the confirmed one is emailed; a topic whose only subscriber is unconfirmed, unsubscribed, waitlisted or inactive is not fetched. Pinned in Task 3.
4. **Duplicate merge collisions:** two topics that normalize the same and share a subscriber and papers merge without unique-key violations, keeping one subscription and one link per paper. Pinned in Task 2.
5. **Curly quotes and empty queries:** `“RLHF”` normalizes like `"RLHF"`; a query that is only quotes or spaces is rejected. Pinned in Task 1.

---

### Task 1: Query normalization and limit settings

**Files:**
- Create: `app/topic_query.py`
- Modify: `app/config.py` (new settings block "public limits"), `app/services/arxiv.py` (rename `_FIELD_PREFIX` to `FIELD_PREFIX`, update its one use)
- Test: `tests/test_topic_query.py`

**Interfaces:**
- Produces:
  - `normalize_query(query: str) -> str` — admin and public. Field-prefixed queries (matching `app.services.arxiv.FIELD_PREFIX` after `strip()`) are returned stripped and otherwise unchanged. Phrases: remove double quotes (`"`, `“`, `”`), collapse runs of whitespace to one space, strip, lowercase. Raises `ValueError("empty query")` if the result is empty.
  - `validate_public_query(query: str, settings: Settings | None = None) -> str` — returns the normalized phrase or raises `ValueError` with a user-facing message. Rejects: field-prefixed queries; normalized length over `settings.max_query_length`; anything not matching `settings.public_query_pattern`.
  - Settings: the seven limits in Global Constraints, plus `public_query_pattern: str = r"^[\w\s\-.,'+/()&]+$"`.

- [ ] **Step 1: Write the failing tests**

```python
import pytest
from app.config import Settings
from app.topic_query import normalize_query, validate_public_query

@pytest.mark.parametrize("raw", ["RLHF", " rlhf ", '"RLHF"', "“RLHF”", "  \"Rlhf\"\t"])
def test_variants_normalize_to_one_topic(raw):
    assert normalize_query(raw) == "rlhf"

def test_whitespace_collapses():
    assert normalize_query("physics-informed   neural\nnetworks") == "physics-informed neural networks"

def test_apostrophes_are_kept():
    assert normalize_query("Bayes' rule") == "bayes' rule"

@pytest.mark.parametrize("raw", ["cat:cs.LG", "  cat:math.NA ", "ti:Transformer AND au:Vaswani"])
def test_field_prefixed_queries_pass_through(raw):
    assert normalize_query(raw) == raw.strip()

@pytest.mark.parametrize("raw", ["", "   ", '""', "“ ”"])
def test_empty_queries_are_rejected(raw):
    with pytest.raises(ValueError):
        normalize_query(raw)

def test_public_rejects_field_prefix():
    with pytest.raises(ValueError, match="phrase"):
        validate_public_query("cat:cs.LG")

def test_public_rejects_too_long():
    with pytest.raises(ValueError):
        validate_public_query("a" * 201, Settings(max_query_length=200))

def test_public_rejects_disallowed_characters():
    with pytest.raises(ValueError):
        validate_public_query("rlhf; DROP TABLE")

def test_public_accepts_and_normalizes():
    assert validate_public_query(' "Physics-Informed Neural Networks" ') == "physics-informed neural networks"

def test_limit_defaults():
    s = Settings()
    assert (s.max_subscribers, s.max_topics_per_subscriber, s.max_active_topics, s.max_query_length) == (200, 5, 100, 200)
    assert (s.public_papers_per_topic_min, s.public_papers_per_topic_max, s.public_papers_per_topic_default) == (5, 10, 5)
```

- [ ] **Step 2: Run to verify they fail**
  Run: `venv/bin/python -m pytest tests/test_topic_query.py -v` — Expected: FAIL (`ModuleNotFoundError: app.topic_query`).

- [ ] **Step 3: Implement `app/topic_query.py` and the settings.** The "phrase" wording in the field-prefix error message is user-facing: `"Enter a phrase, like \"physics-informed neural networks\". Category and field searches aren't available."`

- [ ] **Step 4: Run the new tests and the full suite** — Expected: all pass (292 + new).

- [ ] **Step 5: Commit and push** — `git commit -m "added query normalization and public limit settings"`

---

### Task 2: Subscribers, shared topics, and the data migration

**Files:**
- Create: `alembic/versions/a1b2c3d4e5f7_subscribers_and_shared_topics.py`
- Modify: `app/models.py`, `app/crud.py` (`create_topic`, `create_subscription`, new `get_or_create_subscriber`), `app/schemas.py` (`TopicCreate.name` max 200)
- Test: `tests/test_migration_subscribers.py` (new), `tests/test_subscriptions.py`, `tests/test_topics.py`

**Interfaces:**
- Consumes: nothing from Task 1 inside the migration (copied logic); `normalize_query` in `crud.create_topic`.
- Produces:
  - `models.Subscriber`: table `subscribers`; `id` (UUID str, pk), `email` (String, not null; unique index `uq_subscribers_email_lower` on `lower(email)`), `confirmed_at`, `unsubscribed_at`, `waitlisted_at`, `last_manage_link_sent_at`, `us_resident_attested_at` (all `DateTime(timezone=True)`, nullable), `token_version` (Integer, not null, server default 0), `created_at`, `updated_at`; relationship `subscriptions` (cascade delete-orphan, passive deletes).
  - `models.Subscription`: drops `email`; gains `subscriber_id` (FK `subscribers.id`, `ondelete="CASCADE"`, not null, indexed), `label` (String(200), nullable), `waitlisted_at` (nullable). Unique constraint `uq_subscription_topic_subscriber` on `(topic_id, subscriber_id)`. Read-only property `email -> str` returning `self.subscriber.email` (keeps `SubscriptionOut`, `DueSubscription` and the send workflow unchanged).
  - `models.Topic`: `name` becomes `String(200)`, not unique; gains `normalized_query` (String(200), not null, unique `uq_topics_normalized_query`).
  - `crud.get_or_create_subscriber(db, email: str, *, confirmed: bool) -> models.Subscriber` — case-insensitive lookup; creates with `confirmed_at=utcnow()` when `confirmed`. Does not commit.
  - `crud.create_subscription(db, topic_id, email, cadence, max_papers=None)` — same signature as today; admin-created subscribers are confirmed. Still raises `ValueError` on a duplicate `(topic, subscriber)`.
  - `crud.create_topic` sets `normalized_query = normalize_query(topic.query)`; a duplicate normalized query raises `ValueError` (router already maps it to 409).

**Migration algorithm (upgrade), in order:**
1. Create `subscribers`. Insert one row per `lower(email)` in `subscriptions`, keeping the earliest subscription's email casing, `confirmed_at = created_at = min(subscriptions.created_at)`, `us_resident_attested_at = NULL`.
2. Add `subscriptions.subscriber_id` (nullable), fill it by `lower(email)`, delete duplicate `(topic_id, subscriber_id)` rows keeping the oldest, then set not null, add FK and index, drop `uq_subscription_topic_email` and `email`, add `label`, `waitlisted_at`, the new unique constraint.
3. Add `topics.normalized_query` (nullable). Compute it in Python per row with the copied normalizer. Group topics by it; for each group keep the oldest (`created_at`, then `id`) and, for each other topic: move `subscriptions` (skip ones whose subscriber already has the keeper), move `digests`, insert its `topic_paper` rows into the keeper with `ON CONFLICT (topic_id, paper_id) DO NOTHING`, then delete the topic (cascades the rest). Keeper's `last_checked_at` becomes the group's minimum, so no paper is skipped.
4. Set `normalized_query` not null and unique; drop the unique constraint on `topics.name` (look up its name with `sa.inspect`, it predates naming conventions); widen `name` to 200.
5. `UPDATE subscriptions SET cadence = 'twice_weekly'`; `UPDATE subscriptions SET max_papers = 10 WHERE max_papers IS NULL`.

**Downgrade:** re-add `subscriptions.email` from the subscriber, restore `uq_subscription_topic_email`, drop `subscriber_id`, `label`, `waitlisted_at` and `subscribers`; drop `normalized_query`; narrow `name` to 100 (`USING left(name, 100)`) and restore its unique constraint. Merges, cadence and `max_papers` are not undone.

- [ ] **Step 1: Write the failing migration test** in `tests/test_migration_subscribers.py`. It does not use `db_session`. It defines its own fixture `alembic_at_head_from_seed`: downgrade to `base`, upgrade to `e1f2a3b4c5d6`, seed with raw SQL (below), upgrade to `head`, yield a `SessionLocal()`, and in a `finally` close it and downgrade to `base`. Reuse `_ALEMBIC_CFG` from `tests/conftest.py`. Seed:
  - topics: A `("RLHF", "RLHF")` created first, B `("rlhf dup", ' "rlhf" ')`, C `("Numerical Analysis", "cat:math.NA")`;
  - subscriptions: `me@example.com` on A (weekly, max NULL), `Me@Example.com` on B (biweekly, max 5), `me@example.com` on C (twice_weekly, max 10), `other@example.com` on B (weekly, max 7);
  - one paper P linked to both A and B, paper Q linked only to B; one completed digest on B.

```python
def test_upgrade_merges_and_preserves(alembic_at_head_from_seed):
    db = alembic_at_head_from_seed  # a Session after upgrade to head
    assert db.execute(text("select count(*) from subscribers")).scalar() == 2
    me = db.execute(text("select id, email, confirmed_at from subscribers where lower(email)='me@example.com'")).one()
    assert me.email == "me@example.com" and me.confirmed_at is not None
    topics = db.execute(text("select name, normalized_query from topics order by created_at")).all()
    assert [t.normalized_query for t in topics] == ["rlhf", "cat:math.NA"]
    a = db.execute(text("select id from topics where normalized_query='rlhf'")).scalar()
    assert db.execute(text("select count(*) from subscriptions where topic_id=:a"), {"a": a}).scalar() == 2  # me (once) + other
    assert db.execute(text("select count(*) from topic_paper where topic_id=:a"), {"a": a}).scalar() == 2  # P once, Q
    assert db.execute(text("select count(*) from digests where topic_id=:a"), {"a": a}).scalar() == 1
    assert {r[0] for r in db.execute(text("select distinct cadence from subscriptions"))} == {"twice_weekly"}
    assert db.execute(text("select count(*) from subscriptions where max_papers is null")).scalar() == 0
    assert db.execute(text("select count(*) from papers")).scalar() == 2

def test_downgrade_then_upgrade_round_trips(alembic_at_head_from_seed):
    alembic_at_head_from_seed.close()
    command.downgrade(_ALEMBIC_CFG, "e1f2a3b4c5d6")
    # subscriptions have an email column again: 3 rows (me x2 topics after merge, other x1),
    # emails all present; topics table has no normalized_query column
    command.upgrade(_ALEMBIC_CFG, "head")
    # same counts as test_upgrade_merges_and_preserves: 2 subscribers, 2 topics, 3 subscriptions
```

- [ ] **Step 2: Run it to verify it fails** — `venv/bin/python -m pytest tests/test_migration_subscribers.py -v` — Expected: FAIL (no revision past `e1f2a3b4c5d6`).
- [ ] **Step 3: Write the migration** per the algorithm above.
- [ ] **Step 4: Update `app/models.py`, `app/crud.py`, `app/schemas.py`** per Interfaces.
- [ ] **Step 5: Add tests to `tests/test_subscriptions.py` and `tests/test_topics.py`:**

```python
def test_same_email_any_case_is_one_subscriber(client, db_session):
    t1 = client.post("/topics", json={"name": "RLHF", "query": "rlhf"}).json()
    t2 = client.post("/topics", json={"name": "PINNs", "query": "physics-informed neural networks"}).json()
    client.post(f"/topics/{t1['id']}/subscriptions", json={"email": "Me@Example.com"})
    client.post(f"/topics/{t2['id']}/subscriptions", json={"email": "me@example.com"})
    assert db_session.query(models.Subscriber).count() == 1

def test_deleting_subscriber_removes_subscriptions_not_topics(client, db_session): ...
    # delete the Subscriber row; its subscriptions are gone, both topics remain

def test_topic_with_same_normalized_query_is_a_conflict(client):
    client.post("/topics", json={"name": "RLHF", "query": "RLHF"})
    assert client.post("/topics", json={"name": "Other name", "query": ' "rlhf" '}).status_code == 409

def test_topic_names_need_not_be_unique(client):
    client.post("/topics", json={"name": "Same", "query": "rlhf"})
    assert client.post("/topics", json={"name": "Same", "query": "dpo"}).status_code == 201
```

- [ ] **Step 6: Run the full suite** — Expected: all pass. Existing tests that call `crud.create_subscription` keep working unchanged (same signature).
- [ ] **Step 7: Commit and push** — `git commit -m "added subscribers and shared topics, with a migration that merges duplicates"`

---

### Task 3: Fetch and email only for confirmed, active subscribers

**Files:**
- Modify: `app/crud.py` (`list_topic_ids` → `list_active_topic_ids`, `list_due_subscriptions`, new `active_subscriptions_query`), `app/temporal/activities.py` (`list_active_topic_ids` docstring and call), `app/backfill_summaries.py` (use `active_subscriptions_query`), `tests/conftest.py` (helpers below)
- Test: `tests/test_active_filtering.py` (new), `tests/test_temporal_workflows.py`

**Interfaces:**
- Consumes: Task 2 models.
- Produces:
  - Test helpers in `tests/conftest.py`, plain functions importable as `from tests.conftest import make_subscriber, subscribe`, both committing: `make_subscriber(db, email="me@example.com", *, confirmed=True, unsubscribed=False, waitlisted=False) -> models.Subscriber` and `subscribe(db, topic, subscriber, *, active=True, waitlisted=False, cadence="twice_weekly", max_papers=None, last_sent_at=None) -> models.Subscription`.
  - `crud.active_subscriptions_query(db) -> Query[models.Subscription]` — joins `Subscriber`; `Subscription.active is true`, `Subscription.waitlisted_at is null`, `Subscriber.confirmed_at is not null`, `Subscriber.unsubscribed_at is null`, `Subscriber.waitlisted_at is null`.
  - `crud.list_active_topic_ids(db) -> list[str]` — distinct topic ids from that query. Replaces `list_topic_ids` (remove it; its only caller is the activity).
  - `crud.list_due_subscriptions(db, now)` — same contract as today, built on `active_subscriptions_query`.

- [ ] **Step 1: Write the failing tests** in `tests/test_active_filtering.py`:

```python
@pytest.mark.parametrize("state", ["unconfirmed", "unsubscribed", "subscriber_waitlisted", "subscription_waitlisted", "inactive"])
def test_topic_with_only_an_ineligible_subscriber_is_not_fetched_or_emailed(db_session, state): ...
    # one topic, one subscriber in `state`, last_sent_at long ago
    # assert crud.list_active_topic_ids(db_session) == []
    # assert crud.list_due_subscriptions(db_session, NOW) == []

def test_topic_shared_with_an_eligible_subscriber_is_fetched_but_only_they_are_emailed(db_session): ...
    # confirmed + unconfirmed on the same topic
    # assert list_active_topic_ids == [topic.id]; due emails == ["confirmed@example.com"]

def test_topic_with_no_subscriptions_is_not_fetched(db_session): ...
```

  And one workflow-level test in `tests/test_temporal_workflows.py`: `SendDigestEmailsWorkflow` with one confirmed and one unconfirmed subscriber on a topic with a completed digest sends exactly one email, to the confirmed address.

- [ ] **Step 2: Run to verify they fail.**
- [ ] **Step 3: Implement** per Interfaces.
- [ ] **Step 4: Run the full suite.** Expected failures: `RunAllTopicDigestsWorkflow` tests that create topics with no subscribers now fan out to zero topics. Fix each by subscribing a confirmed subscriber with the helpers above (do not weaken the filter). `_make_subscription` in `tests/test_temporal_workflows.py` keeps working (admin-created subscribers are confirmed).
- [ ] **Step 5: Run the full suite** — Expected: all pass.
- [ ] **Step 6: Commit and push** — `git commit -m "fetch and email only topics with confirmed, active subscribers"`

---

### Task 4: Admin token on the existing API

**Files:**
- Create: `app/auth.py`
- Modify: `app/config.py` (`admin_token: str = ""`), `app/routers/topics.py`, `papers.py`, `digests.py`, `subscriptions.py` (router-level `dependencies=[Depends(require_admin)]`), `tests/conftest.py` (set `ADMIN_TOKEN` before settings load; `client` sends the header; new `anon_client` fixture), `docker-compose.yml` (`ADMIN_TOKEN: ${ADMIN_TOKEN:-}` on `api`), `.env.example`, `README.md` (API examples send the header)
- Test: `tests/test_admin_auth.py`

**Interfaces:**
- Produces: `require_admin(x_admin_token: str | None = Header(default=None)) -> None` raising `HTTPException(401)` unless `get_settings().admin_token` (read at call time, not import time) is non-empty and `hmac.compare_digest` matches. `PUBLIC_PATHS = {"/health"}` in `app/auth.py` (Phase 3 adds its public routes here). Conftest sets `os.environ["ADMIN_TOKEN"] = "test-admin-token"` next to `DATABASE_URL`, and exports `ADMIN_HEADERS = {"X-Admin-Token": "test-admin-token"}`; `client` is `TestClient(app, headers=ADMIN_HEADERS)`, `anon_client` is the same without headers.

- [ ] **Step 1: Write the failing tests:**

```python
import re
from uuid import uuid4

def _admin_routes():
    from fastapi.routing import APIRoute
    from app.main import app
    from app.auth import PUBLIC_PATHS
    return [(m, r) for r in app.routes if isinstance(r, APIRoute) and r.path not in PUBLIC_PATHS for m in r.methods]

@pytest.mark.parametrize("headers", [{}, {"X-Admin-Token": "wrong"}, {"X-Admin-Token": ""}])
def test_every_admin_route_rejects_missing_or_wrong_token(anon_client, headers):
    for method, route in _admin_routes():
        url = re.sub(r"\{[^}]+\}", str(uuid4()), route.path)
        resp = anon_client.request(method, url, headers=headers, json={})
        assert resp.status_code == 401, (method, route.path)

def test_unset_token_locks_everything(anon_client, monkeypatch):
    from app.config import get_settings
    monkeypatch.setattr(get_settings(), "admin_token", "")
    assert anon_client.get("/topics", headers={"X-Admin-Token": ""}).status_code == 401

def test_health_is_public(anon_client):
    assert anon_client.get("/health").status_code == 200

def test_no_public_route_exposes_emails_descriptions_or_cadence():
    # For every APIRoute in PUBLIC_PATHS: neither its response_model nor its body
    # model (route.body_field) has a field named "email", "description" or "cadence",
    # checked recursively through nested pydantic models.
```

- [ ] **Step 2: Run to verify they fail.**
- [ ] **Step 3: Implement** `app/auth.py`, wire the routers, update conftest so existing tests send the header.
- [ ] **Step 4: Run the full suite** — Expected: all pass.
- [ ] **Step 5: Commit and push** — `git commit -m "locked the JSON API behind an admin token"`

---

### Task 5: Migrate the live deployment

No code. Exit criterion: "the current deployment migrates with no data loss."

- [ ] **Step 1: Rehearse on a copy.** `pg_dump` the live `digest_db` from the `research-digest-agent-db-1` container into a scratch database `digest_rehearsal` in the same container; run `alembic upgrade head` against it with `DATABASE_URL` pointed there. Expected: 3 topics, 1 subscriber (confirmed), 3 subscriptions all `twice_weekly` with `max_papers` 10/5/5, paper and digest counts identical to live. Drop `digest_rehearsal`.
- [ ] **Step 2: Back up live** with `pg_dump` to a timestamped file outside the repo (scratchpad or `~/`), and note its path.
- [ ] **Step 3: Add `ADMIN_TOKEN`** (32+ random bytes, `python -c "import secrets; print(secrets.token_urlsafe(32))"`) to the local `.env` (gitignored). Never print it in chat.
- [ ] **Step 4: Apply.** Run `alembic upgrade head` in the `api` container, then restart `api` and `worker` (`docker compose up -d --build api worker`). Expected: worker logs "Daily digest schedule: unchanged", `GET /health` 200, `GET /topics` 401 without the header and 200 with it, and `list_active_topic_ids` returns all 3 topics.
- [ ] **Step 5: Record** the migration in the README's incidents/changes section if the project keeps one there; commit and push.

---

## Outcome (2026-11-12)

Done. Commits `9c67f5f`, `1d0524b`, `2972771`, `ab90b4c`, `c951b37` (final-review fixes). 355 tests pass. The live database was migrated to `a1b2c3d4e5f7` at 10:00 PT after the daily run, with a `pg_dump` taken first (`~/research-digest-backups/`); counts preserved, 3 topics active, API returns 401 without `X-Admin-Token`.

**Deviation from Task 5 Step 4:** the worker was stopped before `alembic upgrade head` and started after it, because old worker code can't run on the new schema and new code can't run on the old one.

**Carried forward**
- Phase 3: map typographic apostrophes (`’`, `‘`) to `'` before validating public queries; turn `"empty query"` into a user-facing message; handle concurrent subscriber creation on the signup path (`INSERT … ON CONFLICT` or retry), since `crud.create_subscription` reports every `IntegrityError` as "already subscribed"; re-check eligibility at send time so an unsubscribe between listing and delivery is honoured; decide whether admin re-adding an unsubscribed subscriber clears `unsubscribed_at`; merges don't carry `waitlisted_at`.
- Phase 3: the public-field guard in `tests/test_admin_auth.py` only sees JSON models; add behaviour tests on rendered pages for the "no public email, description or cadence" rule.
- Phase 5 (before any internet exposure): disable or protect `/docs`, `/redoc` and `/openapi.json`.
- Admin-only, parked: a query whose characters expand under `lower()` (e.g. `İ`) can pass the 200-character raw cap and overflow `normalized_query` (500 instead of 422).
- Small test gaps, add when the files are next touched: length/character error messages and `CAT:`-style prefixes in `tests/test_topic_query.py`; a non-ASCII or same-length wrong admin token; a fan-out workflow test for a topic with only an ineligible subscriber.
