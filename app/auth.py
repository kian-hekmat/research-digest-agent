import hmac

from fastapi import Header, HTTPException

from app.config import get_settings

# Routes served without the admin token (the public pages). Everything else is
# admin-only via the routers' dependencies. A test checks that nothing listed
# here returns emails, descriptions or cadence, or accepts a cadence or
# description.
PUBLIC_PATHS = {
    "/health", "/signup", "/manage-link", "/terms", "/privacy", "/confirm",
    "/manage", "/manage/{action}", "/unsubscribe", "/resubscribe",
}


def require_admin(x_admin_token: str | None = Header(default=None)) -> None:
    # Read at call time, not import time, so the cached Settings can be changed
    # after import. An unset (empty) token rejects everything: fail closed.
    expected = get_settings().admin_token
    if not expected or x_admin_token is None:
        raise HTTPException(status_code=401, detail="Admin token required")
    # Compare bytes: compare_digest raises TypeError on non-ASCII str.
    if not hmac.compare_digest(x_admin_token.encode(), expected.encode()):
        raise HTTPException(status_code=401, detail="Admin token required")
