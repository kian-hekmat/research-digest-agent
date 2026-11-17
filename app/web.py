"""Shared plumbing for the public pages: templates, CSRF and
Origin checks, fail-closed on a missing secret, and Turnstile."""
from __future__ import annotations

import logging
from pathlib import Path

import httpx
from fastapi import HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from app.config import get_settings
from app.tokens import csrf_ok, csrf_token

logger = logging.getLogger(__name__)

# Jinja2Templates autoescapes .html templates.
templates = Jinja2Templates(directory=str(Path(__file__).resolve().parent / "templates"))

TURNSTILE_VERIFY_URL = "https://challenges.cloudflare.com/turnstile/v0/siteverify"


def render(request: Request, name: str, status: int = 200, **ctx) -> HTMLResponse:
    return templates.TemplateResponse(
        request,
        name,
        {"csrf": csrf_token(), "site_key": get_settings().turnstile_site_key, **ctx},
        status_code=status,
    )


def require_secret() -> None:
    """Dependency: public routes are off (503) until SECRET_KEY is set."""
    if not get_settings().secret_key:
        raise HTTPException(status_code=503, detail="Signup is not available yet")


def _own_origins(request: Request) -> set[str]:
    return {
        str(request.base_url).rstrip("/"),
        get_settings().public_base_url.rstrip("/"),
    }


def check_form(request: Request, csrf: str | None) -> None:
    """403 for a cross-site Origin or a missing/invalid CSRF field."""
    origin = request.headers.get("origin")
    if origin and origin.rstrip("/") not in _own_origins(request):
        raise HTTPException(status_code=403, detail="Cross-site form post refused")
    if not csrf_ok(csrf):
        raise HTTPException(status_code=403, detail="This form expired; reload the page and try again")


def client_ip(request: Request) -> str:
    # In production uvicorn runs with --proxy-headers, so behind Caddy this is
    # the visitor's address from X-Forwarded-For (docker-compose.prod.yml).
    return request.client.host if request.client else "unknown"


async def verify_turnstile(token: str | None, ip: str) -> bool:
    """Cloudflare's verdict on a Turnstile token; False on an empty secret,
    an empty token, or any error (fail closed). Async, so a slow Cloudflare
    never blocks the event loop."""
    secret = get_settings().turnstile_secret_key
    if not secret or not token:
        return False
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(
                TURNSTILE_VERIFY_URL,
                data={"secret": secret, "response": token, "remoteip": ip},
            )
        return bool(resp.json().get("success"))
    except (httpx.HTTPError, ValueError):
        logger.warning("Turnstile verification failed to complete", exc_info=True)
        return False
