"""The phase 2d paper blurb: a why-read-it hook and what the paper did, as
structured output, shared across topics (no topic, no rating)."""
import json

import respx

from app.services.summarize import PaperBlurb, parse_paper_blurb
from tests.test_summarize_usage import OPENAI_CHAT, FakeLedger, _reply, _summarizer

VALID = '{"hook": " Halves  the cost ", "what": "They did X. It got 12%."}'


def test_parse_blurb_reads_both_fields():
    assert parse_paper_blurb(VALID) == PaperBlurb("Halves the cost", "They did X. It got 12%.")


def test_parse_blurb_accepts_a_code_fence():
    assert parse_paper_blurb(f"```json\n{VALID}\n```") == PaperBlurb("Halves the cost", "They did X. It got 12%.")


def test_parse_blurb_rejects_missing_or_empty_fields():
    for text in ('{"hook": "x"}', '{"hook": "", "what": "y"}', "not json", "[]"):
        assert parse_paper_blurb(text) is None, text


@respx.mock
def test_write_blurb_sends_title_and_abstract_and_records_summary():
    route = respx.post(OPENAI_CHAT).mock(return_value=_reply(VALID))
    ledger = FakeLedger()

    blurb = _summarizer(ledger).write_blurb("A Faster Solver", "We propose a solver that is 3x faster.")

    assert blurb == PaperBlurb("Halves the cost", "They did X. It got 12%.")
    body = json.loads(route.calls.last.request.content)
    system, user = (m["content"] for m in body["messages"])
    assert "A Faster Solver" in user and "3x faster" in user
    assert "Relevance" not in system and "topic" not in system.lower()
    assert body["response_format"]["type"] == "json_schema"
    assert [r["purpose"] for r in ledger.records] == ["summary"]


@respx.mock
def test_write_blurb_none_when_blocked_or_not_openai():
    assert _summarizer(FakeLedger(allow=False)).write_blurb("T", "A") is None
    assert _summarizer(FakeLedger(), summary_backend="ollama").write_blurb("T", "A") is None
    assert not respx.calls
