"""The interactive API docs (/docs, /redoc, /openapi.json) show the admin
API's shape, so production turns them off with API_DOCS=false; they stay on
locally."""
import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app

DOC_PATHS = ["/docs", "/redoc", "/openapi.json"]


@pytest.mark.parametrize("path", DOC_PATHS)
def test_docs_are_off_when_disabled(path):
    with TestClient(create_app(Settings(api_docs=False))) as c:
        assert c.get(path).status_code == 404


@pytest.mark.parametrize("path", DOC_PATHS)
def test_docs_are_on_by_default(path):
    with TestClient(create_app(Settings())) as c:
        assert c.get(path).status_code == 200


def test_health_still_answers_without_docs():
    with TestClient(create_app(Settings(api_docs=False))) as c:
        assert c.get("/health").json() == {"status": "ok"}
