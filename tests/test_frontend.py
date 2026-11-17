"""The frontend redesign (docs/frontend-plan.md): one stylesheet and one
script under /static, add-a-topic cards, 1-10 papers sliders, no labels."""
import re
from datetime import datetime, timezone
from pathlib import Path

import pytest

from app import models, signup, tokens
from tests.test_public_signup import _subscriber, form, sent, turnstile  # noqa: F401 (fixtures)

NOW = datetime.now(timezone.utc)
TEMPLATES = Path(__file__).resolve().parent.parent / "app" / "templates"


def _inputs(page: str, name_prefix: str) -> list[str]:
    return re.findall(rf'<input[^>]*name="{name_prefix}\d?"[^>]*>', page)


def _csrf(c):
    return re.search(r'name="csrf" value="([^"]+)"', c.get("/signup").text).group(1)


def _member(db, queries=("rlhf",)):
    _, sub = signup.record_signup(db, "me@example.com", [signup.SignupTopic(q, 5) for q in queries], NOW)
    signup.confirm_subscriber(db, sub, NOW, NOW)
    db.refresh(sub)
    return sub


# ---------- static files and the shared layout ----------
def test_static_stylesheet_and_script_are_served_without_a_token(anon_client):
    css = anon_client.get("/static/site.css")
    js = anon_client.get("/static/site.js")
    assert css.status_code == 200 and "text/css" in css.headers["content-type"]
    assert js.status_code == 200 and "javascript" in js.headers["content-type"]
    assert "prefers-color-scheme: dark" in css.text  # follows the OS theme


@pytest.mark.parametrize("path", ["/signup", "/terms", "/privacy", "/confirm?token=bad"])
def test_pages_link_the_stylesheet_script_and_fonts(anon_client, path):
    page = anon_client.get(path).text
    assert 'href="/static/site.css"' in page
    assert 'src="/static/site.js"' in page
    assert "fonts.googleapis.com" in page and "Source+Serif+4" in page and "Inter" in page
    assert '<header class="site-header">' in page and 'href="/signup"' in page


def test_every_page_template_extends_base():
    for path in TEMPLATES.glob("*.html"):
        if path.name in ("base.html",) or path.name.startswith("_"):
            continue
        assert path.read_text().startswith('{% extends "base.html" %}'), path.name


def test_privacy_mentions_google_fonts_and_no_labels(anon_client):
    text = anon_client.get("/privacy").text
    assert "Google Fonts" in text
    assert "labels" not in text


# ---------- signup page ----------
def test_signup_has_hero_and_how_it_works(anon_client):
    page = anon_client.get("/signup").text
    assert 'class="hero"' in page
    assert 'class="steps"' in page and page.count('class="step"') == 3


def test_signup_renders_five_topic_cards_with_sliders_and_no_labels(anon_client):
    page = anon_client.get("/signup").text
    assert len(_inputs(page, "topic_")) == 5
    sliders = _inputs(page, "papers_")
    assert len(sliders) == 5
    for s in sliders:
        assert 'type="range"' in s and 'min="1"' in s and 'max="10"' in s and 'value="5"' in s
    assert page.count("<output") == 5
    assert "label_" not in page
    assert page.count('class="topic-card"') == 5
    assert page.count('class="topic-preview" hidden') == 5  # reserved for the preview
    assert 'data-add-topic' in page and 'data-topic-count' in page
    assert page.count("data-remove-topic") == 5


def test_signup_keeps_the_chosen_papers_value_after_an_error(anon_client, db_session, turnstile, sent):
    resp = anon_client.post("/signup", data=form(anon_client, papers_0="3", us_resident=None))
    assert resp.status_code == 422
    slider = _inputs(resp.text, "papers_0")[0]
    assert 'value="3"' in slider


def test_signup_accepts_topics_with_gaps_between_rows(anon_client, db_session, turnstile, sent):
    resp = anon_client.post("/signup", data=form(
        anon_client, topic_0="reward modeling", topic_1="", papers_1="5",
        topic_2="diffusion language models", papers_2="2",
    ))
    assert resp.status_code == 200
    subs = {s.topic.normalized_query: s.max_papers for s in _subscriber(db_session, "new@example.com").subscriptions}
    assert subs == {"reward modeling": 5, "diffusion language models": 2}


@pytest.mark.parametrize("papers,ok", [("1", True), ("10", True), ("0", False), ("11", False)])
def test_signup_papers_range_is_1_to_10(anon_client, db_session, turnstile, sent, papers, ok):
    resp = anon_client.post("/signup", data=form(anon_client, papers_0=papers))
    assert resp.status_code == (200 if ok else 422)
    if not ok:
        assert "Choose between 1 and 10 papers." in resp.text


# ---------- labels are gone ----------
def test_labels_are_removed_from_code_and_model():
    assert not hasattr(signup, "parse_label")
    assert not hasattr(models.Subscription, "label")
    assert "label" not in signup.SignupTopic.__dataclass_fields__


def test_signup_ignores_a_stray_label_field(anon_client, db_session, turnstile, sent):
    resp = anon_client.post("/signup", data=form(anon_client, label_0="x" * 500))
    assert resp.status_code == 200


# ---------- manage page ----------
def test_manage_page_has_topic_cards_sliders_add_card_and_danger_zone(anon_client, db_session):
    me = _member(db_session, ("rlhf", "reward modeling"))
    page = anon_client.get("/manage", params={"token": tokens.manage_token(me)}).text
    assert page.count('class="topic-card"') == 3  # two topics and the add card
    sliders = _inputs(page, "papers")
    assert len(sliders) == 3
    for s in sliders:
        assert 'type="range"' in s and 'min="1"' in s and 'max="10"' in s
    assert 'name="label"' not in page
    assert 'class="danger-zone"' in page
    assert page.index('action="/manage/pause"') < page.index('action="/manage/delete"')


@pytest.mark.parametrize("papers,status", [("1", 200), ("0", 422), ("11", 422)])
def test_manage_update_papers_range_is_1_to_10(anon_client, db_session, papers, status):
    me = _member(db_session)
    topic_id = me.subscriptions[0].topic_id
    resp = anon_client.post("/manage/update", data={
        "token": tokens.manage_token(me), "csrf": _csrf(anon_client), "topic_id": topic_id, "papers": papers,
    })
    assert resp.status_code == status
    db_session.expire_all()
    assert db_session.get(models.Subscriber, me.id).subscriptions[0].max_papers == (1 if status == 200 else 5)


def test_manage_add_accepts_one_paper(anon_client, db_session):
    me = _member(db_session)
    resp = anon_client.post("/manage/add", data={
        "token": tokens.manage_token(me), "csrf": _csrf(anon_client), "topic": "reward modeling", "papers": "1",
    })
    assert resp.status_code == 200
    db_session.expire_all()
    subs = {s.topic.normalized_query: s.max_papers for s in db_session.get(models.Subscriber, me.id).subscriptions}
    assert subs["reward modeling"] == 1
