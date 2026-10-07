"""The Digest workflow: orchestration only, no I/O of its own.

This is Phase 2's `run_digest` (now retired) split at its I/O boundaries: each
former step is an Activity with its own timeout/retry policy, and per-paper
summarization fans out concurrently instead of running in a loop.

Workflow code must be deterministic (Temporal replays it from history), so:
  - no direct DB/HTTP/LLM calls - everything goes through `execute_activity`
  - wall-clock time comes from `workflow.now()`, never `datetime.now()`
  - the only non-stdlib import at module scope is `activities`, wrapped in
    `imports_passed_through()` since it pulls in SQLAlchemy/httpx/anthropic

`imports_passed_through()` covers *this* import statement, but SQLAlchemy's
declarative models register Tables on a single shared MetaData at import time
- if the sandbox re-executes that registration via any other path, it dies
  with "Table ... already defined". `WORKFLOW_RUNNER` below closes that gap by
  declaring the whole app.* + SQLAlchemy/httpx/anthropic chain passthrough at
  the Worker level; pass it as `Worker(..., workflow_runner=WORKFLOW_RUNNER)`
  wherever a Worker is constructed (see app/worker.py and the tests).
"""
from __future__ import annotations

import asyncio
from datetime import timedelta

from temporalio import workflow
from temporalio.common import RetryPolicy
from temporalio.exceptions import ActivityError
from temporalio.worker.workflow_sandbox import SandboxedWorkflowRunner, SandboxRestrictions

from app.temporal import types

with workflow.unsafe.imports_passed_through():
    from app.temporal import activities

WORKFLOW_RUNNER = SandboxedWorkflowRunner(
    restrictions=SandboxRestrictions.default.with_passthrough_modules(
        "app.temporal.activities",
        "app.crud",
        "app.models",
        "app.database",
        "app.config",
        "app.services.arxiv",
        "app.services.summarize",
        "app.services.email",
        "app.services.ranking",
        "app.services.scholar",
        "sqlalchemy",
        "anthropic",
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
_MAX_CONCURRENT_SUMMARIES = 4
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


def _cause_message(exc: ActivityError) -> str:
    return str(exc.cause) if exc.cause else str(exc)


@workflow.defn
class DigestWorkflow:
    @workflow.run
    async def run(self, digest_id: str) -> None:
        ctx: types.DigestContext = await workflow.execute_activity(
            activities.get_digest_context,
            digest_id,
            start_to_close_timeout=timedelta(seconds=30),
            retry_policy=_DB_RETRY,
        )
        run_started = workflow.now()  # watermark: when the run started, not finished

        # --- 1. fetch ---------------------------------------------------------
        try:
            results = await workflow.execute_activity(
                activities.fetch_arxiv,
                ctx,
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

        # --- 3. summarize + rate relevance to this topic, concurrently -------
        # One LLM call per new paper does both. A paper another topic already
        # summarized only needs this topic's relevance rating; a failed rating
        # just leaves it unrated (the ranking imputes it), so those aren't
        # counted as failures.
        # At most _MAX_CONCURRENT_SUMMARIES in flight: a local Ollama server
        # works through requests a few at a time, and each activity's timeout
        # clock runs while it waits in that queue - firing all 25 at once
        # would time out the back of the queue and retry work already done.
        to_summarize = [p for p in papers if not p.existing_summary]
        to_rate_only = [p for p in papers if p.existing_summary]
        llm_slots = asyncio.Semaphore(_MAX_CONCURRENT_SUMMARIES)

        def llm_input(p: types.IngestedPaper) -> types.SummarizePaperInput:
            return types.SummarizePaperInput(
                paper_id=p.paper_id,
                title=p.title,
                abstract=p.abstract or "",
                topic_id=ctx.topic_id,
                topic_name=ctx.topic_name,
                topic_query=ctx.query,
            )

        async def summarize(p: types.IngestedPaper) -> str | None:
            async with llm_slots:
                return await workflow.execute_activity(
                    activities.summarize_paper,
                    llm_input(p),
                    start_to_close_timeout=_LLM_TIMEOUT,
                    retry_policy=_LLM_RETRY,
                )

        async def rate(p: types.IngestedPaper) -> int | None:
            async with llm_slots:
                return await workflow.execute_activity(
                    activities.rate_relevance,
                    llm_input(p),
                    start_to_close_timeout=_LLM_TIMEOUT,
                    retry_policy=_LLM_RETRY,
                )

        summary_results, _ = await asyncio.gather(
            asyncio.gather(*[summarize(p) for p in to_summarize], return_exceptions=True),
            asyncio.gather(*[rate(p) for p in to_rate_only], return_exceptions=True),
        )
        summary_failures = sum(1 for r in summary_results if isinstance(r, BaseException))
        new_summaries = [r for r in summary_results if isinstance(r, str)]
        all_summaries = [p.existing_summary for p in papers if p.existing_summary] + new_summaries

        # --- 4. synthesized overview across the batch -------------------------
        overview = None
        if all_summaries:
            try:
                overview = await workflow.execute_activity(
                    activities.write_overview,
                    types.WriteOverviewInput(
                        topic_name=ctx.topic_name, summaries=all_summaries
                    ),
                    start_to_close_timeout=_LLM_TIMEOUT,
                    retry_policy=_LLM_RETRY,
                )
            except ActivityError:
                overview = None

        # --- 5. finalize --------------------------------------------------
        error = (
            f"{summary_failures} of {len(to_summarize)} paper summaries failed"
            if summary_failures
            else None
        )
        await workflow.execute_activity(
            activities.finalize_digest,
            types.FinalizeDigestInput(
                digest_id=digest_id,
                status=types.DIGEST_STATUS_COMPLETED,
                overview=overview,
                error=error,
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


@workflow.defn
class RunAllTopicDigestsWorkflow:
    """Scheduled entrypoint (see `app.temporal.schedule`): create a pending
    digest for every topic and run a `DigestWorkflow` child per digest,
    concurrently. Doing the fan-out here - rather than pointing the Schedule
    straight at `DigestWorkflow` - keeps `DigestWorkflow`'s contract (one
    digest id, however it was created) the same for both the manual trigger
    and the schedule.
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

        digest_ids: list[str] = list(
            await asyncio.gather(
                *[
                    workflow.execute_activity(
                        activities.create_pending_digest,
                        topic_id,
                        start_to_close_timeout=timedelta(seconds=30),
                        retry_policy=_DB_RETRY,
                    )
                    for topic_id in topic_ids
                ]
            )
        )

        task_queue = workflow.info().task_queue
        await asyncio.gather(
            *[
                workflow.execute_child_workflow(
                    DigestWorkflow.run,
                    digest_id,
                    id=f"digest-{digest_id}",
                    task_queue=task_queue,
                )
                for digest_id in digest_ids
            ]
        )
        return len(digest_ids)


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
