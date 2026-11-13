"""The pipeline's workflows: orchestration only, no I/O of their own.

`DigestWorkflow` is the cheap daily ingest - fetch, embed and rate each
topic's new papers. Summaries and overviews are written later, for each
topic's top papers only, by send prep. Every step is an Activity with its own
timeout/retry policy, and per-paper ratings fan out concurrently.

arXiv requests (`fetch_arxiv`, `fetch_fulltext`) run on a separate task queue,
`arxiv_queue(<the workflow's queue>)`, whose worker runs one activity at a
time: however many topics ingest at once, one arXiv request is in flight.

Workflow code must be deterministic (Temporal replays it from history), so:
  - no direct DB/HTTP/LLM calls - everything goes through `execute_activity`
  - wall-clock time comes from `workflow.now()`, never `datetime.now()`
  - the only non-stdlib import at module scope is `activities`, wrapped in
    `imports_passed_through()` since it pulls in SQLAlchemy/httpx

`imports_passed_through()` covers *this* import statement, but SQLAlchemy's
declarative models register Tables on a single shared MetaData at import time
- if the sandbox re-executes that registration via any other path, it dies
  with "Table ... already defined". `WORKFLOW_RUNNER` below closes that gap by
  declaring the whole app.* + SQLAlchemy/httpx chain passthrough at
  the Worker level; pass it as `Worker(..., workflow_runner=WORKFLOW_RUNNER)`
  wherever a Worker is constructed (see app/worker.py and the tests).
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta

from temporalio import workflow
from temporalio.common import RetryPolicy
from temporalio.exceptions import ActivityError, ChildWorkflowError
from temporalio.worker.workflow_sandbox import SandboxedWorkflowRunner, SandboxRestrictions

from app.temporal import types

with workflow.unsafe.imports_passed_through():
    from app import cadence
    from app.temporal import activities

WORKFLOW_RUNNER = SandboxedWorkflowRunner(
    restrictions=SandboxRestrictions.default.with_passthrough_modules(
        "app.temporal.activities",
        # Its dataclasses carry datetimes (an edition's slot): decoded inside
        # the sandbox, a re-imported module's `datetime` hint isn't the
        # converter's, and decoding fails.
        "app.temporal.types",
        "app.alerts",
        "app.cadence",
        "app.crud",
        "app.models",
        "app.database",
        "app.config",
        "app.services.arxiv",
        "app.services.summarize",
        "app.services.embed",
        "app.services.retrieval",
        "app.services.fulltext",
        "app.services.email",
        "app.services.ranking",
        "app.services.scholar",
        "sqlalchemy",
        "httpx",
        "feedparser",
    )
)

# --- retry policies, one per kind of external dependency ---
_DB_RETRY = RetryPolicy(
    maximum_attempts=3,
    initial_interval=timedelta(seconds=1),
    backoff_coefficient=2.0,
)
_ARXIV_RETRY = RetryPolicy(
    maximum_attempts=5,
    initial_interval=timedelta(seconds=5),
    backoff_coefficient=2.0,
    maximum_interval=timedelta(seconds=60),
)
_LLM_RETRY = RetryPolicy(
    maximum_attempts=3,
    initial_interval=timedelta(seconds=2),
    backoff_coefficient=2.0,
    maximum_interval=timedelta(seconds=30),
)
# Generous enough for a local model on its first call of a session, when
# Ollama also has to load it into memory.
_LLM_TIMEOUT = timedelta(seconds=180)
# An ingest alerts when every rating raised, once at least this many ran.
_MIN_RATINGS_FOR_ALERT = 3
# Before an email send: wait up to 20 x 30s = 10 min for in-flight digests.
_PENDING_WAIT_POLLS = 20
_PENDING_WAIT_INTERVAL = timedelta(seconds=30)
# For the h-index lookup (Semantic Scholar or OpenAlex): a few spaced-out
# attempts ride out a burst of 429s. Exhausting them only costs the email its
# h-index signal, never the send.
_SCHOLAR_RETRY = RetryPolicy(
    maximum_attempts=4,
    initial_interval=timedelta(seconds=5),
    backoff_coefficient=2.0,
    maximum_interval=timedelta(seconds=30),
)
_EMAIL_RETRY = RetryPolicy(
    maximum_attempts=3,
    initial_interval=timedelta(seconds=2),
    backoff_coefficient=2.0,
    maximum_interval=timedelta(seconds=30),
)


ARXIV_TASK_QUEUE_SUFFIX = "-arxiv"


def arxiv_queue(task_queue: str) -> str:
    """The task queue for arXiv requests that go with `task_queue`'s workflows."""
    return task_queue + ARXIV_TASK_QUEUE_SUFFIX


