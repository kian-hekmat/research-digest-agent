def test_create_topic(client):
    resp = client.post("/topics", json={"name": "RLHF", "query": "reinforcement learning human feedback"})
    assert resp.status_code == 201
    body = resp.json()
    assert body["name"] == "RLHF"
    assert "id" in body


def test_create_duplicate_topic_fails(client):
    payload = {"name": "RLHF", "query": "reinforcement learning human feedback"}
    first = client.post("/topics", json=payload)
    assert first.status_code == 201

    second = client.post("/topics", json=payload)
    assert second.status_code == 409


def test_list_topics(client):
    client.post("/topics", json={"name": "RLHF", "query": "rlhf"})
    client.post("/topics", json={"name": "Mech Interp", "query": "mechanistic interpretability"})

    resp = client.get("/topics")
    assert resp.status_code == 200
    names = {t["name"] for t in resp.json()}
    assert names == {"RLHF", "Mech Interp"}


def test_get_missing_topic_returns_404(client):
    resp = client.get("/topics/does-not-exist")
    assert resp.status_code == 404


def test_delete_topic(client):
    created = client.post("/topics", json={"name": "RLHF", "query": "rlhf"}).json()
    topic_id = created["id"]

    delete_resp = client.delete(f"/topics/{topic_id}")
    assert delete_resp.status_code == 204

    get_resp = client.get(f"/topics/{topic_id}")
    assert get_resp.status_code == 404
