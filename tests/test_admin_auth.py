import re
from uuid import uuid4

import pytest


def _admin_routes():
    from fastapi.routing import APIRoute

    from app.auth import PUBLIC_PATHS
    from app.main import app

    return [
        (m, r)
        for r in app.routes
        if isinstance(r, APIRoute) and r.path not in PUBLIC_PATHS
        for m in r.methods
    ]


def test_admin_routes_are_discovered():
    # Guards the loop below against passing vacuously.
    assert len(_admin_routes()) >= 10


@pytest.mark.parametrize("headers", [{}, {"X-Admin-Token": "wrong"}, {"X-Admin-Token": ""}])
def test_every_admin_route_rejects_missing_or_wrong_token(anon_client, headers):
    # json={} on every method: the check must fire before body validation
    # would answer 422.
    for method, route in _admin_routes():
        url = re.sub(r"\{[^}]+\}", str(uuid4()), route.path)
        resp = anon_client.request(method, url, headers=headers, json={})
        assert resp.status_code == 401, (method, route.path)


def test_valid_token_reaches_the_route(client):
    assert client.get("/topics").status_code == 200


def test_unset_token_locks_everything(anon_client, monkeypatch):
    from app.config import get_settings

    monkeypatch.setattr(get_settings(), "admin_token", "")
    assert anon_client.get("/topics", headers={"X-Admin-Token": ""}).status_code == 401


def test_health_is_public(anon_client):
    assert anon_client.get("/health").status_code == 200


def _field_names(model, seen=None):
    """All field names of a pydantic model, recursing through nested models."""
    from pydantic import BaseModel

    seen = seen if seen is not None else set()
    if model in seen:
        return set()
    seen.add(model)
    names = set()
    for name, info in model.model_fields.items():
        names.add(name)
        stack = [info.annotation]
        while stack:
            ann = stack.pop()
            if isinstance(ann, type) and issubclass(ann, BaseModel):
                names |= _field_names(ann, seen)
            else:
                stack.extend(getattr(ann, "__args__", ()))
    return names


def test_no_public_route_exposes_emails_descriptions_or_cadence():
    from fastapi.routing import APIRoute

    from app.auth import PUBLIC_PATHS
    from app.main import app

    forbidden = {"email", "description", "cadence"}
    public = [r for r in app.routes if isinstance(r, APIRoute) and r.path in PUBLIC_PATHS]
    assert public, "PUBLIC_PATHS matches no route"
    for route in public:
        models = []
        if route.response_model is not None:
            models.append(route.response_model)
        if route.body_field is not None:
            models.append(route.body_field.field_info.annotation)
        for m in models:
            if isinstance(m, type) and hasattr(m, "model_fields"):
                assert not (_field_names(m) & forbidden), (route.path, m)