def _cause_message(exc: BaseException) -> str:
    """The underlying error's message, not Temporal's wrapper around it."""
    if isinstance(exc, (ActivityError, ChildWorkflowError)) and exc.cause:
        return str(exc.cause)
    return str(exc)


def _batches(items: list, size: int) -> list[list]:
    size = max(1, size)
    return [items[i : i + size] for i in range(0, len(items), size)]


@workflow.defn
class DigestWorkflow:
    """The daily ingest for one digest: fetch, ingest, embed, rate every new
    paper, finalize, advance the watermark. No summaries, overview or full
    text - send prep writes those, for each topic's top papers only."""

    @workflow.run
    async def run(self, digest_id: str) -> None:
        ctx: types.DigestContext = await workflow.execute_activity(
            activities.get_digest_context,
            digest_id,
            start_to_close_timeout=timedelta(seconds=30),
            retry_policy=_DB_RETRY,
        )
        run_started = workflow.now()  # watermark: when the run started, not finished

        # --- 1. fetch, on the one-at-a-time arXiv queue ----------------------
        # The timeout covers the request only: the activity starts when the
        # arXiv worker picks it up, not while it waits behind other topics.
        try:
            results = await workflow.execute_activity(
                activities.fetch_arxiv,
                ctx,
                task_queue=arxiv_queue(workflow.info().task_queue),
                start_to_close_timeout=timedelta(seconds=60),
                retry_policy=_ARXIV_RETRY,
            )
        except ActivityError as exc:
            await workflow.execute_activity(
                activities.finalize_digest,
                types.FinalizeDigestInput(
                    digest_id=digest_id,
                    status=types.DIGEST_STATUS_FAILED,
                    error=f"arXiv fetch failed: {_cause_message(exc)}",
                ),
                start_to_close_timeout=timedelta(seconds=30),
                retry_policy=_DB_RETRY,
            )
            return

        # --- 2. upsert papers, link to topic and this digest ------------------
        papers: list[types.IngestedPaper] = await workflow.execute_activity(
            activities.ingest_papers,
            types.IngestPapersInput(
                topic_id=ctx.topic_id, digest_id=digest_id, results=results
            ),
            start_to_close_timeout=timedelta(seconds=30),
            retry_policy=_DB_RETRY,
        )

        # --- 3. embed new papers for retrieval -------------------------------
        # Degrade, don't block: a failure only costs retrieval signals.
        if papers:
            try:
                await workflow.execute_activity(
                    activities.embed_papers,
                    types.EmbedPapersInput(
                        paper_ids=[p.paper_id for p in papers], topic_id=ctx.topic_id
                    ),
                    start_to_close_timeout=_LLM_TIMEOUT,
                    retry_policy=_LLM_RETRY,
                )
            except ActivityError:
                pass

        # --- 4. rate every new paper's relevance to this topic ---------------
        # Whether or not another topic already summarized it: relevance is
        # per topic. A failed rating leaves the paper unrated (send prep
        # re-rates it); one the budget skipped returns None and isn't a
        # failure. At most llm_max_concurrency in flight, so each activity's
        # timeout isn't spent queueing behind the others.
        llm_slots = asyncio.Semaphore(max(1, ctx.llm_max_concurrency))

        async def rate(p: types.IngestedPaper) -> int | None:
            async with llm_slots:
                return await workflow.execute_activity(
                    activities.rate_relevance,
                    types.SummarizePaperInput(
                        paper_id=p.paper_id,
                        title=p.title,
                        abstract=p.abstract or "",
                        topic_id=ctx.topic_id,
                        topic_name=ctx.topic_name,
                        topic_query=ctx.query,
                    ),
                    start_to_close_timeout=_LLM_TIMEOUT,
                    retry_policy=_LLM_RETRY,
                )

        ratings = await asyncio.gather(*[rate(p) for p in papers], return_exceptions=True)
        failures = [r for r in ratings if isinstance(r, BaseException)]
        if len(papers) >= _MIN_RATINGS_FOR_ALERT and len(failures) == len(papers):
            await self._alert_ratings_failing(ctx, len(papers), failures[0])

        # --- 5. finalize, then advance the watermark -------------------------
        await workflow.execute_activity(
            activities.finalize_digest,
            types.FinalizeDigestInput(
                digest_id=digest_id,
                status=types.DIGEST_STATUS_COMPLETED,
                error=f"{len(failures)} of {len(papers)} ratings failed" if failures else None,
            ),
            start_to_close_timeout=timedelta(seconds=30),
            retry_policy=_DB_RETRY,
        )
        await workflow.execute_activity(
            activities.advance_watermark,
            types.AdvanceWatermarkInput(topic_id=ctx.topic_id, checked_at=run_started),
            start_to_close_timeout=timedelta(seconds=30),
            retry_policy=_DB_RETRY,
        )

    async def _alert_ratings_failing(
        self, ctx: types.DigestContext, attempted: int, first_error: BaseException
    ) -> None:
        """One alert a day (Eastern), however many topics fail: the key
        carries the date. The activity never raises."""
        day = workflow.now().astimezone(cadence.SEND_TIME_ZONE).date()
        await workflow.execute_activity(
            activities.send_pipeline_alert,
            types.PipelineAlertInput(
                key=f"ratings-failing:{day:%Y-%m-%d}",
                subject="Research digest: every relevance rating failed",
                body=(
                    f"All {attempted} relevance ratings in today's ingest for the topic "
                    f"{ctx.topic_name!r} raised, after retries. Those papers stay unrated "
                    "until send prep re-rates them.\n\n"
                    f"First error: {_cause_message(first_error)}"
                ),
            ),
            start_to_close_timeout=timedelta(seconds=30),
            retry_policy=_DB_RETRY,
        )


