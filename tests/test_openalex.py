"""The OpenAlex client (app.services.openalex): phrase topics only, kept work
types with abstracts, arXiv works keyed by their arXiv id, cursor paging and
a process-wide request gate. respx-mocked; no test reaches OpenAlex."""
from __future__ import annotations

import time
from datetime import date

import httpx
import pytest
import respx

from app.config import get_settings
from app.services.openalex import (
    is_phrase_query,
    normalize_title,
    rebuild_abstract,
    search_openalex,
    throttle,
)

WORKS = "https://api.openalex.org/works"
SINCE = date(2026, 11, 10)


@pytest.fixture(autouse=True)
def _fast(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "openalex_min_interval", 0.0)
    monkeypatch.setattr(settings, "openalex_api_key", "")
    monkeypatch.setattr(settings, "openalex_enabled", True)


def _work(n, *, work_type="article", abstract="Some words here", doi=None, is_oa=True, source=None, landing=None):
    return {
        "id": f"https://openalex.org/W{n}",
        "doi": f"https://doi.org/{doi}" if doi else None,
        "title": f"Work {n}",
        "abstract_inverted_index": (
            {w: [i] for i, w in enumerate(abstract.split())} if abstract else None
        ),
        "publication_date": "2026-11-11",
        "type": work_type,
        "primary_location": {
            "landing_page_url": landing or f"https://publisher.example/{n}",
            "source": source,
        },
        "best_oa_location": {"landing_page_url": f"https://oa.example/{n}"} if is_oa else None,
        "open_access": {"is_oa": is_oa},
    }


def _page(works, next_cursor=None):
    return httpx.Response(200, json={"meta": {"next_cursor": next_cursor}, "results": works})


def test_phrase_topics_only():
    assert is_phrase_query("physics-informed neural networks")
    assert not is_phrase_query("cat:math.NA")
    with respx.mock(assert_all_called=False) as router:
        route = router.get(WORKS).mock(return_value=_page([]))
        assert search_openalex("cat:math.NA", since=SINCE) == []
    assert not route.called


def test_disabled_makes_no_request(monkeypatch):
    monkeypatch.setattr(get_settings(), "openalex_enabled", False)
    with respx.mock(assert_all_called=False) as router:
        route = router.get(WORKS).mock(return_value=_page([]))
        assert search_openalex("spiking neural networks", since=SINCE) == []
    assert not route.called


@respx.mock
def test_request_filters_and_api_key(monkeypatch):
    route = respx.get(WORKS).mock(return_value=_page([]))
    search_openalex('"spiking neural networks"', since=SINCE)
    params = route.calls[0].request.url.params
    assert params["filter"] == (
        'title_and_abstract.search:"spiking neural networks",from_publication_date:2026-11-10,'
        "type:article|preprint|conference-paper,has_abstract:true"
    )
    assert params["sort"] == "publication_date:desc"
    assert "api_key" not in params and "mailto" not in params

    monkeypatch.setattr(get_settings(), "openalex_api_key", "oa-test-key")
    search_openalex("spiking neural networks", since=SINCE)
    assert route.calls[1].request.url.params["api_key"] == "oa-test-key"


@respx.mock
def test_keeps_articles_preprints_and_conference_papers_with_abstracts():
    respx.get(WORKS).mock(return_value=_page([
        _work(1, work_type="article"),
        _work(2, work_type="preprint"),
        _work(3, work_type="conference-paper"),
        _work(4, work_type="dataset"),
        _work(5, work_type="software"),
        _work(6, abstract=None),
    ]))
    got = search_openalex("spiking neural networks", since=SINCE)
    assert [r.openalex_id for r in got] == ["W1", "W2", "W3"]


@respx.mock
def test_an_arxiv_work_is_keyed_by_its_arxiv_id():
    respx.get(WORKS).mock(return_value=_page([_work(7, work_type="preprint", doi="10.48550/arXiv.2611.01234")]))
    [r] = search_openalex("spiking neural networks", since=SINCE)
    assert (r.key, r.arxiv_id) == ("2611.01234", "2611.01234")
    assert r.url == "https://arxiv.org/abs/2611.01234"
    assert r.open_access is None
    assert r.doi == "10.48550/arxiv.2611.01234"


@respx.mock
def test_other_works_are_keyed_by_openalex_id_with_landing_url():
    journal = {"display_name": "Journal of Spikes", "type": "journal"}
    repo = {"display_name": "Zenodo", "type": "repository"}
    respx.get(WORKS).mock(return_value=_page([
        _work(1, doi="10.1000/ABC", is_oa=True, source=journal),
        _work(2, is_oa=False, source=repo),
    ]))
    open_one, closed = search_openalex("spiking neural networks", since=SINCE)
    assert (open_one.key, open_one.arxiv_id, open_one.openalex_id) == ("openalex:W1", None, "W1")
    assert open_one.doi == "10.1000/abc"
    assert open_one.url == "https://oa.example/1"
    assert open_one.open_access is True
    assert open_one.journal_ref == "Journal of Spikes"
    assert open_one.abstract == "Some words here"
    assert open_one.work_type == "article"
    assert closed.url == "https://publisher.example/2"
    assert closed.open_access is False
    assert closed.journal_ref is None


@respx.mock
def test_pages_with_a_cursor_up_to_max_results(monkeypatch):
    monkeypatch.setattr(get_settings(), "openalex_max_results", 150)
    route = respx.get(WORKS).mock(side_effect=[
        _page([_work(i) for i in range(100)], next_cursor="c2"),
        _page([_work(i) for i in range(100, 200)], next_cursor="c3"),
    ])
    got = search_openalex("spiking neural networks", since=SINCE)
    assert len(got) == 150
    assert route.call_count == 2
    assert route.calls[0].request.url.params["cursor"] == "*"
    assert route.calls[1].request.url.params["cursor"] == "c2"


def test_requests_are_spaced_by_min_interval():
    throttle(0.05)
    start = time.monotonic()
    throttle(0.05)
    assert time.monotonic() - start >= 0.045


def test_rebuild_abstract_and_normalize_title():
    assert rebuild_abstract({"b": [1], "a": [0], "c": [2, 3]}) == "a b c c"
    assert rebuild_abstract(None) == ""
    assert normalize_title("Deep  Learning: A Survey!") == "deep learning a survey"
