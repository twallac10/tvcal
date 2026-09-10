import hashlib

import azure.functions as func
import pytest

import auth

TEST_PASSWORD = "correct horse battery staple"

# conftest.py's autouse bypass_auth fixture stubs out auth.is_authenticated
# for every test in the suite (most tests exercise business logic, not auth
# itself). This file specifically tests the real implementation, so restore
# it here -- module-local autouse fixtures run after conftest.py's.
_real_is_authenticated = auth.is_authenticated


@pytest.fixture(autouse=True)
def use_real_is_authenticated(monkeypatch):
    monkeypatch.setattr(auth, "is_authenticated", _real_is_authenticated)


@pytest.fixture(autouse=True)
def auth_settings(monkeypatch):
    monkeypatch.setenv(
        "ACCESS_PASSWORD_HASH", hashlib.sha256(TEST_PASSWORD.encode()).hexdigest()
    )
    monkeypatch.setenv("SESSION_SECRET", "test-secret-do-not-use-in-prod")


def _request_with_cookie(cookie_header=None):
    headers = {"Cookie": cookie_header} if cookie_header else {}
    return func.HttpRequest(method="GET", url="https://example.com/", headers=headers, body=b"")


def _cookie_token(set_cookie_header):
    # e.g. "tvcal_session=<expiry>.<sig>; Path=/; HttpOnly; ..." -> "<expiry>.<sig>"
    return set_cookie_header.split(";")[0].split("=", 1)[1]


def test_check_password_accepts_correct_password():
    assert auth.check_password(TEST_PASSWORD) is True


def test_check_password_rejects_wrong_password():
    assert auth.check_password("wrong") is False


def test_check_password_raises_when_unconfigured(monkeypatch):
    monkeypatch.delenv("ACCESS_PASSWORD_HASH", raising=False)
    with pytest.raises(auth.AuthConfigError):
        auth.check_password(TEST_PASSWORD)


def test_session_cookie_round_trips_to_authenticated():
    token = _cookie_token(auth.create_session_cookie())
    req = _request_with_cookie(f"{auth.COOKIE_NAME}={token}")

    assert auth.is_authenticated(req) is True


def test_is_authenticated_false_without_cookie():
    assert auth.is_authenticated(_request_with_cookie()) is False


def test_is_authenticated_false_with_tampered_signature():
    token = _cookie_token(auth.create_session_cookie())
    expiry, _, signature = token.partition(".")
    tampered = f"{expiry}.{'0' * len(signature)}"
    req = _request_with_cookie(f"{auth.COOKIE_NAME}={tampered}")

    assert auth.is_authenticated(req) is False


def test_is_authenticated_false_when_expired(monkeypatch):
    monkeypatch.setattr(auth, "_now", lambda: 1_000_000_000)
    token = _cookie_token(auth.create_session_cookie())

    monkeypatch.setattr(auth, "_now", lambda: 1_000_000_000 + auth.SESSION_LIFETIME_SECONDS + 1)
    req = _request_with_cookie(f"{auth.COOKIE_NAME}={token}")

    assert auth.is_authenticated(req) is False


def test_is_authenticated_false_when_session_secret_missing(monkeypatch):
    token = _cookie_token(auth.create_session_cookie())
    monkeypatch.delenv("SESSION_SECRET", raising=False)
    req = _request_with_cookie(f"{auth.COOKIE_NAME}={token}")

    assert auth.is_authenticated(req) is False


def test_cleared_session_cookie_is_not_authenticated():
    token = _cookie_token(auth.clear_session_cookie())
    req = _request_with_cookie(f"{auth.COOKIE_NAME}={token}")

    assert auth.is_authenticated(req) is False


def test_require_session_blocks_unauthenticated(monkeypatch):
    monkeypatch.setattr(auth, "is_authenticated", lambda req: False)

    @auth.require_session
    def handler(req):
        raise AssertionError("handler should not run")

    response = handler(_request_with_cookie())
    assert response.status_code == 401


def test_require_session_allows_authenticated(monkeypatch):
    monkeypatch.setattr(auth, "is_authenticated", lambda req: True)
    calls = []

    @auth.require_session
    def handler(req):
        calls.append(req)
        return "ok-marker"

    result = handler(_request_with_cookie())

    assert result == "ok-marker"
    assert len(calls) == 1
