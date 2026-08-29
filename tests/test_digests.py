import pytest


def test_trigger_digest_requires_valid_topic(client):
    resp = client.post("/digests/does-not-exist")
    assert resp.status_code == 404


def test_trigger_and_fetch_digest(client):
    topic = client.post("/topics", json={"name": "RLHF", "query": "rlhf"}).json()

    digest = client.post(f"/digests/{topic['id']}").json()
    assert digest["status"] == "pending"
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