@workflow.defn
class RunAllTopicDigestsWorkflow:
    """Scheduled entrypoint (see `app.temporal.schedule`): create a pending
    digest for every topic and run a `DigestWorkflow` child per digest, in
    batches of `fanout_batch_size`, waiting for each batch before starting
    the next. Doing the fan-out here - rather than pointing the Schedule
    straight at `DigestWorkflow` - keeps `DigestWorkflow`'s contract (one
    digest id, however it was created) the same for both the manual trigger
    and the schedule.

    One topic's failure (its topic deleted mid-run, a DB outage outlasting
    the retries) is logged and never holds back the other topics. Returns
    how many digests were created.
    """

    @workflow.run
    async def run(self) -> int:
        topic_ids: list[str] = await workflow.execute_activity(
            activities.list_active_topic_ids,
            start_to_close_timeout=timedelta(seconds=30),
            retry_policy=_DB_RETRY,
        )
        if not topic_ids:
            return 0
        limits: types.PipelineLimits = await workflow.execute_activity(
            activities.get_pipeline_limits,
            start_to_close_timeout=timedelta(seconds=30),
            retry_policy=_DB_RETRY,
        )

        created = 0
        for batch in _batches(topic_ids, limits.fanout_batch_size):
            created_ids = await asyncio.gather(
                *[
                    workflow.execute_activity(
                        activities.create_pending_digest,
                        topic_id,
                        start_to_close_timeout=timedelta(seconds=30),
                        retry_policy=_DB_RETRY,
                    )
                    for topic_id in batch
                ],
                return_exceptions=True,
            )
            digest_ids = [d for d in created_ids if isinstance(d, str)]
            created += len(digest_ids)
            outcomes = await asyncio.gather(
                *[
                    workflow.execute_child_workflow(
                        DigestWorkflow.run,
                        digest_id,
                        id=f"digest-{digest_id}",
                        task_queue=workflow.info().task_queue,
                    )
                    for digest_id in digest_ids
                ],
                return_exceptions=True,
            )
            for failure in [*created_ids, *outcomes]:
                if isinstance(failure, BaseException):
                    workflow.logger.warning("A topic's digest failed: %s", _cause_message(failure))
        return created


