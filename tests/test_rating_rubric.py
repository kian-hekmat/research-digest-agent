"""The audited rating rubric (rag-plan 5.10), adopted for the rate-only prompt."""
import json

import httpx
import respx

from app.config import Settings
from app.services import summarize as summarize_module
from app.services.summarize import Summarizer

OLLAMA_CHAT = "http://ollama.test:11434/api/chat"

CANDIDATE = (
    "You rate academic papers for a research alerting digest. "
    "Rate how central the reader's topic is to the paper, from 1 to 10. Judge by what the paper contributes, not by whether the topic's words appear in it.\n"
    "10: the topic is the paper's main subject - new methods, theory, analysis or results for the topic itself.\n"
    "8-9: a major focus - the main contribution directly advances or applies the topic.\n"
    "6-7: one of several substantial parts that a reader of this topic would likely want to see.\n"
    "4-5: the topic is used as a tool, a dataset, an application domain or one component of a larger system, and the paper's contribution lies elsewhere.\n"
    "2-3: the topic appears only as motivation, background, an example or a point of comparison.\n"
    "1: mentioned only in passing, or the words mean something else here than in the reader's field.\n"
    "Reply with only the integer."
)


def test_rate_only_prompt_is_the_audited_rubric():
    system = summarize_module._RELEVANCE_SYSTEM
    assert system == CANDIDATE
    assert system.startswith("You rate academic papers")
    assert system.endswith("\nReply with only the integer.")
    positions = [system.index(f"\n{band}:") for band in ("10", "8-9", "6-7", "4-5", "2-3", "1")]
    assert positions == sorted(positions)


def test_combined_prompt_keeps_its_evaluated_rubric():
    assert summarize_module._RELEVANCE_RUBRIC in summarize_module._RATED_SUMMARY_SYSTEM
    assert summarize_module._RELEVANCE_BANDS not in summarize_module._RATED_SUMMARY_SYSTEM


@respx.mock
def test_rate_relevance_sends_the_new_rubric():
    route = respx.post(OLLAMA_CHAT).mock(
        return_value=httpx.Response(200, json={"message": {"content": "7"}})
    )
    summarizer = Summarizer(Settings(summary_backend="ollama", ollama_base_url="http://ollama.test:11434"))

    assert summarizer.rate_relevance("T", "A.", "PINNs", "pinns") == 7

    system = json.loads(route.calls.last.request.content)["messages"][0]
    assert system["content"] == summarize_module._RELEVANCE_SYSTEM
