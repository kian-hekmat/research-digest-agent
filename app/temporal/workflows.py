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

        # --- 3. summarize papers that don't have a summary yet, concurrently --
        to_summarize = [p for p in papers if not p.existing_summary]
        summary_results = await asyncio.gather(
            *[
                workflow.execute_activity(
                    activities.summarize_paper,
                    types.SummarizePaperInput(
                        paper_id=p.paper_id, title=p.title, abstract=p.abstract or ""
                    ),
                    start_to_close_timeout=timedelta(seconds=60),
                    retry_policy=_LLM_RETRY,
                )
                for p in to_summarize
            ],
            return_exceptions=True,
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
                    start_to_close_timeout=timedelta(seconds=60),
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