@workflow.defn
class PrepTopicEditionWorkflow:
    """Send prep for one topic and slot: re-rate what ingest left unrated,
    look up h-indices, rank the window, summarize the top K only (full text
    first when it's on), write the overview over them, and store it all as
    the topic's edition. Returns "written", "exists" (the slot already has
    an edition: a second prep is a no-op) or "empty" (no papers: nothing
    stored)."""

    @workflow.run
    async def run(self, input: types.PrepTopicInput) -> str:
        plan: types.EditionPlan = await workflow.execute_activity(
            activities.plan_edition,
            types.PlanEditionInput(
                topic_id=input.topic_id, slot=input.slot, window_end=workflow.now()
            ),
            start_to_close_timeout=timedelta(seconds=30),
            retry_policy=_DB_RETRY,
        )
        if plan.exists:
            return "exists"
        if not plan.paper_ids:
            return "empty"
        llm_slots = asyncio.Semaphore(max(1, plan.llm_max_concurrency))

        async def llm(activity, arg):
            async with llm_slots:
                return await workflow.execute_activity(
                    activity, arg, start_to_close_timeout=_LLM_TIMEOUT, retry_policy=_LLM_RETRY
                )

        # 1. The §3.6 re-rating: papers a failed or budget-skipped rating
        # left unrated come back here. A failure leaves them unrated again.
        await asyncio.gather(
            *[llm(activities.rate_relevance, p) for p in plan.unrated], return_exceptions=True
        )

        # 2. h-index, then the ranking. A lookup failure only costs the
        # ranking its h-index term.
        try:
            await workflow.execute_activity(
                activities.enrich_h_index_for_papers,
                plan.paper_ids,
                start_to_close_timeout=timedelta(seconds=90),
                retry_policy=_SCHOLAR_RETRY,
            )
        except ActivityError:
            workflow.logger.warning("Author h-index lookup failed; ranking without it")
        ranking: types.EditionRanking = await workflow.execute_activity(
            activities.rank_edition,
            plan,
            start_to_close_timeout=timedelta(seconds=60),
            retry_policy=_DB_RETRY,
        )

        # 3. Full text for the top K only, on the one-at-a-time arXiv queue.
        to_summarize = [p for p in ranking.top if p.summary is None]
        if plan.fulltext and to_summarize:
            try:
                await workflow.execute_activity(
                    activities.fetch_fulltext,
                    types.FetchFulltextInput(paper_ids=[p.paper_id for p in to_summarize]),
                    task_queue=arxiv_queue(workflow.info().task_queue),
                    start_to_close_timeout=timedelta(minutes=30),
                    heartbeat_timeout=timedelta(minutes=3),
                    retry_policy=_LLM_RETRY,
                )
            except ActivityError:
                pass

        # 4. Summaries for the top K that don't have one yet.
        outcomes = await asyncio.gather(
            *[
                llm(
                    activities.summarize_for_edition,
                    types.SummarizePaperInput(
                        paper_id=p.paper_id,
                        title=p.title,
                        abstract=p.abstract,
                        topic_id=plan.topic_id,
                        topic_name=plan.topic_name,
                        topic_query=plan.topic_query,
                    ),
                )
                for p in to_summarize
            ],
            return_exceptions=True,
        )
        new = {p.paper_id: o for p, o in zip(to_summarize, outcomes)}
        failures = [o for o in outcomes if isinstance(o, BaseException)]
        summaries = [
            s
            for p in ranking.top
            if isinstance(s := (p.summary or new.get(p.paper_id)), str) and s
        ]

        # 5. The overview over the top K's summaries, history-aware.
        overview = None
        if summaries:
            try:
                overview = await llm(
                    activities.write_overview,
                    types.WriteOverviewInput(
                        topic_name=plan.topic_name,
                        summaries=summaries,
                        topic_id=plan.topic_id,
                        paper_ids=[p.paper_id for p in ranking.top],
                    ),
                )
            except ActivityError:
                workflow.logger.warning("Overview failed for topic %s", plan.topic_id)

        await workflow.execute_activity(
            activities.store_edition,
            types.StoreEditionInput(
                topic_id=plan.topic_id,
                slot=plan.slot,
                window_start=plan.window_start,
                window_end=plan.window_end,
                k=plan.k,
                overview=overview,
                ranked_paper_ids=ranking.paper_ids,
            ),
            start_to_close_timeout=timedelta(seconds=30),
            retry_policy=_DB_RETRY,
        )
        if failures and len(failures) == len(to_summarize):
            await self._alert_summaries_failing(plan, len(failures), failures[0])
        return "written"

    async def _alert_summaries_failing(
        self, plan: types.EditionPlan, attempted: int, first_error: BaseException
    ) -> None:
        day = workflow.now().astimezone(cadence.SEND_TIME_ZONE).date()
        await workflow.execute_activity(
            activities.send_pipeline_alert,
            types.PipelineAlertInput(
                key=f"summaries-failing:{day:%Y-%m-%d}",
                subject="Research digest: every paper summary failed",
                body=(
                    f"All {attempted} summaries in send prep for the topic "
                    f"{plan.topic_name!r} raised, after retries. Its edition went out "
                    "without them.\n\n"
                    f"First error: {_cause_message(first_error)}"
                ),
            ),
            start_to_close_timeout=timedelta(seconds=30),
            retry_policy=_DB_RETRY,
        )


