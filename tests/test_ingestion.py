from datetime import datetime, timezone
from types import SimpleNamespace

import httpx
import pytest
import respx

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
    respx.get("http://export.arxiv.org/api/query").mock(
        return_value=httpx.Response(200, text=ATOM)
    )

    results = search_arxiv("retrieval", delay=0)

    assert [r.arxiv_id for r in results] == ["2408.12345", "2408.00001"]
    assert results[0].title == "A Great Paper on Retrieval"
    assert results[0].abstract == "We show that retrieval helps. A lot."
    assert results[0].published_at == datetime(2024, 8, 22, 17, 59, tzinfo=timezone.utc)


@respx.mock
def test_search_arxiv_since_filter_drops_older_entries():
    respx.get("http://export.arxiv.org/api/query").mock(
        return_value=httpx.Response(200, text=ATOM)
    )

    cutoff = datetime(2024, 8, 10, tzinfo=timezone.utc)
    results = search_arxiv("retrieval", since=cutoff, delay=0)

    assert [r.arxiv_id for r in results] == ["2408.12345"]


@respx.mock
def test_search_arxiv_raises_on_http_error():
    respx.get("http://export.arxiv.org/api/query").mock(
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
    out = Summarizer(client=fake).summarize_paper("Some Title", "Some abstract.")

    assert out == "A tight summary."
    call = fake.messages.calls[0]
    assert call["model"] == "claude-haiku-4-5"
    assert call["max_tokens"] == 400
    assert "Some Title" in call["messages"][0]["content"]


def test_write_overview_numbers_the_summaries():
    fake = _FakeAnthropic("Synthesized overview.")
    out = Summarizer(client=fake).write_overview("RLHF", ["first point", "second point"])

    assert out == "Synthesized overview."
    call = fake.messages.calls[0]
    assert call["model"] == "claude-sonnet-5"
    content = call["messages"][0]["content"]
    assert "1. first point" in content and "2. second point" in content
