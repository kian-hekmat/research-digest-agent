# Phase 3a: Signup and Self-service Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A stranger can sign up, confirm, manage, unsubscribe and delete their data through plain server-rendered pages, safely and within the caps, with no admin API call.

**Architecture:**
- **Routes:** a public FastAPI router (`app/routers/public.py`) renders bare Jinja templates and takes form posts.
- **Logic modules:**
  - `app/tokens.py`: signed links;
  - `app/ratelimit.py`: Postgres-backed limits;
  - `app/signup.py`: signup and confirm state changes;
  - `app/waitlist.py`: one cap check.
- **Account emails:** sent by a new Temporal workflow.
- **Digest emails:** gain unsubscribe headers and footer links.

**Tech Stack:** FastAPI, Jinja2, python-multipart, itsdangerous, SQLAlchemy/Alembic (Postgres), Temporal, httpx and respx (Turnstile fake).

**Spec:** `docs/superpowers/specs/2026-11-16-phase-3a-signup-and-self-service-design.md`. Background: `docs/public-launch-plan.md` Phase 3, and the Phase 1 plan's "Carried forward" section.

## Global Constraints

- **Links:** `itsdangerous.URLSafeTimedSerializer(settings.secret_key)` with the purpose as the salt.
  - `confirm` lasts 48 h. `manage` and `unsubscribe` never expire. `csrf` lasts 2 h.
  - `confirm` and `manage` carry `{"s": subscriber_id, "v": token_version}`.
  - `unsubscribe` carries `{"s": subscriber_id, "t": topic_id or None}`, where None means all.
- **Fail closed:** with an empty `secret_key`, every public route returns 503.
- **CSRF:** every public POST needs a valid `csrf` field and no cross-site `Origin`. The exception is `POST /unsubscribe`, which needs neither.
- **No leaking who's signed up:**
  - `POST /signup` and `POST /manage-link` return the same page and status for every address.
  - Address-dependent work runs in a FastAPI `BackgroundTask`.
  - Bad, expired, stale or wrong-purpose links all render `expired.html` with 400, never a 500 and never a reason.
- **Signup validation:**
  - an email (`EmailStr`, no CR/LF);
  - 1–5 distinct topics after `normalize_query`, through `validate_public_query`, with `’` and `‘` mapped to `'` first;
  - labels of at most 200 characters with control characters stripped;
  - papers per email 5–10, default 5 (`public_papers_per_topic_*`);
  - the residency box ticked;
  - Turnstile verified;
  - at most 5 signups per IP per hour.
- **Account emails:** at most 3 per address per day. Over that, nothing is sent and the page doesn't change.
- **Confirmation:**
  - `GET /confirm` never changes state.
  - `POST /confirm` does nothing for an already-active subscriber.
  - A confirm link signed before `unsubscribed_at` counts as expired.
- **Unsubscribing and pausing:**
  - A per-topic unsubscribe deletes the subscription row.
  - An "all" unsubscribe sets `subscriber.unsubscribed_at`.
  - Pause and resume touch only `Subscription.active`.
- **The cap check** is one function, `apply_waitlist`, called at confirm, add-topic, resume and re-subscribe. Admin admit clears `waitlisted_at` without re-checking. There's no auto-admit job and no "you're in" email.
- **Topics are never deleted** by public routes.
- **Git and the live stack:** commit straight to `main`, lowercase past-tense messages, as the owner, with no AI attribution. This adds a migration and a workflow, so the live stack is paused first (Task 0) and deployed last (Task 8).

## Review Focus

1. **The same address written differently** (`" Me@Example.COM "`) must reach the same subscriber as `me@example.com`. Task 4 has the test.
2. **Two topic rows differing only in case or spacing** (`"PINNs"` and `" pinns "`) are a duplicate, rejected inline. Task 4 has the test.
3. **A garbage, expired or wrong-purpose link, or one for a deleted subscriber,** on any link route, renders the expired page with 400, never a 500. Task 6 has the test.
4. **A form submitted twice within seconds** sends one account email. Task 3 has the test.
5. **An old one-click unsubscribe link after "delete my data"** returns 200 and does nothing. Task 6 has the test.

