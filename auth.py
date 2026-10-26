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

A session cookie is bound to the exact credential it was issued against:
it carries a keyed fingerprint of the account's password hash, and only
authenticates while that still matches. A new password always gets a new
salt, so changing it signs the account out on every device -- as does
deleting the account, even if the same username signs up again later. The
cost is one account read per request.

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
MIN_PASSWORD_LENGTH = 8
# Login and signup are anonymous and always run PBKDF2, so an unbounded
# password would let anyone make the worker hash an arbitrarily large
# string (a 50MB body OOMs a Consumption instance).
MAX_PASSWORD_LENGTH = 1024

_USERNAME_RE = re.compile(r"^[A-Za-z0-9_-]{3,50}$")


class AuthConfigError(Exception):
    """Raised when a required auth app setting isn't configured."""


class InvalidUsername(Exception):
    """Raised when a username fails basic format validation."""


class InvalidPassword(Exception):
    """Raised when a new password is too short or too long."""


def _now() -> int:
    return int(time.time())


def validate_username(username: str) -> str:
    """Validate and normalize (lowercase) a username."""
    if not _USERNAME_RE.match(username or ""):
        raise InvalidUsername(
            "Username must be 3-50 characters of letters, digits, '-' or '_'."
        )
    normalized = username.lower()
    if normalized in storage.RESERVED_TOKENS:
        # A username IS a watchlist's PartitionKey (see storage.py) -- these
        # values are reserved elsewhere in the same table, so letting one
        # through here would collide with them. storage.validate_token also
        # rejects them as defense-in-depth, but catching it here gives a
        # clear 400 at signup instead of a generic storage-level error.
        raise InvalidUsername(f"Username '{username}' is reserved.")
    return normalized


def validate_new_password(password: str) -> None:
    if not (MIN_PASSWORD_LENGTH <= len(password) <= MAX_PASSWORD_LENGTH):
        raise InvalidPassword(
            f"Password must be between {MIN_PASSWORD_LENGTH} and {MAX_PASSWORD_LENGTH} characters."
        )


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
    # Bytes, not str: compare_digest raises on non-ASCII strings, so a typo'd
    # code with, say, an iPhone smart dash would 500 instead of being refused.
    return hmac.compare_digest(code.encode("utf-8"), expected.encode("utf-8"))


def create_account(username: str, password: str) -> tuple[str, str]:
    """Create an account. Returns its normalized (lowercased) username and
    its password hash, for create_session_cookie()."""
    username = validate_username(username)
    salt = secrets.token_bytes(16)
    password_hash = _hash_password(password, salt, PBKDF2_ITERATIONS)
    storage.create_account(username, password_hash, salt.hex(), PBKDF2_ITERATIONS)
    return username, password_hash


def change_password(username: str, new_password: str, etag: str) -> str:
    """Set a new password, signing the account out everywhere. Returns the
    new password hash, for create_session_cookie().

    `etag` is that of the account row the caller verified (check_login) or
    read: the write only lands if the account is unchanged since, raising
    storage.AccountChanged otherwise. Without that, someone who knew the old
    password could change it in the moment after its owner did, overwriting
    the owner's new one.
    """
    validate_new_password(new_password)
    salt = secrets.token_bytes(16)
    password_hash = _hash_password(new_password, salt, PBKDF2_ITERATIONS)
    storage.set_password(username, password_hash, salt.hex(), PBKDF2_ITERATIONS, etag)
    return password_hash


def check_login(username: str, password: str) -> dict | None:
    """The account row (as storage.get_account() returns it, plus its
    normalized "username") if `password` is right for it, else None.

    Sessions and password changes must be based on exactly this row: a
    fresh read could see a password that changed while this one was being
    checked (the hash takes most of a second).
    """
    try:
        username = validate_username(username)
        account = storage.get_account(username)
    except (InvalidUsername, storage.UnknownAccount):
        # Still hash something so a nonexistent username doesn't respond
        # measurably faster than a wrong password (basic timing hygiene).
        _hash_password(password, b"\x00" * 16, PBKDF2_ITERATIONS)
        return None

    candidate = _hash_password(password, bytes.fromhex(account["salt"]), account["iterations"])
    if not hmac.compare_digest(candidate, account["password_hash"]):
        return None
    return {**account, "username": username}


def _sign(payload: str) -> str:
    signature = hmac.new(_session_secret(), payload.encode("utf-8"), hashlib.sha256).hexdigest()
    return f"{payload}.{signature}"


def _credential_fingerprint(password_hash: str) -> str:
    # Keyed (and domain-separated from cookie signing), so a cookie reveals
    # nothing about the password hash it's bound to.
    message = f"session-credential:{password_hash}".encode("utf-8")
    return hmac.new(_session_secret(), message, hashlib.sha256).hexdigest()[:32]


def create_session_cookie(username: str, password_hash: str) -> str:
    """Return a Set-Cookie header value establishing a new session for
    `username`, valid only while its password hash is still `password_hash`.

    Pass the hash that was just verified or written, never a fresh read --
    see check_login().
    """
    # username.fingerprint.expiry, joined with "." rather than ":": Azure
    # Functions' underlying host re-serializes a raw Set-Cookie header
    # through its own .NET cookie-writer, which percent-encodes ":" (and
    # anything else outside the URI "unreserved" set) but leaves "."
    # untouched -- and our parser below never URL-decodes what it reads back.
    fingerprint = _credential_fingerprint(password_hash)
    payload = f"{username}.{fingerprint}.{_now() + SESSION_LIFETIME_SECONDS}"
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
    correctly signed, unexpired, and still current for its account --
    otherwise None."""
    token = _extract_cookie(req)
    if not token:
        return None

    payload, sep, signature = token.rpartition(".")
    if not sep:
        return None
    parts = payload.split(".")
    if len(parts) != 3:
        return None
    username, fingerprint, expiry_str = parts
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
    if not hmac.compare_digest(signature.encode("utf-8"), expected_signature.encode("utf-8")):
        return None

    # Correctly signed isn't enough: the account must still exist, with the
    # same password as when this session began.
    try:
        account = storage.get_account(username)
    except (storage.UnknownAccount, storage.InvalidListToken):
        return None
    current = _credential_fingerprint(account["password_hash"])
    if not hmac.compare_digest(fingerprint.encode("utf-8"), current.encode("utf-8")):
        return None
    return username


_SESSION_ATTR = "_tvcal_session_username"


def is_authenticated(req: func.HttpRequest) -> bool:
    return get_username(req) is not None


def get_username(req: func.HttpRequest) -> str | None:
    """Return the signed-in username for a valid session, else None.

    Remembered on the request, since checking a session reads the account
    and both require_session and the handler ask.
    """
    try:
        return getattr(req, _SESSION_ATTR)
    except AttributeError:
        pass
    username = _parse_session_cookie(req)
    try:
        setattr(req, _SESSION_ATTR, username)
    except AttributeError:
        pass  # this request type can't hold it; it just gets checked again
    return username


def require_session(handler):
    """Route decorator: 401s unless a valid session cookie is present."""

    @functools.wraps(handler)
    def wrapper(req: func.HttpRequest) -> func.HttpResponse:
        if not is_authenticated(req):
            return func.HttpResponse("Not authenticated.", status_code=401)
        return handler(req)

    # Lets the test suite enumerate registered routes and prove every
    # non-public one is gated, instead of relying on a hand-kept list that
    # silently stays green when a new route forgets this decorator.
    wrapper.requires_session = True
    return wrapper
