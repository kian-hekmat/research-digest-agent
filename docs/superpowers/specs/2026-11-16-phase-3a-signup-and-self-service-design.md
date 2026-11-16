# Phase 3a: Signup, Confirmation and Self-service — Design

Status: for approval, 2026-11-16 (builder-checked). Source: `docs/public-launch-plan.md` v2.2 Phase 3, plus the "Carried forward" notes in `docs/superpowers/plans/2026-11-12-phase-1-multi-user-data-model.md`.

## Why

The service has to run without the owner. A stranger should be able to sign up, confirm their email, manage their topics and unsubscribe, with nothing done by hand through the admin API. It also has to be safe to expose to the internet and stay inside the subscriber, topic and spend caps.

The owner's priority is **launch speed**. Anything that only matters at volume is kept minimal and recorded as post-launch work.

## Scope

Phase 3 is split, the way Phase 2 was:
- **3a (this doc):** secure links, signup, confirmation, manage, unsubscribe, a simple waitlist, abuse controls, and the email headers and footer.
- **3b (later):** the topic preview (`GET /preview`), the refusal of phrases matching more than about 25 papers a day (checked again at signup), and similar-topic suggestions.

## Goals

- **The exit test passes:** signup → confirm → first digest → one-click unsubscribe, with no admin API call.
- **Signup can't be used to find out who's subscribed.** The response is the same whether or not the email exists.
- **Nobody is emailed without confirming,** and nothing spends OpenAI money on an unconfirmed person. `active_subscriptions_query` already enforces this; keep it that way.
- **Unsubscribing works in one click** from the email client (RFC 8058) and from the footer.

## Non-goals (post-launch or other phases)

- **Waitlist automation** (owner's call, 2026-11-16): the daily auto-admit job and the "you're in" email. People are admitted by hand through the admin API.
- **The topic preview and the broad-phrase refusal:** Phase 3b.
- **Page design, landing page and final wording:** Phase 4. Pages here are bare and unstyled.
- **Brevo:** a settings change at launch. Mail keeps going through the current SMTP settings.
- **Trusting `X-Forwarded-For`** behind Caddy, and locking down `/docs`: Phase 5.

## Anti-goals

- A link-scanner that prefetches the confirmation link confirms someone who never clicked it.
- A page or email reveals one subscriber's email, topics or labels to anyone else.
- Something a user typed is reflected unescaped, or a newline in it injects an email header.

## Approaches considered

1. **Plain server-rendered pages now, styled in Phase 4 (owner picked).** The endpoints take ordinary form posts and render bare Jinja templates. CSRF protection, escaping and the "no public emails" rule are built and tested once, on the real pages.
2. **A JSON API first, with pages in Phase 4.** Phase 3 would be quicker, but no-JavaScript pages and mail clients' one-click unsubscribes need form posts anyway. Phase 4 would add a second set of routes and repeat the security tests.
3. **A JavaScript front end.** Dropped: the plan calls for plain HTML that works without JavaScript.

## Design

### Dependencies and settings

- **New dependencies:** `jinja2`, `python-multipart` (form posts) and `itsdangerous` (signed links), pinned in `requirements.txt`. The Docker image is rebuilt.
- **New settings:**
  - `SECRET_KEY`: required. Public routes return 503 when it's empty (fail closed).
  - `PUBLIC_BASE_URL`, used to build links in emails.
  - `TURNSTILE_SITE_KEY` and `TURNSTILE_SECRET_KEY`. Tests use Cloudflare's published test keys, and calls to Cloudflare are faked.
  - Rate limits: `signup_per_ip_per_hour = 5` and `account_emails_per_address_per_day = 3`.

### Signed links (`app/tokens.py`)

Links are signed with `itsdangerous` `URLSafeTimedSerializer(SECRET_KEY)`, with the purpose as the salt.

| Purpose | Carries | Expires | Invalid when |
|---|---|---|---|
| `confirm` | subscriber id, `token_version` | 48 h | version is stale, or the subscriber was deleted |
| `manage` | subscriber id, `token_version` | never | version is stale, or the subscriber was deleted |
| `unsubscribe` | a subscriber id plus a topic id, or `all` | never | the subscriber was deleted |
| `csrf` | a random nonce | 2 h | |

`token_version` is bumped when someone deletes their data (so old links die) and can be bumped by admin. The links must still verify after a `SECRET_KEY` change only if the owner rotates the key deliberately; that isn't built.

### Pages and routes

All forms carry a signed `csrf` hidden field. POSTs whose `Origin` header names another site are rejected (403). The one exception is the one-click unsubscribe POST, which mail clients send with no `Origin` and no cookies. Templates live in `app/templates/` with autoescaping on.

- **`GET /signup`:** a bare form with:
  - email;
  - up to 5 topic rows, each with a phrase, an optional label and papers per email (5–10, default 5);
  - the US-residency checkbox, with links to `/terms` and `/privacy` (placeholders until Phase 6);
  - the Turnstile widget.
- **`POST /signup`:** validates the form, then always renders the same "check your inbox" page, with the same status and about the same timing. Only the form checks run in the request. Everything that depends on the address (looking it up, writing, starting the email workflow) runs afterwards in a FastAPI `BackgroundTask`, so the response can't reveal anything through timing. *(My call, from the launch plan's timing test.)*
  - **Field checks:**
    - the email (`EmailStr`, no CR/LF);
    - 1–5 distinct topics through `validate_public_query`, after mapping `’` and `‘` to `'`, with friendly messages in place of "empty query";
    - labels of at most 200 characters with control characters stripped;
    - papers per email between 5 and 10;
    - the residency box ticked;
    - Turnstile verified;
    - the per-IP rate limit.

    A failure re-renders the form with inline errors (422). Field errors are about the input, never about the address.
  - **Behaviour by state of the address** (case-insensitive, with concurrent signups made safe by `INSERT … ON CONFLICT`):
    - **New:** create an unconfirmed subscriber (with `us_resident_attested_at`) and its subscriptions, then send a confirmation email.
    - **Exists, unconfirmed, or unsubscribed:** replace its subscriptions with the submitted set and send a confirmation email. An unsubscribed person stays unsubscribed until they confirm.
    - **Exists and confirmed:** change nothing and send a manage-link email instead.
    - **Email limit:** emails per address are capped at 3 a day. Beyond that nothing is sent, and the page is the same.
  - **Topics:** each phrase joins the topic with the same normalized query, or creates it (`ON CONFLICT` on `normalized_query`).