---

### Task 0: Quiesce the live stack

- [ ] Confirm nothing is running: `docker-compose exec -T temporal temporal workflow list --query "ExecutionStatus='Running'"`. Expected: empty.
- [ ] Pause `daily-topic-digests`, `send-prep` and `weekly-digest-emails` with `temporal schedule toggle --pause --reason "phase 3a"`, then `docker-compose stop worker`.
- [ ] Baseline: `venv/bin/python -m pytest -q -p no:cacheprovider`. Record the count.

### Task 1: Dependencies, settings and signed links

**Files:**
- Modify: `requirements.txt` (pin `jinja2`, `python-multipart`, `itsdangerous`; install into `venv`), `app/config.py`
- Create: `app/tokens.py`
- Test: `tests/test_tokens.py`

**Interfaces (produces):**
- **Settings:** `secret_key: str = ""`, `public_base_url: str = "http://localhost:8000"`, `turnstile_site_key: str = ""`, `turnstile_secret_key: str = ""`, `signup_per_ip_per_hour: int = 5`, `account_emails_per_address_per_day: int = 3`.
- `class TokenError(Exception)`
- `make_token(purpose: str, payload: dict) -> str`
- `read_token(purpose: str, token: str, max_age: int | None) -> tuple[dict, datetime]`, which raises `TokenError` and returns the signed time (UTC).
- `confirm_token(sub: models.Subscriber) -> str`, `manage_token(sub) -> str`, `unsubscribe_token(subscriber_id: str, topic_id: str | None) -> str`, `csrf_token() -> str`, `csrf_ok(token: str | None) -> bool`
- `subscriber_for(db, purpose: Literal["confirm", "manage"], token: str) -> tuple[models.Subscriber, datetime]`, which raises `TokenError` for a deleted subscriber or a stale `token_version`.
- `link(path: str, token: str) -> str`, which builds `f"{public_base_url}{path}?token={token}"`.

- [ ] **Step 1: Write the failing tests** (`conftest` sets `SECRET_KEY` for the suite):
  - `test_round_trip_each_purpose`
  - `test_tampered_token_rejected`: flip one character, expect `TokenError`.
  - `test_wrong_purpose_rejected`: a `manage` token read as `confirm`.
  - `test_confirm_expires_after_48h`: issue a token, then monkeypatch `itsdangerous.timed.time.time` forward by 48 h + 1 s. Reading it with `max_age=48*3600` raises `TokenError`, and the same read at +47 h passes.
  - `test_stale_token_version_rejected`: bump `token_version` after issuing.
  - `test_deleted_subscriber_rejected`
  - `test_csrf_ok_only_for_fresh_signed_tokens`: `None`, `"junk"` and a `manage` token all give False.
- [ ] **Step 2: Run them and see them fail:** `venv/bin/python -m pytest tests/test_tokens.py -q -p no:cacheprovider`. Expected: ImportError.
- [ ] **Step 3: Implement** the settings, the pins and `app/tokens.py` with the interfaces above.
- [ ] **Step 4: Run them and see them pass,** then run the full suite (green).
- [ ] **Step 5: Commit:** "added signed links for confirm, manage, unsubscribe and csrf, and the phase 3 settings".

### Task 2: Rate limits

**Files:**
- Create: `alembic/versions/f0a1b2c3d4e5_rate_limit_events.py` (`down_revision = "e5f6a7b8c9db"`), `app/ratelimit.py`
- Modify: `app/models.py` (`RateLimitEvent`)
- Test: `tests/test_ratelimit.py`

**Interfaces (produces):**
- **Table:** `rate_limit_events(id bigint identity, key varchar(320) not null, created_at timestamptz not null default now())`, indexed on `(key, created_at)`.
- `allow(db, key: str, limit: int, window: timedelta, now: datetime) -> bool`. It deletes this key's rows older than `now - window`, counts the rest, inserts a row and returns True only when the count is under `limit`, then commits.
- **Key helpers:** `signup_ip_key(ip: str) -> str` gives `"signup:ip:<ip>"`, and `email_key(address: str) -> str` gives `"email:<lowercased stripped address>"`.

