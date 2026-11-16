"""Public pages (phase 3a): signup, the manage-link form, and the legal
placeholders. Plain server-rendered forms; Phase 4 styles them. Everything
that depends on whether an address exists runs in a BackgroundTask, so the
response can't tell new, pending and confirmed addresses apart."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, BackgroundTasks, Depends, Request
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool

from app import account_emails, models, signup, tokens, web
from app.config import get_settings
from app.database import SessionLocal, get_db
from app.ratelimit import allow, email_key, manage_link_ip_key, signup_ip_key
from app.temporal.client import get_temporal_client

router = APIRouter(dependencies=[Depends(web.require_secret)], include_in_schema=False)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _email_allowed(to: str, now: datetime) -> bool:
    db = SessionLocal()
    try:
        return allow(db, email_key(to), get_settings().account_emails_per_address_per_day, timedelta(days=1), now)
    finally:
        db.close()


async def _send_account_email(client, kind: str, sub_id: str, to: str, token: str, now: datetime) -> None:
    """Count it against the address's daily limit, then start the send. The
    database work runs in a thread so it never blocks the event loop."""
    if await run_in_threadpool(_email_allowed, to, now):
        path = "/confirm" if kind == "confirm" else "/manage"
        await account_emails.start_account_email(client, kind, sub_id, to, tokens.link(path, token), now)


def _record_signup(email: str, topics: list[signup.SignupTopic], now: datetime):
    db = SessionLocal()
    try:
        kind, sub = signup.record_signup(db, email, topics, now)
        token = tokens.confirm_token(sub) if kind == "confirm" else tokens.manage_token(sub)
        return kind, sub.id, sub.email, token
    finally:
        db.close()


async def _after_signup(client, email: str, topics: list[signup.SignupTopic], now: datetime) -> None:
    kind, sub_id, to, token = await run_in_threadpool(_record_signup, email, topics, now)
    await _send_account_email(client, kind, sub_id, to, token, now)


def _manage_link_target(email: str):
    db = SessionLocal()
    try:
        sub = signup.find_subscriber(db, email)
        if sub is None or not signup.is_active(sub):
            return None
        return sub.id, sub.email, tokens.manage_token(sub)
    finally:
        db.close()


async def _after_manage_link(client, email: str, now: datetime) -> None:
    target = await run_in_threadpool(_manage_link_target, email)
    if target is not None:
        sub_id, to, token = target
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
    if errors:
        return web.render(request, "signup.html", 422, errors=errors, form=form)
    now = _now()
    # Counted before Turnstile, so junk tokens can't be tried without limit.
    if not allow(db, signup_ip_key(ip), get_settings().signup_per_ip_per_hour, timedelta(hours=1), now):
        return web.render(request, "rate_limited.html", 429)
    if not await web.verify_turnstile(form.get("cf-turnstile-response"), ip):
        errors["turnstile"] = "Please complete the check below and try again."
        return web.render(request, "signup.html", 422, errors=errors, form=form)
    background.add_task(_after_signup, client, email, topics, now)
    return web.render(request, "check_inbox.html")


@router.post("/manage-link")
async def manage_link_submit(
    request: Request,
    background: BackgroundTasks,
    db: Session = Depends(get_db),
    client=Depends(get_temporal_client),
):
    form = dict(await request.form())
    web.check_form(request, form.get("csrf"))
    email = signup.parse_email(form.get("email"))
    if email is None:
        return web.render(request, "check_inbox.html", 422, error="Enter a valid email address.")
    now = _now()
    ip = web.client_ip(request)
    if not allow(db, manage_link_ip_key(ip), get_settings().signup_per_ip_per_hour, timedelta(hours=1), now):
        return web.render(request, "rate_limited.html", 429)
    background.add_task(_after_manage_link, client, email, now)
    return web.render(request, "check_inbox.html")


@router.get("/terms")
def terms(request: Request):
    return web.render(request, "legal.html", title="Terms of use")


@router.get("/privacy")
def privacy(request: Request):
    return web.render(request, "legal.html", title="Privacy policy")


def _expired(request: Request):
    return web.render(request, "expired.html", 400)


@router.get("/confirm")
def confirm_page(request: Request, token: str = "", db: Session = Depends(get_db)):
    try:
        tokens.subscriber_for(db, "confirm", token)
    except tokens.TokenError:
        return _expired(request)
    return web.render(request, "confirm.html", token=token)


@router.post("/confirm")
async def confirm_submit(request: Request, db: Session = Depends(get_db)):
    form = dict(await request.form())
    web.check_form(request, form.get("csrf"))
    try:
        sub, signed_at = tokens.subscriber_for(db, "confirm", form.get("token") or "")
    except tokens.TokenError:
        return _expired(request)
    reasons = signup.confirm_subscriber(db, sub, signed_at, _now())
    if reasons is None:
        return _expired(request)
    return web.render(request, "confirmed.html", reasons=reasons)


# ---------- manage page ----------
def _manage_page(request: Request, sub, token: str, status: int = 200, notice: list[str] | None = None, error: str | None = None):
    rows = sorted(sub.subscriptions, key=lambda s: s.topic.name)
    return web.render(request, "manage.html", status, token=token, subs=rows, notice=notice or [], error=error)


async def _manage_form(request: Request, db: Session):
    """(form, subscriber) for a manage POST; TokenError for a bad link."""
    form = dict(await request.form())
    web.check_form(request, form.get("csrf"))
    sub, _ = tokens.subscriber_for(db, "manage", form.get("token") or "")
    return form, sub


@router.get("/manage")
def manage_page(request: Request, token: str = "", db: Session = Depends(get_db)):
    try:
        sub, _ = tokens.subscriber_for(db, "manage", token)
    except tokens.TokenError:
        return _expired(request)
    return _manage_page(request, sub, token)


@router.post("/manage/{action}")
async def manage_action(action: str, request: Request, db: Session = Depends(get_db)):
    try:
        form, sub = await _manage_form(request, db)
    except tokens.TokenError:
        return _expired(request)
    token, now = form["token"], _now()
    if action == "update":
        error = signup.update_topic(db, sub, form.get("topic_id", ""), form.get("label"), form.get("papers"))
        return _manage_page(request, sub, token, 422 if error else 200, ["Saved."] if not error else [], error)
    if action == "remove":
        s = signup.subscription_for(sub, form.get("topic_id"))
        if s is not None:
            db.delete(s)
            db.commit()
            db.refresh(sub)
        return _manage_page(request, sub, token, notice=["Removed."])
    if action == "add":
        reasons, error = signup.add_topic(db, sub, form.get("topic", ""), form.get("label"), form.get("papers"), now)
        db.refresh(sub)
        return _manage_page(request, sub, token, 422 if error else 200, reasons or (["Added."] if not error else []), error)
    if action in ("pause", "resume"):
        reasons = signup.set_paused(db, sub, action == "pause", now)
        return _manage_page(request, sub, token, notice=reasons or [f"{action.capitalize()}d."])
    if action == "unsubscribe-all":
        sub.unsubscribed_at = sub.unsubscribed_at or now
        db.commit()
        return web.render(request, "unsubscribed.html", token=tokens.resubscribe_token(sub.id, None), topic=None)
    if action == "delete":
        db.delete(sub)
        db.commit()
        return web.render(request, "goodbye.html")
    return _expired(request)


# ---------- unsubscribe (RFC 8058 one-click) and re-subscribe ----------
def _unsubscribe_target(db: Session, token: str, purpose: str = "unsubscribe"):
    """(payload, subscriber or None, topic or None); TokenError for a bad link."""
    max_age = tokens.RESUBSCRIBE_MAX_AGE if purpose == "resubscribe" else None
    payload, _ = tokens.read_token(purpose, token, max_age)
    sid, tid = payload.get("s"), payload.get("t")
    sub = db.get(models.Subscriber, sid) if isinstance(sid, str) else None
    topic = db.get(models.Topic, tid) if isinstance(tid, str) else None
    return payload, sub, topic


@router.get("/unsubscribe")
def unsubscribe_page(request: Request, token: str = "", db: Session = Depends(get_db)):
    try:
        _, sub, topic = _unsubscribe_target(db, token)
    except tokens.TokenError:
        return _expired(request)
    if sub is None:
        return _expired(request)
    return web.render(request, "unsubscribe.html", token=token, topic=topic)


@router.post("/unsubscribe")
async def unsubscribe_submit(request: Request, token: str = "", db: Session = Depends(get_db)):
    """One-click: no CSRF and no cookies (mail clients post
    `List-Unsubscribe=One-Click` with the link's own token)."""
    if not token:
        token = (await request.form()).get("token") or ""
    try:
        payload, _, topic = _unsubscribe_target(db, token)
    except tokens.TokenError:
        return _expired(request)
    signup.unsubscribe(db, payload, _now())
    resubscribe = tokens.resubscribe_token(payload.get("s"), payload.get("t")) if isinstance(payload.get("s"), str) else ""
    return web.render(request, "unsubscribed.html", token=resubscribe, topic=topic)


@router.post("/resubscribe")
async def resubscribe_submit(request: Request, db: Session = Depends(get_db)):
    form = dict(await request.form())
    web.check_form(request, form.get("csrf"))
    token = form.get("token") or ""
    try:
        payload, sub, topic = _unsubscribe_target(db, token, "resubscribe")
    except tokens.TokenError:
        return _expired(request)
    if sub is None:
        return _expired(request)
    reasons, error = signup.resubscribe(db, sub, payload.get("t"), _now())
    return web.render(request, "resubscribed.html", 422 if error else 200, reasons=reasons, error=error, topic=topic)