- **`GET /confirm?token=`:** shows a page with one "Confirm my subscription" button that POSTs back the token. *(My call: GET doesn't confirm, so link-scanners can't.)*
- **`POST /confirm`:**
  - **Already confirmed and not unsubscribed:** nothing changes, so a repeat doesn't push back their next digest. It just shows the confirmed page.
  - **A link issued before the person's latest unsubscribe:** treated as expired, using the link's signed timestamp. An old email can't quietly re-subscribe someone.
  - **Otherwise:** set `confirmed_at = now`, clear `unsubscribed_at`, set each subscription's `last_sent_at = now`, apply the waitlist (below), and show the confirmed page. That page says plainly what was waitlisted and why, and that digests arrive Monday mornings.

  *(My calls.)*
- **`GET /manage?token=`:** a page with:
  - each topic's label and papers per email, editable. The phrase is read-only: changing it means remove plus add, so the topic cap and the unique constraint are handled in one place. *(My call.)*
  - a remove button per topic;
  - "add a topic" (up to 5 in total);
  - pause and resume all, which sets `Subscription.active`. Pause is the only thing that touches `active`;
  - unsubscribe from everything;
  - "delete my data".

  Each action is its own `POST /manage/...` carrying the manage token plus `csrf`. Adding a topic follows signup's validation and waitlist rules.
- **`POST /manage/delete`:** deletes the subscriber. Subscriptions cascade; topics are never deleted. It shows a goodbye page.
- **`POST /manage-link`** (a form on the "check your inbox" page and on expired-link pages): takes an email, always renders the same page, and sends a manage link only to a confirmed address, within the daily email limit.
- **`GET /unsubscribe?token=`:** a page with an "Unsubscribe" button and, once unsubscribed, "Re-subscribe".
- **`POST /unsubscribe?token=`:** RFC 8058 one-click, with no CSRF field required.
  - A per-topic token **deletes** that subscription row. Pause stays a separate flag, so "resume all" can never bring back a topic the person unsubscribed from. The token carries the subscriber and the topic, not the row id, so re-subscribing can recreate the row. *(My call.)*
  - An `all` token sets the subscriber's `unsubscribed_at`.
  - It's idempotent, returns 200, and doesn't care about the body.
- **`POST /resubscribe?token=`:** reverses the above for the same token, with a `csrf` field. For a topic token it recreates the subscription with the defaults: 5 papers, no label, `last_sent_at = now`. It goes through the waitlist check.
- **Invalid, expired or stale links** render one "this link has expired" page (400) with the manage-link form. It never says why.

### Waitlist (simple; owner's call)