- [ ] **Step 1: Write the failing tests:**
  - `test_allows_up_to_limit_then_refuses`: 5 Trues, then False, at one `now`.
  - `test_window_slides`: refused at t, allowed again at t + window + 1s.
  - `test_keys_are_independent`
  - `test_old_rows_are_pruned`: after a call at t + 2 windows, only one row is left for the key.
  - `test_email_key_normalizes`: `email_key(" Me@Example.COM ") == "email:me@example.com"`.
- [ ] **Step 2: Run them and see them fail.**
- [ ] **Step 3: Implement** the migration, the model and the module.
- [ ] **Step 4: Run them and see them pass,** then the full suite (the migration drift check included).
- [ ] **Step 5: Commit:** "added postgres-backed rate limits".

### Task 3: Account emails (workflow)

**Files:**
- Modify: `app/temporal/types.py` (`AccountEmailInput`, stdlib only), `app/temporal/activities.py`, `app/temporal/workflows.py`, `app/worker.py` (register both)
- Create: `app/account_emails.py`
- Test: `tests/test_account_emails.py`

**Interfaces (produces):**
- `@dataclass AccountEmailInput: kind: str; to: str; url: str`, where `kind` is `"confirm"` or `"manage"`.
- `render_account_email(kind: str, url: str) -> DigestEmailContent`. The subjects are "Confirm your research digest subscription" and "Your research digest manage link". The bodies are plain text plus minimal HTML with the URL escaped.
- Activity `send_account_email(input: AccountEmailInput) -> None`, which renders and calls `EmailSender().send`.
- `SendAccountEmailWorkflow.run(input: AccountEmailInput) -> None`, which runs the activity with `_EMAIL_RETRY` and a 60 s start-to-close timeout.
- `async start_account_email(client, kind: str, subscriber_id: str, to: str, url: str, now: datetime) -> None`. The workflow ID is `f"account-{kind}-{subscriber_id}-{now:%Y%m%dT%H}{now.minute // 10}"`. A `WorkflowAlreadyStartedError` is swallowed.

- [ ] **Step 1: Write the failing tests:**
  - `test_render_escapes_url_and_names_kind`
  - `test_workflow_retries_a_failed_send`: time-skipping, with the activity's sender failing once and then succeeding. The fake records one delivered email and two attempts.
  - `test_same_bucket_starts_once`: two `start_account_email` calls in one 10-minute bucket give one start on the `FakeTemporalClient`. Extend the fake to raise `WorkflowAlreadyStartedError` on a duplicate ID if it doesn't already.
- [ ] **Step 2: Run them and see them fail.**
- [ ] **Step 3: Implement** with the interfaces above. `types.py` stays stdlib-only, with no `from __future__`.
- [ ] **Step 4: Run them and see them pass,** then the full suite.
- [ ] **Step 5: Commit:** "added the account email workflow for confirmation and manage links".

### Task 4: Web scaffolding and signup

**Files:**
- Create: `app/web.py`, `app/signup.py`, `app/routers/public.py`, `app/templates/{base,signup,check_inbox,expired,rate_limited}.html`
- Modify: `app/main.py` (include the router), `app/auth.py` (`PUBLIC_PATHS` adds `/signup`, `/manage-link`, `/confirm`, `/manage`, `/manage/*`, `/unsubscribe`, `/resubscribe`, `/terms`, `/privacy`), `app/crud.py` (an `ON CONFLICT` subscriber upsert)
- Test: `tests/test_public_signup.py`

**Interfaces (produces):**
- `app/web.py`:
  - `templates` (Jinja2Templates, autoescape on);
  - `render(request, name: str, status: int = 200, **ctx) -> HTMLResponse`, which always passes `csrf=csrf_token()` and `site_key`;
  - `require_secret()`, a dependency that returns 503 when `secret_key` is empty;
  - `check_form(request, csrf: str | None)`, which raises 403 on a bad `csrf` or a cross-site `Origin`;
  - `verify_turnstile(token: str | None, ip: str) -> bool`, an httpx POST to `https://challenges.cloudflare.com/turnstile/v0/siteverify`. It returns False when the secret is empty, on error, or when Cloudflare doesn't reply `success`.
