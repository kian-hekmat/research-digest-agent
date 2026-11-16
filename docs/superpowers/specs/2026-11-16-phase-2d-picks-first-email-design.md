# Phase 2d: Picks-first Email and Prompts — Design

Status: for approval, 2026-11-16. Source: `docs/public-launch-plan.md` v2.2, §"Who it's for", §3.5 step 3, §3.9, and Phase 2's "The new email" item, tests and eval gates.

## Why

Every email has to do three things for a reader with a real tie to a field (launch plan, "Who it's for"):
1. Show where the topic is going. The overview is the heart of the email.
2. Sell the one or two papers worth reading.
3. Leave the reader feeling they learned something.

The anti-goal is an email that is correct but forgettable: generic overviews, no paper stands out, and the reader quietly stops opening it.

Today's email can't do this. Summaries are neutral 3–4 sentence paragraphs (`_PER_PAPER_SYSTEM`, and the combined `summarize_and_rate` prompt in prep). The overview is plain text over the top K's summaries, with no picks and no citations. Papers are laid out by day.

## Goals

- **Picks-first sections** in the order §3.9 sets: "Start here" picks, then "The period in brief", then "Also new".
- **Rewritten prompts** that sell (concrete, vivid, specific about why a paper might matter) without claiming more than the abstract supports.
- **Picks and citations come back as arXiv IDs** in structured output. An ID that wasn't in the model's input is dropped.
- **Your hand review passes before launch.** The gates are listed under Round 1.

## Non-goals

- Per-topic blurbs (decided: shared, below).
- Full text (stays off until arXiv allows it).
- Manage and unsubscribe links (Phase 3; the footer leaves room for them).
- Changing ranking, ratings or K.
- A bulk rewrite of existing summaries.

## Anti-goals

- **Overclaiming.** A hook or overview line that says more than the abstract supports. This is worse than a dull line.
- **A citation that doesn't support its claim.** A finding linked to a paper that doesn't say it.
- **Formulaic openers** ("The paper addresses…", "This work proposes…").
- **Overviews that repeat last week's general points.** The `avoid_repeats` history already guards this (rag-plan §5.6), and it must keep working.

## Constraints

- **Model:** `gpt-5.4-mini` over the existing `/chat/completions` path, with abstracts only.
- **Spend:** every call goes through `Summarizer._complete` and `DbLedger`. The new call is billed under purpose `overview`, and blurbs under `summary`. No new purposes.
- **Spend guard:** at 100% these calls return None. The email then goes out without picks or an overview (the existing fallback), never held back.
- **Cost:**
  - Blurb: about 500 in / 120 out tokens, about $0.001.
  - Overview-and-picks: up to 25 abstracts (about 9k in) plus history, about 350 out, about $0.008 per topic per send.
  - At weekly sends that's about $0.035 a month per topic for the overview, against the $50 cap.
- **Picks come from the edition's top 5.** Every reader gets at least 5 papers, so every reader has both picks.
- **Every subscriber of a topic gets the same picks and overview.**
- **Prompt changes are gated by evals**, recorded in `docs/rag-plan.md` (CLAUDE.md).

## Approaches considered

1. **Build order (owner picked "prompts first").**
   - **Round 1:** the prompts plus an eval script that produces your review files.
   - **Round 2,** after the review passes: storage, the template, prep wiring, deploy.
   - **The alternative** was to build everything at once and judge it on live Monday emails. That shows the email sooner, but it risks reworking storage and the template if the review changes what the prompts return, and your own emails would carry unreviewed text meanwhile.
2. **Blurb sharing (owner picked "shared").** One blurb per paper, stored on the paper and reused by every topic, as summaries are today. The topic-specific angle comes from the picks and the overview, which are per topic. Per-topic blurbs would move summaries into a paper×topic table and multiply the cost.
3. **One overview-and-picks call, not two.** As §3.5 specifies. Two calls would read the same 25 abstracts twice.

## Round 1: prompts and the eval script

### Paper blurb (replaces the summary prompt for prep)

- **Input:** title and abstract. No topic and no rating: the blurb is shared across topics, and rating already happens at ingest.
- **Output** (structured JSON): `hook` and `what`.
  - `hook`: one line on why it's worth reading, at most about 20 words.
  - `what`: one or two concrete sentences on what the paper did and found, with numbers where the abstract has them.
- **Prompt rules:**
  - Sell through specifics, not adjectives.
  - Never state more than the abstract supports.
  - No "The paper addresses / This work proposes" openers.
  - No LaTeX, markdown or preamble.
  - Don't mention the reader's topic.
- **The related-work note** (`RAG_RELATED_NOTES`) keeps its current mechanism and citation check. Whether it rides in the blurb call or stays in its own path is left to the plan; its behaviour must not change.

### Overview and picks (one call per topic per edition)

