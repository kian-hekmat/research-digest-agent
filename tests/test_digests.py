import pytest

from tests.conftest import make_result


def test_trigger_digest_requires_valid_topic(client):
    resp = client.post("/digests/does-not-exist")
    assert resp.status_code == 404


def test_trigger_returns_pending_then_fetch(client):
    topic = client.post("/topics", json={"name": "RLHF", "query": "rlhf"}).json()

    digest = client.post(f"/digests/{topic['id']}").json()
    assert digest["status"] == "pending"  # response is serialized before the task runs
    assert digest["topic_id"] == topic["id"]

    fetched = client.get(f"/digests/{digest['id']}")
    assert fetched.status_code == 200
    assert fetched.json()["id"] == digest["id"]


def test_get_missing_digest_returns_404(client):
    resp = client.get("/digests/does-not-exist")
    assert resp.status_code == 404


def test_deleting_topic_cascades_to_its_digests(client, db_session):
    from app import models

    topic = client.post("/topics", json={"name": "RLHF", "query": "rlhf"}).json()
    digest = client.post(f"/digests/{topic['id']}").json()

    assert client.delete(f"/topics/{topic['id']}").status_code == 204

    db_session.expire_all()
    assert db_session.get(models.Digest, digest["id"]) is None


def test_digest_status_is_constrained_to_the_enum(db_session):
    from sqlalchemy.exc import DataError, IntegrityError
    from app import models

    topic = models.Topic(name="RLHF", query="rlhf")
    db_session.add(topic)
    db_session.commit()

    db_session.add(models.Digest(topic_id=topic.id, status="bogus"))
    with pytest.raises((DataError, IntegrityError, LookupError)):
        db_session.commit()
    db_session.rollback()


# ---------- Phase 2: ingestion + summarization ----------
def test_digest_ingests_summarizes_and_writes_overview(client, db_session, fake_fetcher):
    from app import models

    fake_fetcher.results = [
        make_result("2408.0001", "Paper One"),
        make_result("2408.0002", "Paper Two"),
    ]
    topic = client.post("/topics", json={"name": "RLHF", "query": "rlhf"}).json()

    digest_id = client.post(f"/digests/{topic['id']}").json()["id"]
    digest = client.get(f"/digests/{digest_id}").json()

    assert digest["status"] == "completed"
    assert digest["error"] is None
    assert {p["title"] for p in digest["papers"]} == {"Paper One", "Paper Two"}
    assert all(p["summary"] for p in digest["papers"])
    assert digest["overview"] == "Overview of RLHF: 2 papers."

    db_session.expire_all()
    assert db_session.query(models.Paper).count() == 2
    topic_row = db_session.get(models.Topic, topic["id"])
    assert topic_row.last_checked_at is not None


def test_digest_with_no_new_papers_completes_empty(client, fake_fetcher, fake_summarizer):
    fake_fetcher.results = []
    topic = client.post("/topics", json={"name": "RLHF", "query": "rlhf"}).json()

    digest_id = client.post(f"/digests/{topic['id']}").json()["id"]
    digest = client.get(f"/digests/{digest_id}").json()

    assert digest["status"] == "completed"
    assert digest["papers"] == []
    assert digest["overview"] is None
    assert digest["error"] is None
    assert fake_summarizer.overview_calls == []


def test_digest_fails_when_arxiv_fetch_errors(client, db_session, fake_fetcher):
    from app import models

    fake_fetcher.exc = RuntimeError("arxiv is down")
    topic = client.post("/topics", json={"name": "RLHF", "query": "rlhf"}).json()

    digest_id = client.post(f"/digests/{topic['id']}").json()["id"]
    digest = client.get(f"/digests/{digest_id}").json()

    assert digest["status"] == "failed"
    assert "arXiv fetch failed" in digest["error"]
    assert digest["papers"] == []

    db_session.expire_all()
    topic_row = db_session.get(models.Topic, topic["id"])
    assert topic_row.last_checked_at is None  # watermark not advanced on failure


def test_digest_tolerates_partial_summary_failure(client, db_session, fake_fetcher, fake_summarizer):
    from app import models

    fake_fetcher.results = [
        make_result("2408.0001", "Paper One"),
        make_result("2408.0002", "Paper Two"),
    ]
    fake_summarizer.fail_titles = {"Paper Two"}
    topic = client.post("/topics", json={"name": "RLHF", "query": "rlhf"}).json()

    digest_id = client.post(f"/digests/{topic['id']}").json()["id"]
    digest = client.get(f"/digests/{digest_id}").json()

    assert digest["status"] == "completed"
    assert digest["error"] == "1 of 2 paper summaries failed"
    assert digest["overview"] == "Overview of RLHF: 1 papers."

    by_title = {p["title"]: p for p in digest["papers"]}
    assert by_title["Paper One"]["summary"] == "Summary of Paper One."
    assert by_title["Paper Two"]["summary"] is None


def test_rerun_reuses_papers_and_passes_watermark(client, db_session, fake_fetcher):
    from app import models

    fake_fetcher.results = [
        make_result("2408.0001", "Paper One"),
        make_result("2408.0002", "Paper Two"),
    ]
    topic = client.post("/topics", json={"name": "RLHF", "query": "rlhf"}).json()

    first = client.post(f"/digests/{topic['id']}").json()["id"]
    second = client.post(f"/digests/{topic['id']}").json()["id"]

    db_session.expire_all()
    assert db_session.query(models.Paper).count() == 2  # deduped on arxiv_id

    second_digest = client.get(f"/digests/{second}").json()
    assert {p["title"] for p in second_digest["papers"]} == {"Paper One", "Paper Two"}

    # first run: no watermark; second run: watermark from the first
    assert fake_fetcher.calls[0][1] is None
    assert fake_fetcher.calls[1][1] is not None
    assert first != second
