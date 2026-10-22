"""Phase 2 retrieval: related-work notes (prompt, citation check, storage),
retrieval signals (novelty, near duplicates), and how emails show them."""
import json
import math
from datetime import datetime, timezone

import httpx
import pytest
import respx

from app import crud, models
from app.config import Settings, get_settings
from app.models import EMBEDDING_DIM
from app.services import summarize as summarize_module
from app.services.email import DigestForEmail, PaperForEmail, build_topic_section, render_digest_email
from app.services.retrieval import compute_signals, embed_missing, neighbours, related_context
from app.services.summarize import RatedSummary, RelatedPaper, Summarizer, parse_rated_summary
from app.temporal import activities as temporal_activities

OLLAMA_CHAT = "http://ollama.test:11434/api/chat"
MODEL = "fake-embed"


# ---------- citation check ----------
OFFERED = {"2609.01234", "2609.05678"}


def test_valid_related_line_is_split_off_and_kept():
    out = parse_rated_summary(
        "Relevance: 8\n\nA summary.\n\nRelated: Same sampling problem as [2609.01234], learned instead.", OFFERED
    )
    assert out.summary == "A summary."
    assert out.relevance == 8
    assert out.related_note == "Same sampling problem as [2609.01234], learned instead."
    assert out.related_ids == ("2609.01234",)
    assert not out.related_rejected


@pytest.mark.parametrize(
    "line",
    [
        "Related: Same problem as [2609.99999], other method.",  # not offered
        "Related: Same problem as [2609.01234] and [2501.00001], other method.",  # one of two not offered
        "Related: Similar in spirit to earlier work.",  # cites nothing
    ],
)
def test_related_line_failing_the_check_is_dropped_but_the_summary_kept(line):
    out = parse_rated_summary(f"Relevance: 6\n\nA summary.\n\n{line}", OFFERED)
    assert out.summary == "A summary."
    assert out.relevance == 6
    assert out.related_note is None and out.related_ids == ()
    assert out.related_rejected


def test_related_tacked_onto_the_paragraph_is_still_split_off():
    out = parse_rated_summary(
        "A summary ends here. Related: Same spectral-bias problem as [2609.01234], solved adversarially.", OFFERED
    )
    assert out.summary == "A summary ends here."
    assert out.related_ids == ("2609.01234",)


def test_a_bare_citation_is_not_a_note():
    out = parse_rated_summary("A summary ends here. Related: [2609.01234]", OFFERED)
    assert out.summary == "A summary ends here."
    assert out.related_note is None and out.related_rejected


def test_the_word_related_inside_a_sentence_is_left_alone():
    out = parse_rated_summary("Results on related tasks: strong. More text.", OFFERED)
    assert out.summary == "Results on related tasks: strong. More text."
    assert not out.related_rejected


def test_no_related_line_means_no_note():
    out = parse_rated_summary("Relevance: 6\n\nA summary.", OFFERED)
    assert (out.summary, out.related_note, out.related_rejected) == ("A summary.", None, False)


def test_without_offered_papers_a_related_line_is_not_parsed():
    # The pre-retrieval behavior, unchanged.
    out = parse_rated_summary("Relevance: 6\n\nA summary.\nRelated: x [2609.01234]")
    assert out.related_note is None
    assert "Related:" in out.summary


def test_duplicate_citations_collapse():
    out = parse_rated_summary("A summary.\nRelated: Both [2609.01234] and again [2609.01234] share a solver.", OFFERED)
    assert out.related_ids == ("2609.01234",)


# ---------- prompt ----------
def _settings():
    return Settings(summary_backend="ollama", ollama_base_url="http://ollama.test:11434")


def _reply(text):
    return httpx.Response(200, json={"message": {"role": "assistant", "content": text}})


@respx.mock
def test_related_papers_go_into_the_prompt_and_their_ids_are_checked():
    route = respx.post(OLLAMA_CHAT).mock(
        return_value=_reply("Relevance: 9\n\nNew summary.\n\nRelated: Same problem as [2609.01234], new method.")
    )
    related = [RelatedPaper("2609.01234", "Old Paper", "Old summary " * 200)]

    out = Summarizer(_settings()).summarize_and_rate("New", "New abstract.", "PINNs", "pinns", related=related)

    assert out.related_ids == ("2609.01234",)
    system, user = json.loads(route.calls.last.request.content)["messages"]
    assert system["content"] == summarize_module._RATED_SUMMARY_WITH_RELATED_SYSTEM
    assert "Earlier papers the reader has seen:\n[2609.01234] Old Paper: Old summary" in user["content"]
    assert user["content"].endswith("New paper:\nTitle: New\n\nAbstract: New abstract.")
    # Each earlier paper's text is clipped so it can't crowd out the new one.
    assert len(user["content"]) < 1200


