"""Public pages (phase 3a): signup, the manage-link form, and the legal
placeholders. Plain server-rendered forms; Phase 4 styles them. Everything
that depends on whether an address exists runs in a BackgroundTask, so the
response can't tell new, pending and confirmed addresses apart."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, BackgroundTasks, Depends, Request
from sqlalchemy.orm import Session

from app import account_emails, signup, tokens, web
from app.config import get_settings
from app.database import SessionLocal, get_db
from app.ratelimit import allow, email_key, signup_ip_key
from app.temporal.client import get_temporal_client

router = APIRouter(dependencies=[Depends(web.require_secret)], include_in_schema=False)


def _now() -> datetime:
    return datetime.now(timezone.utc)


async def _send_account_email(client, kind: str, sub_id: str, to: str, token: str, now: datetime) -> None:
    """Count it against the address's daily limit, then start the send."""
    db = SessionLocal()
    try:
        ok = allow(db, email_key(to), get_settings().account_emails_per_address_per_day, timedelta(days=1), now)
    finally:
        db.close()
    if ok:
        path = "/confirm" if kind == "confirm" else "/manage"
        await account_emails.start_account_email(client, kind, sub_id, to, tokens.link(path, token), now)


async def _after_signup(client, email: str, topics: list[signup.SignupTopic], now: datetime) -> None:
    db = SessionLocal()
    try:
        kind, sub = signup.record_signup(db, email, topics, now)
        token = tokens.confirm_token(sub) if kind == "confirm" else tokens.manage_token(sub)
        sub_id, to = sub.id, sub.email
    finally:
        db.close()
    await _send_account_email(client, kind, sub_id, to, token, now)


async def _after_manage_link(client, email: str, now: datetime) -> None:
    db = SessionLocal()
    try:
        sub = signup.find_subscriber(db, email)
        if sub is None or not signup.is_active(sub):
            return
        token, sub_id, to = tokens.manage_token(sub), sub.id, sub.email
    finally:
        db.close()
    await _send_account_email(client, "manage", sub_id, to, token, now)


@router.get("/signup")
def signup_page(request: Request):
    return web.render(request, "signup.html", errors={}, form={})


@router.post("/signup")
async def signup_submit(
    request: Request,
    background: BackgroundTasks,
    db: Session = Depends(get_db),
    client=Depends(get_temporal_client),
):
    form = dict(await request.form())
    web.check_form(request, form.get("csrf"))
    ip = web.client_ip(request)
    email, topics, errors = signup.parse_signup(form)
    if not errors and not web.verify_turnstile(form.get("cf-turnstile-response"), ip):
        errors["turnstile"] = "Please complete the check below and try again."
    if errors:
        return web.render(request, "signup.html", 422, errors=errors, form=form)
    now = _now()
    if not allow(db, signup_ip_key(ip), get_settings().signup_per_ip_per_hour, timedelta(hours=1), now):
        return web.render(request, "rate_limited.html", 429)
    background.add_task(_after_signup, client, email, topics, now)
    return web.render(request, "check_inbox.html")


@router.post("/manage-link")
async def manage_link_submit(request: Request, background: BackgroundTasks, client=Depends(get_temporal_client)):
    form = dict(await request.form())
    web.check_form(request, form.get("csrf"))
    email = signup.parse_email(form.get("email"))
    if email is None:
        return web.render(request, "check_inbox.html", 422, error="Enter a valid email address.")
    background.add_task(_after_manage_link, client, email, _now())
    return web.render(request, "check_inbox.html")


@router.get("/terms")
def terms(request: Request):
    return web.render(request, "legal.html", title="Terms of use")


@router.get("/privacy")
def privacy(request: Request):
    return web.render(request, "legal.html", title="Privacy policy")
