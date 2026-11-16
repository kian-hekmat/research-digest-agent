"""Start Temporal's test servers reliably.

`WorkflowEnvironment.start_time_skipping()` and `.start_local()` download
their server binaries (about 65 MB and 120 MB) the first time they run on a
machine. On a fresh CI runner that download happens inside whichever test
starts a server first, and a transient CDN failure ("Failed starting test
server: error decoding response body", 2026-11-16) failed that one test.

So: retry the start when it fails to start (only that), and let CI point the
SDK at a cached directory (TEMPORAL_TEST_SERVER_DIR) that it fills before the
suite runs - `python -m tests.temporal_servers` downloads both binaries.
Locally the variable is unset and the SDK's default temp dir is used.
"""
from __future__ import annotations

import asyncio
import os
from typing import Awaitable, Callable, TypeVar

from temporalio.testing import WorkflowEnvironment

T = TypeVar("T")
_DELAYS = (2, 5)  # seconds before the 2nd and 3rd attempts


def download_dir() -> str | None:
    return os.environ.get("TEMPORAL_TEST_SERVER_DIR") or None


async def start_with_retry(
    start: Callable[[], Awaitable[T]], *, sleep: Callable[[float], Awaitable[None]] = asyncio.sleep
) -> T:
    """`start()`, retried up to twice when the SDK reports it couldn't start
    or download the server. Any other error is a real failure: raised at once."""
    for attempt, delay in enumerate((*_DELAYS, None)):
        try:
            return await start()
        except RuntimeError as exc:
            if delay is None or not str(exc).startswith("Failed starting"):
                raise
            await sleep(delay)
    raise AssertionError("unreachable")


async def time_skipping() -> WorkflowEnvironment:
    return await start_with_retry(lambda: WorkflowEnvironment.start_time_skipping(download_dest_dir=download_dir()))


async def local() -> WorkflowEnvironment:
    return await start_with_retry(lambda: WorkflowEnvironment.start_local(download_dest_dir=download_dir()))


async def _download_both() -> None:
    for start in (time_skipping, local):
        env = await start()
        await env.shutdown()


if __name__ == "__main__":
    asyncio.run(_download_both())
    print("Temporal test servers ready in", download_dir() or "the system temp dir")