@respx.mock
def test_no_related_papers_sends_exactly_the_pre_retrieval_prompt():
    route = respx.post(OLLAMA_CHAT).mock(return_value=_reply("Relevance: 5\n\nS."))

    Summarizer(_settings()).summarize_and_rate("T", "A.", "PINNs", "pinns", related=[])

    system, user = json.loads(route.calls.last.request.content)["messages"]
    assert system["content"] == summarize_module._RATED_SUMMARY_SYSTEM
    assert user["content"] == "Reader's topic: PINNs\n\nTitle: T\n\nAbstract: A."


# ---------- retrieval signals ----------
def _vec(angle_deg):
    v = [0.0] * EMBEDDING_DIM
    v[0], v[1] = math.cos(math.radians(angle_deg)), math.sin(math.radians(angle_deg))
    return v


class AngleEmbedder:
    enabled = True
    model = MODEL

    def embed(self, texts):
        return [_vec(float(t.rsplit("angle:", 1)[1])) for t in texts]


@pytest.fixture
def topic(db_session):
    t = models.Topic(name="PINNs", query="pinns")
    db_session.add(t)
    db_session.commit()
    return t


def _paper(db, arxiv_id, angle, day, topic=None, summary=None):
    p = models.Paper(
        arxiv_id=arxiv_id,
        title=f"Paper {arxiv_id}",
        abstract=f"angle:{angle}",
        summary=summary,
        published_at=datetime(2026, 9, day, tzinfo=timezone.utc),
    )
    if topic is not None:
        p.topics.append(topic)
    db.add(p)
    db.commit()
    return p


def _signals(db, ids=None):
    compute_signals(db, ids, model=MODEL, near_duplicate_similarity=0.9, novelty_window_days=90)


def test_unlinked_papers_are_never_retrieved(db_session, topic):
    q = _paper(db_session, "q", 0, 20, topic)
    _paper(db_session, "orphan", 1, 1)  # unlinked: off-topic history
    _paper(db_session, "linked", 30, 2, topic)
    embed_missing(db_session, AngleEmbedder())

    found = related_context(db_session, q.id, model=MODEL, k=4, min_similarity=0.0)

    assert [n.arxiv_id for n in found] == ["linked"]


def test_within_days_window(db_session, topic):
    q = _paper(db_session, "q", 0, 28, topic)
    _paper(db_session, "recent", 30, 20, topic)
    _paper(db_session, "old", 1, 1, topic)
    embed_missing(db_session, AngleEmbedder())

    found = neighbours(db_session, q.id, model=MODEL, published_before=True, within_days=10)

    assert [n.arxiv_id for n in found] == ["recent"]


def test_prior_similarity_is_the_closest_earlier_paper(db_session, topic):
    first = _paper(db_session, "first", 0, 1, topic)
    second = _paper(db_session, "second", 60, 5, topic)
    later = _paper(db_session, "later", 55, 9, topic)  # later: never "prior" to second
    embed_missing(db_session, AngleEmbedder())

    _signals(db_session)

    db_session.expire_all()
    assert db_session.get(models.Paper, first.id).prior_similarity is None
    assert db_session.get(models.Paper, second.id).prior_similarity == pytest.approx(math.cos(math.radians(60)), abs=1e-4)
    assert db_session.get(models.Paper, later.id).prior_similarity == pytest.approx(math.cos(math.radians(5)), abs=1e-4)


def test_near_duplicate_points_from_the_later_paper_to_the_earlier(db_session, topic):
    original = _paper(db_session, "2609.00001", 0, 1, topic)
    dup = _paper(db_session, "2609.00002", 5, 3, topic)  # cos 5deg = 0.996
    other = _paper(db_session, "2609.00003", 40, 4, topic)
    embed_missing(db_session, AngleEmbedder())

    _signals(db_session)

    db_session.expire_all()
    assert db_session.get(models.Paper, dup.id).near_duplicate_of_id == original.id
    assert db_session.get(models.Paper, original.id).near_duplicate_of_id is None
    assert db_session.get(models.Paper, other.id).near_duplicate_of_id is None