The caps are checked by one shared function, called **at confirmation, when a topic is added, on resume, and on re-subscribe**. Leaving and coming back can't get around a cap. *(My call.)*
- **Subscriber cap:** if confirmed, active subscribers would exceed `max_subscribers`, set `subscriber.waitlisted_at`.
- **Topic cap:** otherwise, for each subscription to a topic with no active subscribers, set `subscription.waitlisted_at` if active topics would exceed `max_active_topics`, or if `spend_state` is soft or hard.
- **Admin:**
  - `GET /admin/waitlist` lists waitlisted subscribers and subscriptions, oldest first.
  - `POST /admin/subscribers/{id}/admit` and `POST /admin/subscriptions/{id}/admit` clear `waitlisted_at`.
  - Admitting doesn't re-check the caps; that's the owner's judgement.
- **Not built:** the auto-admit job and the "you're in" email.

### Account emails (confirmation and manage link)

- **Sending:** a small `SendAccountEmailWorkflow` on the main task queue, started by the API through `get_temporal_client`, as `POST /digests` already does. It uses the existing retry policy and `EmailSender`.
- **Workflow ID:** derived from the subscriber and the email kind plus a time bucket, so a double-submitted form doesn't send two.
- **Content:** plain text plus minimal HTML. It names no topics other than those the person submitted.

### Digest emails

- **Headers:** add `List-Unsubscribe: <{PUBLIC_BASE_URL}/unsubscribe?token=…>`, carrying the subscriber's `all` token, and `List-Unsubscribe-Post: List-Unsubscribe=One-Click`.
- **Footer:**
  - a per-topic "unsubscribe from this topic" link (subscription token);
  - a manage link;
  - the arXiv thank-you;
  - the AI disclaimer.
- **Send-time check (carried forward):** `gather_digest_content` re-checks that each subscription is still active, so an unsubscribe between listing and delivery is honoured.

### Rate limits

A `rate_limit_events(key, created_at)` table, added by migration and indexed on `(key, created_at)`. A limit is "count the rows with this key newer than the window". Old rows are pruned when they're checked. Keys are `signup:ip:<ip>` and `email:<lower(address)>`. Over the limit:
- signup returns a 429 page;
- the email limit silently sends nothing.

The client IP is `request.client.host` until Phase 5 adds the trusted proxy.

### Carried forward from Phase 1, done here

- Typographic apostrophes in public queries.
- A friendly empty-query message.
- Concurrent subscriber creation.
- The send-time eligibility re-check.
- **Admin re-adding an unsubscribed subscriber** leaves `unsubscribed_at` set. Only the person's own confirmation clears it. *(My call.)*
- **The public-field guard** gets behaviour tests on the rendered pages. `PUBLIC_PATHS` lists the new routes.

## Tests (from the launch plan, adapted)

- **Links:** round-trip; reject a tampered signature, an expired link, the wrong purpose, a stale `token_version`, and a deleted subscriber.
- **Flows** (TestClient, real Postgres, fake email sender, fake Turnstile):
  - signup → captured confirmation → GET then POST confirm → active;
  - no residency box → rejected;
  - at the subscriber cap → waitlisted, and admin admit makes them active;
  - a new topic over the topic cap → only that subscription is waitlisted;
  - each manage edit;
  - one-click `POST /unsubscribe` with no cookies or CSRF;
  - delete data removes the subscriber's rows and leaves topics;
  - re-signup after unsubscribe needs confirmation again.
- **Security:**
  - the same body and status for new, unconfirmed and confirmed addresses, and none of them does its address work inside the request (that's checked through the `BackgroundTask` boundary, not with a wall-clock timing test);
  - a confirm link issued before an unsubscribe is rejected, and a repeated confirm leaves `last_sent_at` alone;
  - "resume all" doesn't restore an unsubscribed topic, and resume and re-subscribe both go through the cap check;
  - 429 over the per-IP limit;
  - no account email beyond 3 a day;
  - a POST without a valid `csrf`, or with a cross-site `Origin`, is rejected;
  - a CR/LF in the email or a label can't inject a header;
  - HTML in labels is escaped in pages and emails;
  - no page shows another subscriber's email or labels.
- **Workflow:** a confirmation email that fails to send is retried.
- **Exit test** (time-skipping): signup → confirm → a send delivers a digest with both unsubscribe headers → one-click POST on the header link → the next send skips them.

## Left to the builder

- Module layout (for example `app/routers/public.py`, `app/tokens.py`, `app/ratelimit.py`).
- Template names and copy.
- The exact time bucket for the email workflow ID.
- How the 5 topic rows are encoded in the form.
- Index and pruning details for `rate_limit_events`.
