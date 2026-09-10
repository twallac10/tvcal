"""Account-based session auth gating the management UI/API.

Real (if lightweight) accounts, not a single shared password: each person
signs up with their own username/password, so the same login reaches the
same watchlist from any device. There's still no external identity
provider -- accounts live in Table Storage (see storage.py) and passwords
are PBKDF2-HMAC-SHA256 hashed with a per-account salt, never stored in
plaintext.

Self-serve signup is gated by a shared SIGNUP_CODE app setting (plus
storage.MAX_ACCOUNTS as a hard backstop) so it's still just the people you
give the code to who can create accounts, not the whole internet. Rotate or
blank out SIGNUP_CODE once everyone who needs an account has one.

This deliberately does NOT gate /calendar.ics: that URL is fetched by
external calendar apps with no cookies at all, and is already scoped by its
own read-only feed token (see storage.py) -- a cookie-based login can't
apply there and shouldn't.
"""
from __future__ import annotations

import functools
import hashlib
import hmac
import os
import re
import secrets
import time

import azure.functions as func

import storage

COOKIE_NAME = "tvcal_session"
SESSION_LIFETIME_SECONDS = 30 * 24 * 60 * 60  # 30 days
PBKDF2_ITERATIONS = 600_000

_USERNAME_RE = re.compile(r"^[A-Za-z0-9_-]{3,50}$")


class AuthConfigError(Exception):
    """Raised when a required auth app setting isn't configured."""


class InvalidUsername(Exception):
    """Raised when a username fails basic format validation."""


def _now() -> int:
    return int(time.time())


def validate_username(username: str) -> str:
    """Validate and normalize (lowercase) a username."""
    if not _USERNAME_RE.match(username or ""):
        raise InvalidUsername(
            "Username must be 3-50 characters of letters, digits, '-' or '_'."
        )
    return username.lower()


def _session_secret() -> bytes:
    value = os.environ.get("SESSION_SECRET")
    if not value:
        raise AuthConfigError("SESSION_SECRET app setting is not configured.")
    return value.encode("utf-8")


def _hash_password(password: str, salt: bytes, iterations: int) -> str:
    return hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations).hex()


def check_signup_code(code: str) -> bool:
    expected = os.environ.get("SIGNUP_CODE")
    if not expected:
        raise AuthConfigError("SIGNUP_CODE app setting is not configured.")
    return hmac.compare_digest(code, expected)


def create_account(username: str, password: str) -> str:
    """Create an account, returning its normalized (lowercased) username."""
    username = validate_username(username)
    salt = secrets.token_bytes(16)
    password_hash = _hash_password(password, salt, PBKDF2_ITERATIONS)
    storage.create_account(username, password_hash, salt.hex(), PBKDF2_ITERATIONS)
    return username


def verify_login(username: str, password: str) -> bool:
    try:
        username = validate_username(username)
        account = storage.get_account(username)
    except (InvalidUsername, storage.UnknownAccount):
        # Still hash something so a nonexistent username doesn't respond
        # measurably faster than a wrong password (basic timing hygiene).
        _hash_password(password, b"\x00" * 16, PBKDF2_ITERATIONS)
        return False

    candidate = _hash_password(password, bytes.fromhex(account["salt"]), account["iterations"])
    return hmac.compare_digest(candidate, account["password_hash"])


def _sign(payload: str) -> str:
    signature = hmac.new(_session_secret(), payload.encode("utf-8"), hashlib.sha256).hexdigest()
    return f"{payload}.{signature}"


def create_session_cookie(username: str) -> str:
    """Return a Set-Cookie header value establishing a new session for `username`."""
    payload = f"{username}:{_now() + SESSION_LIFETIME_SECONDS}"
    token = _sign(payload)
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


def _parse_session_cookie(req: func.HttpRequest) -> str | None:
    """Return the session's username if the cookie is present, well-formed,
    correctly signed, and unexpired -- otherwise None."""
    token = _extract_cookie(req)
    if not token or "." not in token:
        return None

    payload, _, signature = token.rpartition(".")
    username, sep, expiry_str = payload.partition(":")
    if not sep:
        return None
    try:
        expiry = int(expiry_str)
    except ValueError:
        return None
    if expiry < _now():
        return None

    try:
        expected_signature = _sign(payload).rsplit(".", 1)[1]
    except AuthConfigError:
        # Fail closed: if the server isn't configured, no one is authenticated.
        return None
    if not hmac.compare_digest(signature, expected_signature):
        return None
    return username


def is_authenticated(req: func.HttpRequest) -> bool:
    return _parse_session_cookie(req) is not None


def get_username(req: func.HttpRequest) -> str | None:
    """Return the signed-in username for a valid session, else None."""
    return _parse_session_cookie(req)


def require_session(handler):
    """Route decorator: 401s unless a valid session cookie is present."""

    @functools.wraps(handler)
    def wrapper(req: func.HttpRequest) -> func.HttpResponse:
        if not is_authenticated(req):
            return func.HttpResponse("Not authenticated.", status_code=401)
        return handler(req)

    return wrapper