@workflow.defn
class PrepEditionsWorkflow:
    """Scheduled send prep (see app.temporal.schedule): one
    `PrepTopicEditionWorkflow` per topic with active subscriptions, in
    batches of `fanout_batch_size`. The slot defaults to the next send slot.
    One topic's failure never holds back the others. Returns how many
    editions were written."""

    @workflow.run
    async def run(self, slot_iso: str | None = None) -> int:
        slot = (
            datetime.fromisoformat(slot_iso) if slot_iso else cadence.next_slot(workflow.now())
        )
        topic_ids: list[str] = await workflow.execute_activity(
            activities.list_active_topic_ids,
            start_to_close_timeout=timedelta(seconds=30),
            retry_policy=_DB_RETRY,
        )
        if not topic_ids:
            return 0
        limits: types.PipelineLimits = await workflow.execute_activity(
            activities.get_pipeline_limits,
            start_to_close_timeout=timedelta(seconds=30),
            retry_policy=_DB_RETRY,
        )
        written = 0
        for batch in _batches(topic_ids, limits.fanout_batch_size):
            outcomes = await asyncio.gather(
                *[
                    workflow.execute_child_workflow(
                        PrepTopicEditionWorkflow.run,
                        types.PrepTopicInput(topic_id=topic_id, slot=slot),
                        id=f"prep-{topic_id}-{slot:%Y%m%dT%H%M}",
                        task_queue=workflow.info().task_queue,
                    )
                    for topic_id in batch
                ],
                return_exceptions=True,
            )
            for outcome in outcomes:
                if isinstance(outcome, BaseException):
                    workflow.logger.warning("A topic's send prep failed: %s", _cause_message(outcome))
                elif outcome == "written":
                    written += 1
        return written


