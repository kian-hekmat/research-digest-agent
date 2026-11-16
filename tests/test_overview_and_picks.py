"""The phase 2d overview-and-picks call: one cited overview and up to two
picks from the top 5, validated against the papers actually offered."""
import json

import respx

from app.services.summarize import (
    OverviewPaper,
    Pick,
    parse_overview_and_picks,
)
from tests.test_summarize_usage import OPENAI_CHAT, FakeLedger, _reply, _summarizer

A, B, C, D, E, F = (f"2611.0000{i}" for i in range(1, 7))
OFFERED = [A, B, C, D, E, F]
TOP = [A, B, C, D, E]


def _reply_json(overview, picks=()):
    return json.dumps({"overview": overview, "picks": [{"arxiv_id": i, "hook": h} for i, h in picks]})


def _parse(overview, picks=()):
    return parse_overview_and_picks(_reply_json(overview, picks), OFFERED, TOP)


def test_offered_citations_kept_unknown_removed():
    r = _parse(f"X rose [{A}]. Y fell [2611.99999].")
    assert r.overview == f"X rose [{A}]. Y fell."
    assert r.dropped_citations == ["2611.99999"]


def test_version_suffix_counts_as_offered():
    r = _parse(f"Z doubled [{B}v3].")
    assert r.overview == f"Z doubled [{B}]."
    assert r.dropped_citations == []


def test_bare_unknown_id_removed_bare_offered_kept():
    r = _parse(f"See 2611.99999 and {C}.")
    assert r.overview == f"See and {C}."
    assert r.dropped_citations == ["2611.99999"]


def test_removing_all_citations_leaves_clean_prose():
    r = _parse("A gain [2611.99998], and [2611.99999] a loss .")
    assert r.overview == "A gain, and a loss."
    assert "[]" not in r.overview and "  " not in r.overview


def test_picks_only_from_top_deduped_capped_at_two():
    r = _parse("Fine.", [(F, "f"), (A, "a"), (A, "a again"), (B, "b"), (C, "c")])
    assert r.picks == [Pick(A, "a"), Pick(B, "b")]
    assert r.dropped_picks == [F]


def test_pick_with_empty_hook_dropped():
    r = _parse("Fine.", [(A, "  "), (B, "b")])
    assert r.picks == [Pick(B, "b")]


def test_fenced_json_parses():
    text = f"```json\n{_reply_json('Fine.', [(A, 'a')])}\n```"
    assert parse_overview_and_picks(text, OFFERED, TOP).picks == [Pick(A, "a")]


def test_invalid_or_missing_overview_is_none():
    for text in ("nope", '{"picks": []}', '{"overview": "", "picks": []}'):
        assert parse_overview_and_picks(text, OFFERED, TOP) is None, text


def _papers(n):
    return [
        OverviewPaper(f"2611.{i:05d}", f"Title {i}", "" if i == 2 else f"Abstract {i}.")
        for i in range(1, n + 1)
    ]


@respx.mock
def test_write_overview_and_picks_request_and_usage():
    route = respx.post(OPENAI_CHAT).mock(
        return_value=_reply(_reply_json("New results [2611.00001].", [("2611.00003", "c")]))
    )
    ledger = FakeLedger()

    r = _summarizer(ledger).write_overview_and_picks(
        "PINNs", _papers(26), previous=["Last week: " + "x" * 2000]
    )

    assert r.overview == "New results [2611.00001]." and r.picks == [Pick("2611.00003", "c")]
    body = json.loads(route.calls.last.request.content)
    user = body["messages"][1]["content"]
    for i in range(1, 26):
        assert f"[2611.{i:05d}] Title {i}" in user
    assert "2611.00026" not in user  # only the 25 best are offered
    assert "Title 2" in user  # an empty abstract is still offered by title
    assert "2611.00001, 2611.00002, 2611.00003, 2611.00004, 2611.00005" in user  # the pick pool
    assert "Last week:" in user and "x" * 2000 not in user  # history clipped
    assert body["response_format"]["type"] == "json_schema"
    assert [rec["purpose"] for rec in ledger.records] == ["overview"]


@respx.mock
def test_write_overview_and_picks_none_when_blocked_or_not_openai():
    assert _summarizer(FakeLedger(allow=False)).write_overview_and_picks("T", _papers(5)) is None
    assert _summarizer(FakeLedger(), summary_backend="ollama").write_overview_and_picks("T", _papers(5)) is None
    assert not respx.calls