- `app/signup.py`:
  - `@dataclass SignupTopic: query: str; label: str | None; max_papers: int`
  - `parse_signup(form: dict) -> tuple[str | None, list[SignupTopic], dict[str, str]]`, giving the normalized email, the topics and the field errors. The topic rows are form fields `topic_{i}`, `label_{i}` and `papers_{i}` for i in 0..4, plus `email` and `us_resident`.
  - `record_signup(db, email: str, topics: list[SignupTopic], now: datetime) -> tuple[str | None, models.Subscriber | None]`, returning `("confirm" | "manage" | None, subscriber)`:
    - new address: create;
    - unconfirmed or unsubscribed: replace its subscriptions;
    - confirmed: change nothing and return `"manage"`.

    Topics are joined or created with `ON CONFLICT` on `normalized_query`. It sets `us_resident_attested_at`, and commits.
- **Routes:**
  - `GET /signup`
  - `POST /signup`: validate, then the BackgroundTask runs `record_signup`, the `email_key` limit, and `start_account_email` with `confirm_token` or `manage_token`. It always renders `check_inbox.html` (200).
  - `POST /manage-link`: same pattern, sending only to confirmed addresses.
  - `GET /terms` and `GET /privacy`: placeholder pages.

- [ ] **Step 1: Write the failing tests.** Use the `anon_client` fixture, the respx-faked Turnstile, the `FakeTemporalClient`, and a fixture `form(**overrides)` that builds a valid signup form with a fresh `csrf`. The signup flow:
  - `test_signup_page_renders_form_with_csrf_and_site_key`
  - `test_new_signup_creates_pending_subscriber_and_starts_confirm_email`: the subscriber is unconfirmed, the subscriptions have papers 5 and the given label, and a workflow started with `kind="confirm"`.
  - `test_response_identical_for_new_pending_and_confirmed_addresses`: the same status and the same body once the csrf value is removed.
  - `test_confirmed_address_gets_manage_email_and_no_changes`
  - `test_pending_signup_replaces_its_topics`
  - `test_email_case_and_space_variants_hit_one_subscriber` (Review Focus 1)

  Validation and abuse controls:
  - `test_missing_residency_box_rejected_inline_422`
  - `test_duplicate_topics_after_normalizing_rejected` (Review Focus 2)
  - `test_typographic_apostrophe_accepted`: `"Pólya’s urn"` is valid.
  - `test_empty_topic_message_is_friendly`: no `"empty query"` text.
  - `test_papers_outside_5_10_rejected`
  - `test_label_html_escaped_and_control_chars_stripped`
  - `test_crlf_in_email_rejected`
  - `test_turnstile_failure_rejected`
  - `test_sixth_signup_from_one_ip_in_an_hour_gets_429`
  - `test_fourth_account_email_in_a_day_not_sent_page_unchanged`
  - `test_missing_or_bad_csrf_403`, `test_cross_site_origin_403`
  - `test_503_when_secret_key_empty`
  - `test_no_public_page_shows_other_subscribers_email_or_labels`: render signup and check-inbox with two subscribers in the DB, and neither page contains either email.
- [ ] **Step 2: Run them and see them fail.**
- [ ] **Step 3: Implement** with the interfaces above.
- [ ] **Step 4: Run them and see them pass,** then the full suite, including `tests/test_admin_auth.py`'s public-path guard.
- [ ] **Step 5: Commit:** "added public signup: form, validation, turnstile, rate limits and enumeration-safe responses".

### Task 5: Confirmation, the waitlist and admin admit

**Files:**
- Create: `app/waitlist.py`, `app/templates/{confirm,confirmed}.html`
- Modify: `app/signup.py` (`confirm_subscriber`), `app/routers/public.py`, `app/routers/admin.py`, `app/schemas.py`
- Test: `tests/test_public_confirm.py`, `tests/test_waitlist.py`

