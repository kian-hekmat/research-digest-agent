from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from app.config import Settings, get_settings
from app.routers import admin, topics, papers, digests, subscriptions, public


def create_app(settings: Settings | None = None) -> FastAPI:
    """The API: the public pages plus the token-protected admin JSON API.
    With API_DOCS=false (production) there is no /docs, /redoc or
    /openapi.json."""
    settings = settings or get_settings()
    docs = settings.api_docs
    app = FastAPI(
        title="Papers Weekly",
        description="Public signup pages and the admin API for papersweekly.com.",
        docs_url="/docs" if docs else None,
        redoc_url="/redoc" if docs else None,
        openapi_url="/openapi.json" if docs else None,
    )
    app.include_router(topics.router)
    app.include_router(papers.router)
    app.include_router(digests.router)
    app.include_router(subscriptions.router)
    app.include_router(admin.router)
    app.include_router(public.router)
    # The public pages' stylesheet and script (docs/frontend-plan.md).
    app.mount("/static", StaticFiles(directory=Path(__file__).resolve().parent / "static"), name="static")

    @app.get("/health")
    def health():
        return {"status": "ok"}

    return app


app = create_app()
