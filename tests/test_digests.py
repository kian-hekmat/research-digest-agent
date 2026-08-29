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