- **Input:**
  - the topic name;
  - up to 25 of the window's highest-ranked papers, each with its arXiv ID, rank, title and abstract;
  - which IDs are the top 5;
  - the topic's previous overviews, in the `avoid_repeats` style (seen only so as not to repeat them; never mentioned).
- **Output** (structured JSON):
  - `overview`: 80–120 words of concrete findings. Whenever it states a specific paper's finding, it cites the paper inline as `[arXiv ID]`. There's no required number of citations.
  - `picks`: 0–2 items, each `{arxiv_id, hook}`. Each comes from the top 5 and has a one-line reason to start with it. A pick needn't be #1.
- **Validation** (code, not prompt):
  - A cited ID not in the input is removed from the text.
  - A pick not in the top 5, a duplicate, or an unknown ID is dropped.
  - Invalid or unparseable JSON means None, treated like a failed overview.

### Structured output

Use OpenAI's `response_format` with a strict `json_schema` if `gpt-5.4-mini` accepts it on `/chat/completions`. Otherwise use JSON mode plus the same validation. Check this with a single probe call (fractions of a cent) as the plan's first step. The Ollama backend is legacy: the new calls need `SUMMARY_BACKEND=openai`, and with any other backend they return None.

### Eval script (`evals/`, a new module)

- **Overview windows:** rebuilt Monday-to-Monday weekly windows from the completed daily digests, so the review sees what production will (a week of papers, up to 25 abstracts, a top 5).
  - Each window is re-ranked with today's ranking (`_rank_window`, from stored ratings and h-indices; missing signals take the batch median, as in production).
  - It's given the `avoid_repeats` history of the windows before it.
  - Only windows with at least 5 papers count. The live database has about 17 (Numerical Analysis 7, PINNs 9, RLHF 1, checked 2026-11-16).
  - **Sample size (owner's call, 2026-11-16): use all of them, about 17, without padding.** Mostly Numerical Analysis and PINNs, so topic variety is thin.
- **Overviews are judged on their own, not paired with old ones.** The old prompt can't produce picks or citations, and the launch plan sets the review as about 25 overviews plus about 20 blind summary pairs, about an hour.
- **Blurb pairs:** about 20 blind pairs, the current prep summary vs the new blurb, for papers drawn from those windows' top 5s.
  - The current prep summary is `summarize_and_rate` re-run now on `gpt-5.4-mini` with the paper's topic, keeping only its paragraph. *(My call: the stored summaries mix in older models, which would flatter the new prompt.)*
  - The blurb is rendered as hook plus `what`.
  - Pairs are in a seeded random order, in the existing `_blind.json` format, with the key in the report file.
- **Automatic checks in the report:**
  - overview word counts;
  - citations dropped by validation;
  - picks outside the top 5 (should be 0 after validation; counted before it);
  - numbers that appear in an overview or blurb but not in its input (the existing `evals.metrics` check).
- **Read-only** against the database. Spend goes through the ledger with no topic. Estimated well under $1.

### Gates (you judge, by hand, about an hour)

- Overview factual errors: at most 1 across the roughly 17 windows (the launch plan's "1 in 25", applied to the smaller sample).
- Every citation points to a paper whose abstract supports the claim.
- New blurbs preferred over old summaries in the blind pairs.
- No overclaiming.

If a gate fails, revise the prompts and run again. Results and the decision go in `docs/rag-plan.md`.

## Round 2: after the gates pass

Designed now so Round 1's output shape fits it, and planned separately.

- **Storage:**
  - Papers gain the blurb's hook next to `summary` (the `what` text), plus a prompt-version marker.
  - Editions store the overview text with its `[ID]` markers, and the picks (paper, hook, order).
  - Existing summaries are rewritten lazily: only when a paper lands in an edition's top K and its marker is older than the current prompt.
- **Prep:** after the top-K blurbs, one overview-and-picks call replaces `write_overview`. A failed or budget-blocked call stores the edition without overview and picks.
- **Email, per topic section:**
  - a header with the label, date range and "N new papers this period, your top K below";
  - **Start here:** the picks with hooks and links;
  - **The period in brief:** the overview, with `[ID]` rendered as arXiv links, even to papers outside this reader's list;
  - **Also new:** the rest of the reader's top K, with hook and `what`, best first (no longer grouped by day);
  - a footer with the arXiv thank-you, the AI disclaimer, and room for Phase 3's manage and unsubscribe links.
  - All model text and labels are HTML-escaped before linkifying. A fallback edition renders "Also new" only.
- **Tests (from the launch plan):**
  - section order;
  - citations render as working links;
  - escaping;
  - unknown IDs dropped;
  - picks only from the top 5;
  - the same picks and overview for every subscriber of a topic.

## Left to the builder

- Function names and signatures.
- Where the blurb hook and version live (a column on `papers` or a small JSON).
- The eval module's name and CLI flags.
- How the 25-paper input is clipped if it gets long.
- Exact prompt wording within the rules above. The wording is iterated against the eval.
