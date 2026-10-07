import json
from datetime import datetime, timezone

import httpx
import pytest
import respx

from app.config import Settings
from app.services.arxiv import search_arxiv
from app.services.summarize import Summarizer, parse_rated_summary, parse_relevance

# A trimmed but realistically-shaped arXiv Atom response: line-wrapped title and
# summary, a version suffix on the id, two entries with different dates, and
# arXiv's namespaced comment/journal-ref on the first.
ATOM = """<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom" xmlns:arxiv="http://arxiv.org/schemas/atom">
  <entry>
    <id>http://arxiv.org/abs/2408.12345v2</id>
    <published>2024-08-22T17:59:00Z</published>
    <title>A Great Paper
  on Retrieval</title>
    <summary>  We show that retrieval helps.
  A lot.  </summary>
    <arxiv:comment>Accepted at NeurIPS 2024.
  12 pages, 3 figures</arxiv:comment>
    <arxiv:journal_ref>Proc. NeurIPS 37 (2024)</arxiv:journal_ref>
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
    assert results[0].comment == "Accepted at NeurIPS 2024. 12 pages, 3 figures"
    assert results[0].journal_ref == "Proc. NeurIPS 37 (2024)"
    assert results[1].comment is None and results[1].journal_ref is None


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


# ---------- Summarizer (local Gemma via Ollama) ----------
OLLAMA_CHAT = "http://ollama.test:11434/api/chat"


def _ollama_settings(**overrides):
    fields = {
        "summary_backend": "ollama",
        "ollama_base_url": "http://ollama.test:11434",
        "ollama_model": "gemma3:12b",
    }
    return Settings(**{**fields, **overrides})


def _ollama_reply(text):
    return httpx.Response(
        200, json={"model": "gemma3:12b", "message": {"role": "assistant", "content": text}, "done": True}
    )


def _sent(route):
    """The JSON body of the last request a respx route received."""
    return json.loads(route.calls.last.request.content)


@respx.mock
def test_summarize_paper_posts_a_chat_request():
    route = respx.post(OLLAMA_CHAT).mock(return_value=_ollama_reply("  A local summary.\n"))

    out = Summarizer(settings=_ollama_settings()).summarize_paper("Some Title", "Some abstract.")

    assert out == "A local summary."
    body = _sent(route)
    assert body["model"] == "gemma3:12b"
    assert body["stream"] is False
    assert body["options"]["num_predict"] == 400
    system, user = body["messages"]
    assert system["role"] == "system" and "3-4 sentences" in system["content"]
    assert user == {"role": "user", "content": "Title: Some Title\n\nAbstract: Some abstract."}


@respx.mock
def test_write_overview_numbers_the_summaries():
    route = respx.post(OLLAMA_CHAT).mock(return_value=_ollama_reply("Synthesized overview."))

    out = Summarizer(settings=_ollama_settings()).write_overview("RLHF", ["first point", "second point"])

    assert out == "Synthesized overview."
    body = _sent(route)
    assert body["model"] == "gemma3:12b"
    assert body["options"]["num_predict"] == 500
    user = body["messages"][1]["content"]
    assert "1. first point" in user and "2. second point" in user


def test_every_method_is_a_no_op_when_summaries_are_off():
    """SUMMARY_BACKEND=none: return nothing without touching the network.
    respx.mock with no routes would raise on any request at all."""
    summarizer = Summarizer(settings=_ollama_settings(summary_backend="none"))

    with respx.mock:
        assert summarizer.summarize_paper("T", "A") is None
        assert summarizer.write_overview("RLHF", ["a summary"]) is None
        assert summarizer.rate_relevance("T", "A", "RLHF", "rlhf") is None
        rated = summarizer.summarize_and_rate("T", "A", "RLHF", "rlhf")
    assert rated.summary is None and rated.relevance is None


# ---------- relevance rating ----------
@respx.mock
def test_summarize_and_rate_splits_the_rating_off_the_summary():
    route = respx.post(OLLAMA_CHAT).mock(return_value=_ollama_reply("Relevance: 8\n\nA tight summary."))

    out = Summarizer(settings=_ollama_settings()).summarize_and_rate(
        "Some Title", "Some abstract.", "PINNs", "physics-informed neural networks"
    )

    assert out.summary == "A tight summary."
    assert out.relevance == 8
    system, user = _sent(route)["messages"]
    assert "Relevance: N" in system["content"]
    assert user["content"].startswith(
        "Reader's topic: PINNs (arXiv search: physics-informed neural networks)\n\nTitle: Some Title"
    )


@respx.mock
def test_topic_query_is_left_out_when_it_just_repeats_the_name():
    route = respx.post(OLLAMA_CHAT).mock(return_value=_ollama_reply("7"))

    Summarizer(settings=_ollama_settings()).rate_relevance("T", "A", "RLHF", '"rlhf"')

    assert _sent(route)["messages"][1]["content"].startswith("Reader's topic: RLHF\n\n")


@respx.mock
def test_rate_relevance_is_a_short_rating_only_call():
    route = respx.post(OLLAMA_CHAT).mock(return_value=_ollama_reply("6"))

    out = Summarizer(settings=_ollama_settings()).rate_relevance("T", "A", "RLHF", "rlhf")

    assert out == 6
    assert _sent(route)["options"]["num_predict"] == 10


@pytest.mark.parametrize(
    "text, summary, relevance",
    [
        ("Relevance: 7\n\nThe paper does X.", "The paper does X.", 7),
        ("**Relevance:** 9/10\n\nThe paper does X.", "The paper does X.", 9),
        ("relevance = 3\nThe paper does X.", "The paper does X.", 3),
        # A rating out of range is dropped, the summary kept.
        ("Relevance: 0\n\nThe paper does X.", "The paper does X.", None),
        # No rating line at all: never lose the summary over it.
        ("The paper does X.", "The paper does X.", None),
    ],
)
def test_parse_rated_summary(text, summary, relevance):
    rated = parse_rated_summary(text)
    assert (rated.summary, rated.relevance) == (summary, relevance)


@pytest.mark.parametrize(
    "text, expected", [("8", 8), (" 10\n", 10), ("Rating: 4", 4), ("42", None), ("high", None)]
)
def test_parse_relevance(text, expected):
    assert parse_relevance(text) == expected


@respx.mock
def test_summarize_and_rate_end_to_end():
    respx.post(OLLAMA_CHAT).mock(return_value=_ollama_reply("Relevance: 5\n\nLocal summary."))

    out = Summarizer(settings=_ollama_settings()).summarize_and_rate("T", "A", "RLHF", "rlhf")

    assert (out.summary, out.relevance) == ("Local summary.", 5)


@respx.mock
def test_trailing_slash_in_base_url():
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
    assert settings.smtp_password == ""
    # Set by conftest's environment, so the developer's SUMMARY_BACKEND=ollama
    # in .env can't make tests call their real local model.
    assert settings.summary_backend == "none"


def test_summaries_enabled_by_backend():
    assert Settings(summary_backend="ollama").summaries_enabled is True
    assert Settings(summary_backend="none").summaries_enabled is False