def test_similar_link_is_the_closest_earlier_paper_above_the_floor(db_session, topic):
    first = _paper(db_session, "first", 0, 1, topic)
    close = _paper(db_session, "close", 30, 5, topic)  # cos 30 = 0.87 to first
    far = _paper(db_session, "far", 80, 9, topic)  # cos 50 = 0.64 to close: below 0.75
    embed_missing(db_session, AngleEmbedder())

    compute_signals(db_session, None, model=MODEL, near_duplicate_similarity=0.95,
                    novelty_window_days=90, similar_paper_similarity=0.75)

    db_session.expire_all()
    assert [p.id for p in db_session.get(models.Paper, close.id).related_papers] == [first.id]
    assert db_session.get(models.Paper, far.id).related_papers == []
    assert db_session.get(models.Paper, first.id).related_papers == []


def test_signals_leave_an_llm_notes_citations_alone(db_session, topic):
    first = _paper(db_session, "first", 0, 1, topic)
    other = _paper(db_session, "other", 50, 2, topic)
    noted = _paper(db_session, "noted", 10, 5, topic)
    noted.related_note = "Same problem as [other], other method."
    crud.set_paper_related(db_session, noted.id, [(other.id, 0.6)])
    db_session.commit()
    embed_missing(db_session, AngleEmbedder())

    _signals(db_session)

    db_session.expire_all()
    assert [p.id for p in db_session.get(models.Paper, noted.id).related_papers] == [other.id]


def test_set_paper_related_replaces_previous_citations(db_session, topic):
    a, b, c = (_paper(db_session, x, 0, 1, topic) for x in ("a", "b", "c"))
    crud.set_paper_related(db_session, a.id, [(b.id, 0.7)])
    crud.set_paper_related(db_session, a.id, [(c.id, 0.8)])
    db_session.commit()
    db_session.expire_all()

    assert [p.arxiv_id for p in db_session.get(models.Paper, a.id).related_papers] == ["c"]


# ---------- the summarize activity ----------
class RecordingSummarizer:
    offered: list = []
    reply: RatedSummary | None = None

    def summarize_and_rate(self, title, abstract, topic_name, topic_query, related=None, rating_context=None, excerpts=None):
        type(self).offered = list(related or [])
        return type(self).reply


def test_summarize_activity_offers_earlier_papers_and_stores_checked_citations(db_session, topic, monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "summary_backend", "ollama")
    monkeypatch.setattr(settings, "embedding_model", MODEL)
    monkeypatch.setattr(settings, "rag_min_similarity", 0.5)
    monkeypatch.setattr(settings, "rag_related_notes", True)
    monkeypatch.setattr(temporal_activities, "Summarizer", RecordingSummarizer)
    earlier = _paper(db_session, "2609.00001", 0, 1, topic, summary="Earlier summary.")
    _paper(db_session, "2609.00002", 85, 2, topic)  # cos 65deg = 0.42: below the floor
    new = _paper(db_session, "2609.00009", 20, 9, topic)
    embed_missing(db_session, AngleEmbedder())
    RecordingSummarizer.reply = RatedSummary(
        summary="New summary.", relevance=8,
        related_note="Extends [2609.00001].", related_ids=("2609.00001",),
    )

    temporal_activities.summarize_paper(
        temporal_activities.SummarizePaperInput(
            paper_id=new.id, title=new.title, abstract=new.abstract,
            topic_id=topic.id, topic_name="PINNs", topic_query="pinns",
        )
    )

    assert [(r.arxiv_id, r.text) for r in RecordingSummarizer.offered] == [("2609.00001", "Earlier summary.")]
    db_session.expire_all()
    stored = db_session.get(models.Paper, new.id)
    assert (stored.summary, stored.related_note) == ("New summary.", "Extends [2609.00001].")
    assert [p.id for p in stored.related_papers] == [earlier.id]


