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


def _names_in(annotation):
    """Field names of every pydantic model in `annotation`, unwrapping
    list[...], Optional[...] and the like."""
    from pydantic import BaseModel

    if isinstance(annotation, type) and issubclass(annotation, BaseModel):
        return _field_names(annotation)
    names = set()
    for arg in getattr(annotation, "__args__", ()):
        names |= _names_in(arg)
    return names


def test_public_routes_never_return_emails_descriptions_or_cadence_nor_accept_cadence_or_descriptions():
    """Nothing public *returns* an email, a topic description or a cadence,
    or *accepts* a cadence or a description. Public routes may accept an
    email (Phase 3's signup must)."""
    from fastapi.routing import APIRoute

    from app.auth import PUBLIC_PATHS
    from app.main import app

    never_returned = {"email", "description", "cadence"}
    never_accepted = {"cadence", "description"}
    public = [r for r in app.routes if isinstance(r, APIRoute) and r.path in PUBLIC_PATHS]
    assert public, "PUBLIC_PATHS matches no route"
    for route in public:
        if route.response_model is not None:
            returned = _names_in(route.response_model)
            assert not (returned & never_returned), (route.path, "response", returned & never_returned)
        if route.body_field is not None:
            accepted = _names_in(route.body_field.field_info.annotation)
            assert not (accepted & never_accepted), (route.path, "body", accepted & never_accepted)
