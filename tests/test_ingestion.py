import json
from datetime import datetime, timezone
from types import SimpleNamespace

import httpx
import pytest
import respx

from app.config import Settings
from app.services.arxiv import search_arxiv
from app.services.summarize import Summarizer

# A trimmed but realistically-shaped arXiv Atom response: line-wrapped title and
# summary, a version suffix on the id, two entries with different dates.
ATOM = """<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <entry>
    <id>http://arxiv.org/abs/2408.12345v2</id>
    <published>2024-08-22T17:59:00Z</published>
    <title>A Great Paper
  on Retrieval</title>
    <summary>  We show that retrieval helps.
  A lot.  </summary>
  </entry>
  <entry>
    <id>http://arxiv.org/abs/2408.00001v1</id>
    <published>2024-08-01T00:00:00Z</published>
    <title>An Older Paper</title>
    <summary>Older work on the same topic.</summary>
  </entry>
</feed>
"""


@respx.mock
def test_search_arxiv_parses_atom():
    respx.get("https://export.arxiv.org/api/query").mock(
        return_value=httpx.Response(200, text=ATOM)
    )

    results = search_arxiv("retrieval", delay=0)

    assert [r.arxiv_id for r in results] == ["2408.12345", "2408.00001"]
    assert results[0].title == "A Great Paper on Retrieval"
    assert results[0].abstract == "We show that retrieval helps. A lot."
    assert results[0].published_at == datetime(2024, 8, 22, 17, 59, tzinfo=timezone.utc)


@respx.mock
def test_search_arxiv_since_filter_drops_older_entries():
    respx.get("https://export.arxiv.org/api/query").mock(
        return_value=httpx.Response(200, text=ATOM)
    )

    cutoff = datetime(2024, 8, 10, tzinfo=timezone.utc)
    results = search_arxiv("retrieval", since=cutoff, delay=0)

    assert [r.arxiv_id for r in results] == ["2408.12345"]


@pytest.mark.parametrize(
    "query, expected",
    [
        ("reinforcement learning from human feedback", 'all:"reinforcement learning from human feedback"'),
        ('"physics-informed neural networks"', 'all:"physics-informed neural networks"'),
        # A field-prefixed query is sent as-is - how to track a whole category.
        ("cat:math.NA", "cat:math.NA"),
        ("  ti:transformer  ", "ti:transformer"),
    ],
)
@respx.mock
def test_search_arxiv_search_query(query, expected):
    route = respx.get("https://export.arxiv.org/api/query").mock(
        return_value=httpx.Response(200, text=ATOM)
    )

    search_arxiv(query, delay=0)

    assert route.calls.last.request.url.params["search_query"] == expected


@respx.mock
def test_search_arxiv_raises_on_http_error():
    respx.get("https://export.arxiv.org/api/query").mock(
        return_value=httpx.Response(503)
    )

    with pytest.raises(httpx.HTTPStatusError):
        search_arxiv("retrieval", delay=0)


class _FakeMessages:
    def __init__(self, text):
        self._text = text
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        return SimpleNamespace(
            content=[SimpleNamespace(type="text", text=self._text)]
        )


class _FakeAnthropic:
    def __init__(self, text):
        self.messages = _FakeMessages(text)


def test_summarize_paper_calls_summary_model():
    fake = _FakeAnthropic("A tight summary.")
    settings = Settings(anthropic_api_key="test-key")
    out = Summarizer(client=fake, settings=settings).summarize_paper("Some Title", "Some abstract.")

    assert out == "A tight summary."
    call = fake.messages.calls[0]
    assert call["model"] == "claude-haiku-4-5"
    assert call["max_tokens"] == 400
    assert "Some Title" in call["messages"][0]["content"]


def test_write_overview_numbers_the_summaries():
    fake = _FakeAnthropic("Synthesized overview.")
    settings = Settings(anthropic_api_key="test-key")
    out = Summarizer(client=fake, settings=settings).write_overview(
        "RLHF", ["first point", "second point"]
    )

    assert out == "Synthesized overview."
    call = fake.messages.calls[0]
    assert call["model"] == "claude-sonnet-5"
    content = call["messages"][0]["content"]
    assert "1. first point" in content and "2. second point" in content


def test_summarize_paper_returns_none_without_api_key():
    fake = _FakeAnthropic("should never be seen")
    settings = Settings(anthropic_api_key="")

    out = Summarizer(client=fake, settings=settings).summarize_paper("Title", "Abstract")

    assert out is None
    assert fake.messages.calls == []  # no network call attempted