**Interfaces (produces):**
- `apply_waitlist(db, subscriber, subscriptions: list[models.Subscription], now: datetime) -> list[str]` (does not commit). It returns a plain reason per hold:
  - **Subscriber cap:** sets `subscriber.waitlisted_at` if confirmed, not-unsubscribed, not-waitlisted subscribers other than this one number `max_subscribers` or more.
  - **Topic cap:** otherwise, for each subscription whose topic has no active subscription from anyone else, sets `subscription.waitlisted_at` if distinct active topics number `max_active_topics` or more, or if `spend_state(db, now)` isn't `"ok"`.
- `confirm_subscriber(db, sub, signed_at: datetime, now: datetime) -> list[str] | None`:
  - None when the link predates `unsubscribed_at` (the route renders expired);
  - `[]` and no change when already active;
  - otherwise it sets `confirmed_at`, clears `unsubscribed_at`, sets every subscription's `last_sent_at = now`, and returns `apply_waitlist`'s reasons. It commits.
- **Routes:** `GET /confirm?token=` renders the button. `POST /confirm` (token and `csrf`) renders `confirmed.html` with the reasons and "Digests arrive Monday mornings".
- **Admin (admin token):**
  - `GET /admin/waitlist` gives `{subscribers: [...], subscriptions: [...]}`, oldest first;
  - `POST /admin/subscribers/{id}/admit` and `POST /admin/subscriptions/{id}/admit` clear `waitlisted_at` and give 204, or 404 for an unknown ID.

- [ ] **Step 1: Write the failing tests:**
  - `test_get_confirm_changes_nothing`
  - `test_post_confirm_activates_and_sets_last_sent`
  - `test_repeat_confirm_keeps_last_sent_at`
  - `test_confirm_link_from_before_unsubscribe_is_expired`
  - `test_at_subscriber_cap_confirm_waitlists_and_page_says_so`
  - `test_new_topic_over_topic_cap_waitlists_only_that_subscription`
  - `test_new_topic_at_soft_spend_waitlisted_existing_topic_not`
  - `test_admin_admit_makes_active`
  - `test_waitlisted_subscriber_not_in_active_query`
  - `test_admin_routes_need_admin_token`
- [ ] **Step 2: Run them and see them fail.**
- [ ] **Step 3: Implement** with the interfaces above.
- [ ] **Step 4: Run them and see them pass,** then the full suite.
- [ ] **Step 5: Commit:** "added confirmation, the cap check with a hand-admitted waitlist, and admin admit".

### Task 6: Manage, unsubscribe, re-subscribe and delete

**Files:**
- Create: `app/templates/{manage,unsubscribe,unsubscribed,goodbye}.html`
- Modify: `app/routers/public.py`, `app/signup.py`
- Test: `tests/test_public_manage.py`

**Interfaces (produces):** routes, all keyed by `token` plus `csrf` unless noted.
- **Manage:**
  - `GET /manage`
  - `POST /manage/update`: per topic, label and papers 5–10.
  - `POST /manage/remove` (`topic_id`): deletes that row.
  - `POST /manage/add` (one topic row): validates like signup, to at most 5 subscriptions in total, then `apply_waitlist`.
  - `POST /manage/pause` and `/manage/resume`: set `active` on all of them. Resume calls `apply_waitlist`.
  - `POST /manage/unsubscribe-all`: sets `unsubscribed_at`.
  - `POST /manage/delete`: deletes the subscriber and bumps nothing else; topics stay.
- **Unsubscribe:**
  - `GET /unsubscribe`: a button page.
  - `POST /unsubscribe`: no csrf and any body. With a topic, it deletes the row. With all, it sets `unsubscribed_at`. It's 200 and idempotent, and a missing target is a no-op.
  - `POST /resubscribe` (csrf):
    - a topic token recreates the row with 5 papers, no label and `last_sent_at = now`;
    - all clears `unsubscribed_at`;
    - then `apply_waitlist`.