@workflow.defn
class SendDigestEmailsWorkflow:
    """Scheduled entrypoint for email delivery (see app.temporal.schedule).

    Fires twice a week; each subscription is judged individually against its
    own cadence by `list_due_subscriptions`. A recipient's due subscriptions
    are combined into ONE email with a section per topic, rather than one
    email per topic. Deliveries to different recipients fan out concurrently,
    and one recipient's failure (or nothing-new skip) never blocks another's.

    Before gathering, it brings content up to date itself instead of trusting
    that the daily digest run happened: it runs a digest pass as a child, then
    waits (bounded) for any other digest runs still in flight - e.g. the daily
    run firing at the same moment on a laptop-wake catch-up, or one resuming
    after a restart. A refresh failure never blocks the send; it goes out
    with whatever completed.

    Returns the number of emails sent.
    """

    @workflow.run
    async def run(self) -> int:
        await self._refresh_digests()
        await self._wait_for_in_flight_digests()

        due: list[types.DueSubscription] = await workflow.execute_activity(
            activities.list_due_subscriptions,
            start_to_close_timeout=timedelta(seconds=30),
            retry_policy=_DB_RETRY,
        )
        if not due:
            return 0

        await self._enrich_author_h_index(due)

        # dict preserves first-seen order, and `due` arrives oldest
        # subscription first - so topics appear in the order subscribed.
        by_recipient: dict[str, list[types.DueSubscription]] = {}
        for sub in due:
            by_recipient.setdefault(sub.email, []).append(sub)

        results = await asyncio.gather(
            *[self._deliver(email, subs) for email, subs in by_recipient.items()],
            return_exceptions=True,
        )
        return sum(1 for r in results if r is True)

    async def _refresh_digests(self) -> None:
        try:
            await workflow.execute_child_workflow(
                RunAllTopicDigestsWorkflow.run,
                id=f"{workflow.info().workflow_id}-refresh",
                task_queue=workflow.info().task_queue,
            )
        except Exception:
            # Same principle as a failed summary: degrade, don't block. Content
            # that did complete still goes out; the rest lands next time.
            workflow.logger.warning("Digest refresh before send failed", exc_info=True)

    async def _wait_for_in_flight_digests(self) -> None:
        """Poll until no digest is pending, for at most _PENDING_WAIT_POLLS x
        _PENDING_WAIT_INTERVAL. Giving up is safe: delivery keys off
        completed_at, so anything still pending goes out with the next email
        rather than being lost - this wait only keeps it from being late."""
        for _ in range(_PENDING_WAIT_POLLS):
            pending = await workflow.execute_activity(
                activities.count_pending_digests,
                start_to_close_timeout=timedelta(seconds=30),
                retry_policy=_DB_RETRY,
            )
            if not pending:
                return
            await workflow.sleep(_PENDING_WAIT_INTERVAL)
        workflow.logger.warning("Sending with digests still pending; they'll go out next time")

    async def _enrich_author_h_index(self, due: list[types.DueSubscription]) -> None:
        """One Semantic Scholar pass for every recipient's papers, before any
        gather. Failure degrades the ranking (no h-index term), never the send."""
        try:
            await workflow.execute_activity(
                activities.enrich_author_h_index,
                _windows(due),
                start_to_close_timeout=timedelta(seconds=90),
                retry_policy=_SCHOLAR_RETRY,
            )
        except ActivityError:
            workflow.logger.warning("Author h-index lookup failed; ranking without it", exc_info=True)

    async def _deliver(self, email: str, subs: list[types.DueSubscription]) -> bool:
        gathered: types.GatheredContent | None = await workflow.execute_activity(
            activities.gather_digest_content,
            _windows(subs),
            start_to_close_timeout=timedelta(seconds=30),
            retry_policy=_DB_RETRY,
        )
        if gathered is None:
            return False  # nothing new on any topic - leave every watermark alone

        await workflow.execute_activity(
            activities.send_digest_email,
            types.SendEmailInput(email=email, content=gathered.content),
            start_to_close_timeout=timedelta(seconds=30),
            retry_policy=_EMAIL_RETRY,
        )
        # Only advance watermarks on a confirmed send - a failed send (all
        # _EMAIL_RETRY attempts exhausted) leaves them alone, so this recipient
        # is retried, with the same content, on the next scheduled run. Topics
        # that had nothing new weren't in the email, so theirs stay put too.
        await asyncio.gather(
            *[
                workflow.execute_activity(
                    activities.mark_subscription_sent,
                    types.MarkSentInput(subscription_id=sub_id, sent_at=gathered.as_of),
                    start_to_close_timeout=timedelta(seconds=30),
                    retry_policy=_DB_RETRY,
                )
                for sub_id in gathered.subscription_ids
            ]
        )
        return True


def _windows(subs: list[types.DueSubscription]) -> types.GatherContentInput:
    return types.GatherContentInput(
        topics=[
            types.TopicWindow(
                subscription_id=s.subscription_id,
                topic_id=s.topic_id,
                topic_name=s.topic_name,
                since=s.last_sent_at,
                max_papers=s.max_papers,
            )
            for s in subs
        ]
    )