def test_write_overview_returns_none_without_api_key():
    fake = _FakeAnthropic("should never be seen")
    settings = Settings(anthropic_api_key="")

    out = Summarizer(client=fake, settings=settings).write_overview("RLHF", ["a summary"])

    assert out is None
    assert fake.messages.calls == []


# ---------- Ollama backend ----------
OLLAMA_CHAT = "http://ollama.test:11434/api/chat"


def _ollama_settings(**overrides):
    return Settings(
        summary_backend="ollama",
        ollama_base_url="http://ollama.test:11434",
        ollama_model="gemma3:12b",
        anthropic_api_key="",
        **overrides,
    )


def _ollama_reply(text):
    return httpx.Response(
        200, json={"model": "gemma3:12b", "message": {"role": "assistant", "content": text}, "done": True}
    )


@respx.mock
def test_ollama_summarize_paper_posts_a_chat_request():
    route = respx.post(OLLAMA_CHAT).mock(return_value=_ollama_reply("  A local summary.\n"))

    out = Summarizer(settings=_ollama_settings()).summarize_paper("Some Title", "Some abstract.")

    assert out == "A local summary."
    body = json.loads(route.calls.last.request.content)
    assert body["model"] == "gemma3:12b"
    assert body["stream"] is False
    assert body["options"]["num_predict"] == 400
    system, user = body["messages"]
    assert system["role"] == "system" and "3-4 sentences" in system["content"]
    assert user == {"role": "user", "content": "Title: Some Title\n\nAbstract: Some abstract."}


@respx.mock
def test_ollama_write_overview_uses_the_local_model_not_the_claude_one():
    route = respx.post(OLLAMA_CHAT).mock(return_value=_ollama_reply("Overview."))

    out = Summarizer(settings=_ollama_settings()).write_overview("RLHF", ["first", "second"])

    assert out == "Overview."
    body = json.loads(route.calls.last.request.content)
    assert body["model"] == "gemma3:12b"
    assert body["options"]["num_predict"] == 500
    assert "1. first" in body["messages"][1]["content"]


@respx.mock
def test_ollama_backend_needs_no_api_key_and_never_calls_anthropic():
    respx.post(OLLAMA_CHAT).mock(return_value=_ollama_reply("Local."))
    fake_anthropic = _FakeAnthropic("should never be seen")

    out = Summarizer(client=fake_anthropic, settings=_ollama_settings()).summarize_paper("T", "A")

    assert out == "Local."
    assert fake_anthropic.messages.calls == []


@respx.mock
def test_ollama_trailing_slash_in_base_url():
    route = respx.post(OLLAMA_CHAT).mock(return_value=_ollama_reply("ok"))
    settings = _ollama_settings()
    settings.ollama_base_url = "http://ollama.test:11434/"

    Summarizer(settings=settings).summarize_paper("T", "A")

    assert route.called


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(404, json={"error": "model 'gemma3:12b' not found"}),  # not pulled
        httpx.Response(500, json={"error": "llama runner process has terminated"}),
    ],
)
@respx.mock
def test_ollama_errors_raise_so_the_activity_retries(response):
    """Must raise, not return None - None means "summaries deliberately off"
    and would be stored as no summary instead of being retried/counted."""
    respx.post(OLLAMA_CHAT).mock(return_value=response)

    with pytest.raises(httpx.HTTPStatusError):
        Summarizer(settings=_ollama_settings()).summarize_paper("T", "A")


@respx.mock
def test_ollama_server_down_raises():
    respx.post(OLLAMA_CHAT).mock(side_effect=httpx.ConnectError("connection refused"))

    with pytest.raises(httpx.ConnectError):
        Summarizer(settings=_ollama_settings()).summarize_paper("T", "A")


def test_test_session_never_reads_the_local_env_file():
    """conftest disables .env loading; it once did so only after
    app.database had already cached a .env-loaded Settings, so tests ran with
    the developer's real SMTP credentials and SUMMARY_BACKEND (and called
    their real local model). Every field below is set only in .env, never by
    the test harness, so a default here proves the cached Settings is clean."""
    from app.config import get_settings

    settings = get_settings()
    assert settings.summary_backend == "anthropic"
    assert settings.smtp_password == ""


def test_summaries_enabled_by_backend():
    assert Settings(summary_backend="ollama", anthropic_api_key="").summaries_enabled is True
    assert Settings(summary_backend="anthropic", anthropic_api_key="").summaries_enabled is False
    assert Settings(summary_backend="anthropic", anthropic_api_key="k").summaries_enabled is True
