# Frontend redesign plan

Decided with the owner on 2026-11-17 in an interview. Ships **before invites** (launch section 4): the smoke test runs on the redesigned signup.

## Goals

- Replace the fixed five-topic form with one topic row and an "Add another topic" button.
- Remove the unused topic label.
- A modern, clean, editorial look across every public page.
- Leave room in the layout for a topic preview, without building it yet.

## Decisions

| # | Area | Decision |
|---|---|---|
| 1 | Stack | Keep the server-rendered Jinja templates. Move the inline stylesheet into `app/static/site.css`, add one small `app/static/site.js` and mount `/static` in FastAPI. No build step, no framework. |
| 2 | Scope | All public pages: signup, manage, confirm, check inbox, confirmed, expired, unsubscribe, unsubscribed, resubscribed, goodbye, rate limited, terms, privacy. The digest email is out of scope. |
| 3 | Style | Editorial and academic: serif display headings, a warm cream background, thin rules, generous whitespace. Accent: ink blue. |
| 4 | Fonts | Google Fonts: Source Serif 4 for headings and body text, Inter for form controls and small UI text. The privacy page gains a line that pages load fonts from Google Fonts. |
| 5 | Theme | Follows the OS light or dark setting, as today. No toggle. Dark mode keeps the editorial feel: a warm near-black background and a lighter ink blue. |
| 6 | Topic entry | One topic card to start. "+ Add another topic" adds cards up to 5, with a "1 of 5" counter. Each card has a remove (×) button, except when it's the only card. |
| 7 | No-JS | The server still renders 5 rows. JS hides the empty ones (keeping any row that has a value or an error) and shows the add button. With JS off, all 5 rows show. The form field names (`topic_i`, `papers_i`) and the server-side parsing don't change. |
| 8 | Papers per email | A per-topic slider from **1 to 10** (was 5 to 10), step 1, default 5, with the value shown next to it (`<input type="range">` plus an `<output>`). It submits as `papers_i`, as today. Server side, only `public_papers_per_topic_min` in `app/config.py` changes from 5 to 1; `parse_papers` already reads it. The edition's K keeps its floor of 5 (`_MIN_K`), so prep cost doesn't change: a reader who picks 1 to 4 gets the top N of a 5-paper edition. The week's overview is shared by the edition, so it can mention papers beyond a small cap. |
| 9 | Label | Removed everywhere: signup, manage, `app/signup.py` (`parse_label`, `MAX_LABEL`, the `label` fields), `app/routers/public.py`, the model, and the tests. A migration drops `subscriptions.label`; back up before running it in production. |
| 10 | Landing (signup) | A hero headline and tagline, a three-step "How it works" (pick topics → we read and rank every new paper → a Monday email), then the form, then "Already subscribed?". No sample digest and no FAQ. |
| 11 | Manage page | Each topic is a card with its phrase, slider, a Save button and a Remove button. An "Add topic" card at the end works like signup. Account actions (pause, unsubscribe, delete my data) are grouped at the bottom, with delete visually separated as the dangerous one. |
| 12 | Topic preview | **Placeholder only.** Each topic card reserves a spot under the phrase for a future preview (an empty, hidden element with a stable class such as `.topic-preview`), so adding it later needs no layout change. What the preview shows was left open; the options were matching papers from our database (cheap, fast), a live arXiv search (real coverage, slow behind the 3 s throttle), or both with a breadth warning. |

## Page structure

**Shared (`base.html`).** A slim header with the "Papers Weekly" wordmark (links to `/signup`), a centered content column of about 40rem (wider on the landing hero), and the existing footer (terms, privacy, manage link). The stylesheet uses CSS custom properties for color, type scale and spacing, defined once for light and once for dark.

**Signup.**

```
Papers Weekly                                    (header)
──────────────────────────────────────────────
The new arXiv papers worth your time,
every Monday.                                   (serif hero)
A short weekly email for the topics you follow. Free.

  1 Pick topics    2 We read & rank    3 Monday email

Email  [ you@university.edu        ]

Your topics
┌──────────────────────────────────────────┐
│ Phrase [ reward modeling            ] [×]│
│ (preview placeholder, hidden)            │
│ Papers per email  ──●──────────  7       │
└──────────────────────────────────────────┘
[ + Add another topic ]              1 of 5

[✓] I live in the US and am 13 or older ... terms, privacy
[ Turnstile ]
[ Sign up ]

Already subscribed?  [ email ] [ Send me a link ]
```

**Status pages** (check inbox, confirmed, expired, and so on): one centered card with a heading, a sentence or two and, where there is one, a single primary button.

**Terms and privacy:** a readable long-form text style with serif body text and comfortable line length.

## JavaScript (`app/static/site.js`)

- Topic rows: on load, hide the empty rows beyond the first, unless a row has a value or an error. "Add" reveals the next hidden row and focuses its phrase input. "×" clears the row's inputs and hides it. Update the counter and disable "Add" at 5.
- Sliders: keep the `<output>` in sync with the range input.
- Nothing else. No library, no fetch calls yet.

## Unchanged

- Routes, CSRF, rate limits, Turnstile, signed links, the waitlist and "no leaking who's signed up".
- The maximum of 5 topics, and the maximum of 10 papers per email.
- The digest email's HTML.

## Tests (test-driven, written first)

- Signup renders the five `topic_i` / `papers_i` inputs, the add button and the counter, and no `label_` inputs.
- `papers_i` renders as a range input with min 1, max 10 and the submitted value kept on a re-render after an error.
- Signup and the manage add and update routes accept 1 and reject 0 and 11; a subscription capped at 1 gets exactly one paper in its email.
- A signup with topics only in rows 0 and 2 still succeeds (gaps are allowed, since hidden rows submit empty).
- Label: `parse_label` is gone, the manage add and update routes ignore a `label` field, and the migration drops the column. The migrations up/down round trip in `db_session` covers drift.
- `/static/site.css` and `/static/site.js` are served and pass `app.auth` (add `/static` to the public paths).
- The privacy page mentions Google Fonts.
- Every public page extends `base.html` and links the stylesheet.
- Update the existing tests that post or assert `label_i` / `label` (`tests/test_public_signup.py`, `tests/test_public_manage.py`, `tests/test_launch_pages.py`, and any others that reference the field), and report each change.

## Deploy

1. Run the full suite locally, then commit and push to `main`.
2. On the server: confirm no workflows are running, pause the three schedules and stop the worker (the model changes).
3. `scripts/backup.sh && $C build api worker && $C run --rm worker alembic upgrade head && $C up -d`, then unpause the schedules.
4. Check the pages on https://papersweekly.com in light and dark mode and at phone width, then run the launch section 4 signup smoke test.

## Later (not in this build)

- The topic preview itself (decision 12).
- Restyling the digest email to match.
