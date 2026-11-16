"""Starting Temporal's test servers survives a failed binary download: CI
runners download the server on first use, and a transient CDN error
("Failed starting test server: error decoding response body") must not
fail whichever test happens to run first."""
import pytest

from tests.temporal_servers import start_with_retry

DOWNLOAD_ERROR = RuntimeError("Failed starting test server: error decoding response body")


async def _no_sleep(seconds):
    _no_sleep.slept.append(seconds)


def _starter(failures: list):
    calls = []

    async def start():
        calls.append(1)
        if failures:
            raise failures.pop(0)
        return "env"

    return start, calls


async def test_retries_a_failed_download_then_succeeds():
    _no_sleep.slept = []
    start, calls = _starter([DOWNLOAD_ERROR, DOWNLOAD_ERROR])
    assert await start_with_retry(start, sleep=_no_sleep) == "env"
    assert len(calls) == 3 and _no_sleep.slept == [2, 5]


async def test_gives_up_after_three_attempts():
    _no_sleep.slept = []
    start, calls = _starter([DOWNLOAD_ERROR] * 3)
    with pytest.raises(RuntimeError, match="error decoding response body"):
        await start_with_retry(start, sleep=_no_sleep)
    assert len(calls) == 3


async def test_other_errors_are_not_retried():
    _no_sleep.slept = []
    start, calls = _starter([ValueError("a real bug")])
    with pytest.raises(ValueError):
        await start_with_retry(start, sleep=_no_sleep)
    assert len(calls) == 1 and _no_sleep.slept == []


def test_download_dir_comes_from_the_environment(monkeypatch):
    from tests import temporal_servers

    monkeypatch.delenv("TEMPORAL_TEST_SERVER_DIR", raising=False)
    assert temporal_servers.download_dir() is None  # the SDK default (system temp dir)
    monkeypatch.setenv("TEMPORAL_TEST_SERVER_DIR", "/tmp/x")
    assert temporal_servers.download_dir() == "/tmp/x"
