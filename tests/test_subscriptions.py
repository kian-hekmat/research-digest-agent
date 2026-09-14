def test_create_subscription(client):
    topic = client.post("/topics", json={"name": "RLHF", "query": "rlhf"}).json()

    resp = client.post(
        f"/topics/{topic['id']}/subscriptions",
        json={"email": "reader@example.com", "cadence": "weekly"},
    )

    assert resp.status_code == 201
    body = resp.json()
    assert body["email"] == "reader@example.com"
    assert body["cadence"] == "weekly"
    assert body["topic_id"] == topic["id"]
    assert body["active"] is True
    assert "last_sent_at" in body


def test_create_subscription_defaults_to_weekly(client):
    topic = client.post("/topics", json={"name": "RLHF", "query": "rlhf"}).json()

    resp = client.post(
        f"/topics/{topic['id']}/subscriptions", json={"email": "reader@example.com"}
    )

    assert resp.status_code == 201
    assert resp.json()["cadence"] == "weekly"


def test_create_subscription_for_missing_topic_returns_404(client):
    resp = client.post(
        "/topics/does-not-exist/subscriptions", json={"email": "reader@example.com"}
    )
    assert resp.status_code == 404


def test_create_subscription_rejects_invalid_email(client):
    topic = client.post("/topics", json={"name": "RLHF", "query": "rlhf"}).json()

    resp = client.post(
        f"/topics/{topic['id']}/subscriptions", json={"email": "not-an-email"}
    )

    assert resp.status_code == 422


def test_duplicate_subscription_for_same_topic_fails(client):
    topic = client.post("/topics", json={"name": "RLHF", "query": "rlhf"}).json()
    payload = {"email": "reader@example.com", "cadence": "biweekly"}

    first = client.post(f"/topics/{topic['id']}/subscriptions", json=payload)
    assert first.status_code == 201

    second = client.post(f"/topics/{topic['id']}/subscriptions", json=payload)
    assert second.status_code == 409


def test_same_email_can_subscribe_to_different_topics(client):
    rlhf = client.post("/topics", json={"name": "RLHF", "query": "rlhf"}).json()
    interp = client.post(
        "/topics", json={"name": "Mech Interp", "query": "mechanistic interpretability"}
    ).json()
    payload = {"email": "reader@example.com"}

    assert client.post(f"/topics/{rlhf['id']}/subscriptions", json=payload).status_code == 201
    assert client.post(f"/topics/{interp['id']}/subscriptions", json=payload).status_code == 201


def test_list_subscriptions_for_topic(client):
    topic = client.post("/topics", json={"name": "RLHF", "query": "rlhf"}).json()
    client.post(f"/topics/{topic['id']}/subscriptions", json={"email": "a@example.com"})
    client.post(f"/topics/{topic['id']}/subscriptions", json={"email": "b@example.com"})

    resp = client.get(f"/topics/{topic['id']}/subscriptions")

    assert resp.status_code == 200
    emails = {s["email"] for s in resp.json()}
    assert emails == {"a@example.com", "b@example.com"}


def test_list_subscriptions_for_missing_topic_returns_404(client):
    resp = client.get("/topics/does-not-exist/subscriptions")
    assert resp.status_code == 404


def test_delete_subscription(client):
    topic = client.post("/topics", json={"name": "RLHF", "query": "rlhf"}).json()
    sub = client.post(
        f"/topics/{topic['id']}/subscriptions", json={"email": "reader@example.com"}
    ).json()

    assert client.delete(f"/subscriptions/{sub['id']}").status_code == 204
    assert client.get(f"/topics/{topic['id']}/subscriptions").json() == []


def test_delete_missing_subscription_returns_404(client):
    resp = client.delete("/subscriptions/does-not-exist")
    assert resp.status_code == 404


def test_deleting_topic_cascades_to_its_subscriptions(client, db_session):
    from app import models

    topic = client.post("/topics", json={"name": "RLHF", "query": "rlhf"}).json()
    sub = client.post(
        f"/topics/{topic['id']}/subscriptions", json={"email": "reader@example.com"}
    ).json()

    assert client.delete(f"/topics/{topic['id']}").status_code == 204

    db_session.expire_all()
    assert db_session.get(models.Subscription, sub["id"]) is None
