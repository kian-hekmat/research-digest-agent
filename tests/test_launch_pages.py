"""The public pages' launch copy (launch plan v3, section 2): the signup
intro, one small stylesheet, and real terms and privacy pages."""
import pytest

ARXIV_THANKS = "Thank you to arXiv for use of its open access interoperability."


def test_signup_page_introduces_the_service(anon_client):
    page = anon_client.get("/signup").text
    assert "Papers Weekly" in page
    assert "Digests arrive every Monday morning for free." in page
    assert "AI-written" not in page  # owner's copy, 2026-11-17
    assert ARXIV_THANKS in page


@pytest.mark.parametrize("path", ["/signup", "/terms", "/privacy"])
def test_public_pages_share_one_phone_width_stylesheet(anon_client, path):
    page = anon_client.get(path).text
    assert '<meta name="viewport" content="width=device-width, initial-scale=1">' in page
    assert 'href="/static/site.css"' in page and "max-width" in anon_client.get("/static/site.css").text
    # Every page links the legal pages.
    assert 'href="/terms"' in page and 'href="/privacy"' in page


def test_terms_are_real(anon_client):
    page = anon_client.get("/terms")
    assert page.status_code == 200
    text = page.text
    assert "will be published before launch" not in text
    for phrase in ("United States", "13", "free", "AI", "may be wrong", "change or stop"):
        assert phrase in text, phrase
    assert "endorse" in text  # no implied arXiv endorsement


def test_privacy_policy_says_what_is_stored_and_who_processes_it(anon_client):
    page = anon_client.get("/privacy")
    assert page.status_code == 200
    text = page.text
    assert "will be published before launch" not in text
    for phrase in (
        "email address", "topics", "papers per topic", "send history",  # what's stored
        "hashed",  # rate limits keep IPs only as keyed hashes
        "Hetzner", "Resend", "OpenAI", "Cloudflare",  # processors
        "never your email address",  # what OpenAI sees
        "European Union",  # where it's stored
        "manage page", "delete",  # how to delete
    ):
        assert phrase in text, phrase


def test_privacy_policy_lists_a_contact_address_not_replies(anon_client):
    """Replies to digest@ aren't received, so the policy gives the owner's
    address instead (owner's call, 2026-11-17)."""
    text = anon_client.get("/privacy").text
    assert "reply to any digest" not in text
    assert 'href="mailto:kianhekmatnejad@gmail.com"' in text
