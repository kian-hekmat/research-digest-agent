"""Every OpenAI call the Summarizer makes is recorded with its purpose, and
the spend guard can stop it (app.services.spend). A fake ledger: no DB."""
import httpx
import pytest
import respx

from app.config import Settings
from app.services.spend import UnknownModelPrice, Usage
from app.services.summarize import RatedSummary, Summarizer

OPENAI_CHAT = "http://openai.test/v1/chat/completions"
USAGE = {"prompt_tokens": 500, "completion_tokens": 40, "prompt_tokens_details": {"cached_tokens": 0}}


class FakeLedger:
    def __init__(self, allow: bool = True) -> None:
        self._allow = allow
        self.asked: list[str] = []
        self.records: list[dict] = []
        self.hard_limits = 0

    def allow(self, purpose):
        self.asked.append(purpose)
        return self._allow

    def record(self, *, model, purpose, topic_id, usage):
        self.records.append({"model": model, "purpose": purpose, "topic_id": topic_id, "usage": usage})

    def hard_limit_hit(self):
        self.hard_limits += 1


def _settings(**overrides):
    values = {
        "summary_backend": "openai",
        "openai_api_key": "test-key",
        "openai_base_url": "http://openai.test/v1/",
        "openai_model": "gpt-5.4-mini",
    }
    return Settings(**{**values, **overrides})


def _reply(text, usage=USAGE):
    body = {"choices": [{"message": {"role": "assistant", "content": text}}]}
    if usage is not None:
        body["usage"] = usage
    return httpx.Response(200, json=body)


def _summarizer(ledger, **overrides):
    return Summarizer(settings=_settings(**overrides), ledger=ledger, topic_id="t1")


@respx.mock
@pytest.mark.parametrize(
    "call,purposes",
    [
        (lambda s: s.summarize_paper("T", "A"), ["summary"]),
        (lambda s: s.summarize_and_rate("T", "A", "RLHF", "rlhf"), ["summary"]),
        (lambda s: s.summarize_and_rate("T", "A", "RLHF", "rlhf", excerpts=["x"]), ["summary", "rating"]),
        (lambda s: s.rate_relevance("T", "A", "RLHF", "rlhf"), ["rating"]),
        (lambda s: s.write_overview("RLHF", ["s1", "s2"]), ["overview"]),
    ],
)
def test_every_openai_call_records_exactly_one_row_with_its_purpose(call, purposes):
    respx.post(OPENAI_CHAT).mock(return_value=_reply("Relevance: 8\n\nA summary."))
    ledger = FakeLedger()

    call(_summarizer(ledger))

    assert [r["purpose"] for r in ledger.records] == purposes
    assert ledger.asked == purposes
    for r in ledger.records:
        assert r["usage"] == Usage(500, 0, 40)
        assert r["topic_id"] == "t1"
        assert r["model"] == "gpt-5.4-mini"


@respx.mock
def test_blocked_purpose_makes_no_http_call_and_returns_no_text():
    route = respx.post(OPENAI_CHAT).mock(return_value=_reply("Relevance: 8\n\nA summary."))
    ledger = FakeLedger(allow=False)
    s = _summarizer(ledger)

    assert s.summarize_and_rate("T", "A", "RLHF", "rlhf") == RatedSummary(summary=None, relevance=None)
    assert s.summarize_and_rate("T", "A", "RLHF", "rlhf", excerpts=["x"]) == RatedSummary(
        summary=None, relevance=None
    )
    assert s.rate_relevance("T", "A", "RLHF", "rlhf") is None
    assert s.write_overview("RLHF", ["s1"]) is None
    assert s.summarize_paper("T", "A") is None
    assert not route.called
    assert ledger.records == []


@respx.mock
def test_spend_limit_429_returns_none_without_raising_and_reports_once():
    route = respx.post(OPENAI_CHAT).mock(
        return_value=httpx.Response(429, json={"error": {"code": "project_spend_limit_exceeded"}})
    )
    ledger = FakeLedger()

    assert _summarizer(ledger).rate_relevance("T", "A", "RLHF", "rlhf") is None

    assert route.call_count == 1
    assert ledger.hard_limits == 1
    assert ledger.records == []


@respx.mock
def test_spend_limit_429_on_summary_gives_no_text():
    respx.post(OPENAI_CHAT).mock(
        return_value=httpx.Response(429, json={"error": {"code": "organization_spend_limit_exceeded"}})
    )
    out = _summarizer(FakeLedger()).summarize_and_rate("T", "A", "RLHF", "rlhf")
    assert out == RatedSummary(summary=None, relevance=None)


@respx.mock
def test_other_429_still_raises():
    respx.post(OPENAI_CHAT).mock(
        return_value=httpx.Response(429, json={"error": {"code": "rate_limit_exceeded"}})
    )
    ledger = FakeLedger()
    with pytest.raises(httpx.HTTPStatusError):
        _summarizer(ledger).rate_relevance("T", "A", "RLHF", "rlhf")
    assert ledger.hard_limits == 0


@respx.mock
def test_usage_recorded_even_if_content_is_malformed():
    respx.post(OPENAI_CHAT).mock(return_value=httpx.Response(200, json={"choices": [], "usage": USAGE}))
    ledger = FakeLedger()

    with pytest.raises(IndexError):
        _summarizer(ledger).summarize_paper("T", "A")

    assert [r["purpose"] for r in ledger.records] == ["summary"]


@respx.mock
def test_missing_usage_is_estimated():
    respx.post(OPENAI_CHAT).mock(return_value=_reply("Relevance: 7", usage=None))
    ledger = FakeLedger()

    _summarizer(ledger).rate_relevance("T", "A", "RLHF", "rlhf")

    (record,) = ledger.records
    assert record["usage"].estimated is True
    assert record["usage"].output_tokens == 10  # rate_relevance's max tokens
    assert record["usage"].input_tokens > 0


@respx.mock
def test_unknown_model_fails_before_http():
    route = respx.post(OPENAI_CHAT).mock(return_value=_reply("x"))
    with pytest.raises(UnknownModelPrice):
        _summarizer(FakeLedger(), openai_model="gpt-9").summarize_paper("T", "A")
    assert not route.called


@respx.mock
def test_no_ledger_records_nothing_and_still_works():
    respx.post(OPENAI_CHAT).mock(return_value=_reply("A summary."))
    s = Summarizer(settings=_settings(openai_model="unpriced-test-model"))
    assert s.summarize_paper("T", "A") == "A summary."


@respx.mock
def test_ollama_backend_records_nothing():
    respx.post("http://ollama.test:11434/api/chat").mock(
        return_value=httpx.Response(200, json={"message": {"content": "A summary."}})
    )
    ledger = FakeLedger()
    s = Summarizer(
        settings=Settings(summary_backend="ollama", ollama_base_url="http://ollama.test:11434"),
        ledger=ledger,
    )
    assert s.summarize_paper("T", "A") == "A summary."
    assert ledger.records == [] and ledger.asked == []


@respx.mock
def test_out_of_credits_429_returns_none_and_reports():
    respx.post(OPENAI_CHAT).mock(
        return_value=httpx.Response(429, json={"error": {"code": "insufficient_quota"}})
    )
    ledger = FakeLedger()
    assert _summarizer(ledger).write_overview("RLHF", ["s1"]) is None
    assert ledger.hard_limits == 1