def test_with_related_notes_off_nothing_is_offered_and_the_similar_link_stays(db_session, topic, monkeypatch):
    """The default: retrieval still runs (embed_papers records the closest
    earlier paper), but the summarizer gets the pre-retrieval prompt."""
    settings = get_settings()
    monkeypatch.setattr(settings, "summary_backend", "ollama")
    monkeypatch.setattr(settings, "embedding_model", MODEL)
    monkeypatch.setattr(temporal_activities, "Summarizer", RecordingSummarizer)
    earlier = _paper(db_session, "2609.00001", 0, 1, topic)
    new = _paper(db_session, "2609.00009", 10, 9, topic)
    embed_missing(db_session, AngleEmbedder())
    _signals(db_session)
    RecordingSummarizer.reply = RatedSummary(summary="S.", relevance=5)

    temporal_activities.summarize_paper(
        temporal_activities.SummarizePaperInput(paper_id=new.id, title="t", abstract="a", topic_id=topic.id)
    )

    assert RecordingSummarizer.offered == []
    db_session.expire_all()
    stored = db_session.get(models.Paper, new.id)
    assert stored.related_note is None
    assert [p.id for p in stored.related_papers] == [earlier.id]


def test_summarize_activity_without_embeddings_offers_nothing(db_session, topic, monkeypatch):
    monkeypatch.setattr(temporal_activities, "Summarizer", RecordingSummarizer)
    new = _paper(db_session, "2609.00009", 20, 9, topic)
    RecordingSummarizer.reply = RatedSummary(summary="S.", relevance=5)

    temporal_activities.summarize_paper(
        temporal_activities.SummarizePaperInput(paper_id=new.id, title="t", abstract="a", topic_id=topic.id)
    )

    assert RecordingSummarizer.offered == []
    db_session.expire_all()
    assert db_session.get(models.Paper, new.id).related_note is None


# ---------- email ----------
def _ep(arxiv_id, day=1, **kw):
    return PaperForEmail(
        title=f"Title {arxiv_id}", summary="S.", arxiv_id=arxiv_id,
        published_at=datetime(2026, 9, day, 14, tzinfo=timezone.utc), **kw,
    )


def _digest(papers):
    return DigestForEmail(generated_at=datetime(2026, 9, 30, tzinfo=timezone.utc), overview=None, papers=papers)


def test_near_duplicates_fold_under_their_original_and_skip_the_cap():
    papers = [
        _ep("2609.00001", relevance=9),
        _ep("2609.00002", relevance=9, near_duplicate_of="2609.00001"),
        _ep("2609.00003", relevance=9, near_duplicate_of="2609.00002"),  # chain
        _ep("2609.00004", relevance=5),
    ]

    section = build_topic_section("PINNs", [_digest(papers)], max_papers=2)

    assert [p.arxiv_id for p in section.papers] == ["2609.00001", "2609.00004"]
    assert [d.arxiv_id for d in section.papers[0].see_also] == ["2609.00002", "2609.00003"]
    assert section.omitted == 0
    text = render_digest_email([section]).text_body
    assert "Also (near-duplicate): Title 2609.00002 https://arxiv.org/abs/2609.00002" in text


def test_near_duplicate_of_a_paper_not_in_the_email_is_shown_normally():
    section = build_topic_section("PINNs", [_digest([_ep("2609.00002", near_duplicate_of="2609.00001")])])
    assert [p.arxiv_id for p in section.papers] == ["2609.00002"]


def test_similar_earlier_paper_shown_when_there_is_no_note():
    section = build_topic_section(
        "PINNs", [_digest([_ep("2609.00002", similar=[("2609.00001", "Old <Paper>")])])]
    )

    content = render_digest_email([section])

    assert "  Similar earlier paper: Old <Paper> https://arxiv.org/abs/2609.00001" in content.text_body
    assert (
        '<em>Similar earlier paper:</em> <a href="https://arxiv.org/abs/2609.00001">Old &lt;Paper&gt;</a>'
    ) in content.html_body


def test_note_takes_precedence_over_the_similar_link():
    section = build_topic_section(
        "PINNs",
        [_digest([_ep("2609.00002", related_note="Same problem as [2609.00001], new method.",
                      similar=[("2609.00001", "Old")])])],
    )
    text = render_digest_email([section]).text_body
    assert "Related: Same problem" in text and "Similar earlier paper" not in text


def test_related_note_rendered_with_linked_ids_in_html():
    section = build_topic_section(
        "PINNs", [_digest([_ep("2609.00002", related_note="Extends <the> sampler of [2609.00001].")])]
    )

    content = render_digest_email([section])

    assert "  Related: Extends <the> sampler of [2609.00001]." in content.text_body
    assert (
        '<em>Related:</em> Extends &lt;the&gt; sampler of '
        '[<a href="https://arxiv.org/abs/2609.00001">2609.00001</a>].'
    ) in content.html_body