- [ ] **Step 1: Write the failing tests:**
  - `test_manage_page_lists_only_this_subscribers_topics`
  - `test_update_label_and_papers`, `test_update_rejects_papers_out_of_range`
  - `test_remove_topic_leaves_topic_row`
  - `test_add_topic_validates_and_caps_at_five`, `test_add_new_topic_over_cap_waitlisted`
  - `test_pause_then_resume`, `test_resume_rechecks_caps`
  - `test_one_click_unsubscribe_topic_without_cookies_or_csrf`
  - `test_one_click_unsubscribe_all`
  - `test_resume_does_not_restore_unsubscribed_topic`
  - `test_resubscribe_topic_recreates_with_defaults_and_rechecks_caps`
  - `test_delete_removes_subscriber_rows_keeps_topics`
  - `test_old_unsubscribe_link_after_delete_is_200_noop` (Review Focus 5)
  - `test_bad_links_render_expired_400_on_every_route`: parametrize over every GET and POST link route with `"junk"`, a wrong-purpose token, an expired confirm token, and a deleted subscriber's token (Review Focus 3).
  - `test_resignup_after_unsubscribe_needs_confirmation`
- [ ] **Step 2: Run them and see them fail.**
- [ ] **Step 3: Implement** with the interfaces above.
- [ ] **Step 4: Run them and see them pass,** then the full suite.
- [ ] **Step 5: Commit:** "added the manage page, one-click unsubscribe, re-subscribe and delete my data".

### Task 7: Digest email headers, footer, send-time re-check and the exit test

**Files:**
- Modify:
  - `app/temporal/types.py` (`DigestEmailContent.headers: dict[str, str] = field(default_factory=dict)`, stdlib);
  - `app/services/email.py` (`EmailSender.send` sets `content.headers`; `TopicSection.topic_id: str | None = None`; `render_digest_email(..., links: EmailLinks | None = None)`);
  - `app/temporal/activities.py` (`gather_digest_content` builds the links and re-checks eligibility).
- Test: `tests/test_digest_unsubscribe.py`, `tests/test_public_e2e.py`

**Interfaces (produces):**
- `@dataclass EmailLinks: manage_url: str; unsubscribe_all_url: str; topic_unsubscribe_urls: dict[str, str]`, keyed by topic ID.
- **When `links` is given:**
  - Headers are `{"List-Unsubscribe": f"<{unsubscribe_all_url}>", "List-Unsubscribe-Post": "List-Unsubscribe=One-Click"}`.
  - Each section's text and HTML end with an "Unsubscribe from <topic>" link.
  - The footer gets a "Manage your topics" link, "Thank you to arXiv for use of its open access interoperability.", and "Summaries are written by AI and can be wrong; read the paper before relying on them."
- **In `gather_digest_content`:**
  - Subscriptions no longer in `active_subscriptions_query` are dropped.
  - The links come from `tokens` and `settings.public_base_url`.

- [ ] **Step 1: Write the failing tests:**
  - `test_headers_and_footer_links_present_when_links_given`
  - `test_no_links_no_headers` (existing callers are unchanged)
  - `test_sender_sets_headers_on_the_message`
  - `test_label_and_topic_html_escaped_in_footer`
  - `test_unsubscribed_between_listing_and_delivery_is_skipped` (time-skipping, using `temporal_client`)
  - `test_signup_to_digest_to_unsubscribe_exit` (time-skipping):
    1. POST signup;
    2. take the confirm URL from the captured workflow input;
    3. GET, then POST confirm;
    4. seed a completed digest with papers for the topic, and run prep and send with `slot_iso`;
    5. the captured digest carries both headers;
    6. POST its `List-Unsubscribe` URL with body `List-Unsubscribe=One-Click`;
    7. the next slot's send delivers nothing to this address.
- [ ] **Step 2: Run them and see them fail.**
- [ ] **Step 3: Implement** with the interfaces above.
- [ ] **Step 4: Run them and see them pass,** then the full suite.
- [ ] **Step 5: Commit:** "added unsubscribe headers and footer links to digests, re-checked eligibility at send, and the signup-to-unsubscribe exit test".

### Task 8: Deploy and record

