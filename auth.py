"""Cookie-based session auth gating the management UI/API.

Not tied to any external identity provider -- a single shared password
(hashed, stored as an app setting) protects write access to the app so a
stumbled-upon URL is useless without it. This deliberately does NOT gate
/calendar.ics: that URL is fetched by external calendar apps with no
cookies at all, and is already scoped by its own read-only feed token (see
storage.py) -- a cookie-based login can't apply there and shouldn't.
"""
from __future__ import annotations

import functools
import hashlib
import hmac
import os
import time

import azure.functions as func

COOKIE_NAME = "tvcal_session"
SESSION_LIFETIME_SECONDS = 30 * 24 * 60 * 60  # 30 days


class AuthConfigError(Exception):
    """Raised when a required auth app setting isn't configured."""


def _now() -> int:
    return int(time.time())


def _password_hash() -> str:
    value = os.environ.get("ACCESS_PASSWORD_HASH")
    if not value:
        raise AuthConfigError("ACCESS_PASSWORD_HASH app setting is not configured.")
    return value


def _session_secret() -> bytes:
    value = os.environ.get("SESSION_SECRET")
    if not value:
        raise AuthConfigError("SESSION_SECRET app setting is not configured.")
    return value.encode("utf-8")


def check_password(password: str) -> bool:
    candidate = hashlib.sha256(password.encode("utf-8")).hexdigest()
    return hmac.compare_digest(candidate, _password_hash())


def _sign(expiry: int) -> str:
    signature = hmac.new(_session_secret(), str(expiry).encode("utf-8"), hashlib.sha256).hexdigest()
    return f"{expiry}.{signature}"


def create_session_cookie() -> str:
    """Return a Set-Cookie header value establishing a new session."""
    token = _sign(_now() + SESSION_LIFETIME_SECONDS)
    return (
        f"{COOKIE_NAME}={token}; Path=/; HttpOnly; Secure; SameSite=Strict; "
        f"Max-Age={SESSION_LIFETIME_SECONDS}"
    )


def clear_session_cookie() -> str:
    """Return a Set-Cookie header value that clears the session cookie."""
    return f"{COOKIE_NAME}=; Path=/; HttpOnly; Secure; SameSite=Strict; Max-Age=0"


def _extract_cookie(req: func.HttpRequest) -> str | None:
    header = req.headers.get("Cookie") or req.headers.get("cookie")
    if not header:
        return None
    for part in header.split(";"):
        name, _, value = part.strip().partition("=")
        if name == COOKIE_NAME:
            return value
    return None


def is_authenticated(req: func.HttpRequest) -> bool:
    token = _extract_cookie(req)
    if not token or "." not in token:
        return False

    expiry_str, _, signature = token.partition(".")
    try:
        expiry = int(expiry_str)
    except ValueError:
        return False
    if expiry < _now():
        return False

    try:
        expected = _sign(expiry).rsplit(".", 1)[1]
    except AuthConfigError:
        # Fail closed: if the server isn't configured, no one is authenticated.
        return False
    return hmac.compare_digest(signature, expected)


def require_session(handler):
    """Route decorator: 401s unless a valid session cookie is present."""

    @functools.wraps(handler)
    def wrapper(req: func.HttpRequest) -> func.HttpResponse:
        if not is_authenticated(req):
            return func.HttpResponse("Not authenticated.", status_code=401)
        return handler(req)

    return wrapper