- [ ] **Step 1:** Generate `SECRET_KEY` (`python -c "import secrets;print(secrets.token_urlsafe(48))"`) and append it to `.env` without printing it. Set `PUBLIC_BASE_URL=http://localhost:8000` for now. Leave the Turnstile keys empty: public signup fails closed until the owner adds the real keys.
- [ ] **Step 2:** Back up: `pg_dump` to `~/research-digest-backups/digest_db_<ts>_pre-3a.sql`. Then `docker-compose run --rm worker alembic upgrade head` and `docker-compose up -d --build api worker`. Expected: the migration lands at `f0a1b2c3d4e5`, and the worker starts with the new workflow registered.
- [ ] **Step 3:** Smoke-test: `GET /signup` is 200 and `GET /health` is ok. Unpause the three schedules.
- [ ] **Step 4:** Docs:
  - mark Phase 3a done in `docs/public-launch-plan.md`;
  - add the public routes and new settings to `CLAUDE.md`;
  - write this plan's Outcome.

  Commit and push: "deployed phase 3a and recorded it".

---

## Outcome (2026-11-16)

Built inline (executing-plans), with one whole-change security review on the most capable model. Commits `08217ed` (links), `2477bab` (rate limits), `20546e5` (account emails), `d07be47` (signup), `91add38` (confirm and waitlist), `83bacf7` (manage, unsubscribe, delete), `34fef2c` (digest headers and the exit test), `99d83f3` (review fixes). 553 → 641 tests, and the signup-to-unsubscribe exit test passes.

**Live since 2026-11-16 17:23 ET:**
- **Database:** backed up to `~/research-digest-backups/digest_db_20261116_142300_pre-3a.sql`, then migrated to `f0a1b2c3d4e5`.
- **Settings:** `SECRET_KEY` was generated into `.env` and never printed, with `PUBLIC_BASE_URL=http://localhost:8000` for now.
- **Images:** rebuilt with `jinja2`, `python-multipart` and `itsdangerous`.
- **Schedules:** paused 16:50–17:23 ET, so no runs were skipped.
- **Smoke test:** `/signup` returns 200 with its form, a junk manage link gives 400, and admin without a token gives 401.
- **Owner to do:**
  - add the real Turnstile keys; signup refuses everyone until then;
  - set `PUBLIC_BASE_URL` to the real domain at deploy. Until then, the links in the owner's own digests point at `localhost:8000`.

**Rulings:**
- Worked on `main`.
- `start_account_email` names its workflow rather than importing it, to avoid an import cycle.
- Manage POSTs share one route, `/manage/{action}`.
- Re-subscribing refuses beyond 5 topics, and for a topic that no longer exists.
- Links and `List-Unsubscribe` are added only when `SECRET_KEY` is set.
- The exit test backdates `last_sent_at` to stand in for waiting until Monday.
- The final review ran before the deploy, so its fixes shipped in the same deploy.

**Review fixes** (each test failed first, then passed; full suite 641 passed):
- **One account email per 10-minute bucket on the real server** (`REJECT_DUPLICATE`). The fake had hidden this.
- **Async Turnstile,** with the per-IP limit counted before it. Background database work now runs in a thread.
- **A per-IP limit on `/manage-link`.**
- **Re-subscribing works only from a one-hour link** issued on the unsubscribed page, so an old digest link can't undo an opt-out.
- **Rate-limit keys are HMAC'd, and rows older than 2 days are pruned for every key.** "Delete my data" leaves no plaintext address behind.

**Deferred minors (from the review):**
- A re-signup doesn't bump `token_version`, and the confirm page doesn't list the topics.
- Background-task errors are only logged by the server.
- A concurrent double-submit for one pending address can raise in one background task.
- Add-topic or resume can waitlist an already-active subscriber when the count is already over the cap.
- Count-then-insert races can overshoot a cap by one.
- CSRF failures render JSON, not a page.
- A multipart file part in a form gives a 500.
- There's no `Referrer-Policy` or `Cache-Control: no-store` on pages carrying tokens.
- Live manage URLs stay in Temporal history.
