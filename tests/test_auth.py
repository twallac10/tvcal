import pytest

import auth
import storage

# conftest.py's autouse bypass_auth fixture stubs out auth.is_authenticated
# and auth.get_username for every test in the suite (most tests exercise
# business logic, not auth itself). This file specifically tests the real
# implementation, so restore both here -- module-local autouse fixtures run
# after conftest.py's.
_real_is_authenticated = auth.is_authenticated
_real_get_username = auth.get_username


@pytest.fixture(autouse=True)
def use_real_auth_checks(monkeypatch):
    monkeypatch.setattr(auth, "is_authenticated", _real_is_authenticated)
    monkeypatch.setattr(auth, "get_username", _real_get_username)


@pytest.fixture(autouse=True)
def auth_settings(monkeypatch):
    monkeypatch.setenv("SESSION_SECRET", "test-secret-do-not-use-in-prod")
    monkeypatch.setenv("SIGNUP_CODE", "letmein")


@pytest.fixture
def fake_accounts(monkeypatch):
    """In-memory stand-in for storage.create_account/get_account."""
    accounts = {}

    def fake_create_account(username, password_hash, salt, iterations):
        if username in accounts:
            raise storage.UsernameTaken(f"Username '{username}' is already taken.")
        accounts[username] = {
            "password_hash": password_hash,
            "salt": salt,
            "iterations": iterations,
        }

    def fake_get_account(username):
        if username not in accounts:
            raise storage.UnknownAccount(f"No account for username '{username}'.")
        return accounts[username]

    monkeypatch.setattr(storage, "create_account", fake_create_account)
    monkeypatch.setattr(storage, "get_account", fake_get_account)
    return accounts


def _request_with_cookie(cookie_header=None):
    import azure.functions as func

    headers = {"Cookie": cookie_header} if cookie_header else {}
    return func.HttpRequest(method="GET", url="https://example.com/", headers=headers, body=b"")


def _cookie_token(set_cookie_header):
    # e.g. "tvcal_session=<user>:<expiry>.<sig>; Path=/; HttpOnly; ..." -> "<user>:<expiry>.<sig>"
    return set_cookie_header.split(";")[0].split("=", 1)[1]


# -- usernames --------------------------------------------------------------


@pytest.mark.parametrize("username", ["bob", "Bob_2", "a-b-c", "abc"])
def test_validate_username_accepts_valid_formats(username):
    assert auth.validate_username(username) == username.lower()


@pytest.mark.parametrize("username", ["", "ab", "has spaces", "has/slash", "x" * 51])
def test_validate_username_rejects_invalid_formats(username):
    with pytest.raises(auth.InvalidUsername):
        auth.validate_username(username)


@pytest.mark.parametrize("username", ["__accounts__", "__feed_index__", "__meta__", "__ACCOUNTS__"])
def test_validate_username_rejects_reserved_names(username):
    # These are reserved Table Storage PartitionKeys/RowKeys elsewhere in
    # storage.py; letting an account claim one would corrupt other data.
    with pytest.raises(auth.InvalidUsername):
        auth.validate_username(username)


# -- accounts -----------------------------------------------------------


def test_create_account_and_verify_login_round_trip(fake_accounts):
    auth.create_account("Alice", "hunter2pass")

    assert auth.verify_login("alice", "hunter2pass") is True
    assert auth.verify_login("Alice", "hunter2pass") is True  # case-insensitive


def test_verify_login_rejects_wrong_password(fake_accounts):
    auth.create_account("alice", "hunter2pass")
    assert auth.verify_login("alice", "wrong") is False


def test_verify_login_rejects_unknown_username(fake_accounts):
    assert auth.verify_login("nobody", "whatever") is False


def test_create_account_rejects_invalid_username(fake_accounts):
    with pytest.raises(auth.InvalidUsername):
        auth.create_account("ab", "hunter2pass")


def test_create_account_normalizes_to_lowercase(fake_accounts):
    normalized = auth.create_account("Alice", "hunter2pass")
    assert normalized == "alice"
    assert "alice" in fake_accounts
    assert "Alice" not in fake_accounts


def test_create_account_raises_username_taken(fake_accounts):
    auth.create_account("alice", "hunter2pass")
    with pytest.raises(storage.UsernameTaken):
        auth.create_account("ALICE", "different-password")


# -- signup code --------------------------------------------------------


def test_check_signup_code_accepts_correct_code():
    assert auth.check_signup_code("letmein") is True


def test_check_signup_code_rejects_wrong_code():
    assert auth.check_signup_code("nope") is False


def test_check_signup_code_raises_when_unconfigured(monkeypatch):
    monkeypatch.delenv("SIGNUP_CODE", raising=False)
    with pytest.raises(auth.AuthConfigError):
        auth.check_signup_code("anything")


# -- session cookie -------------------------------------------------------


def test_session_cookie_round_trips_to_username():
    token = _cookie_token(auth.create_session_cookie("alice"))
    req = _request_with_cookie(f"{auth.COOKIE_NAME}={token}")

    assert auth.is_authenticated(req) is True
    assert auth.get_username(req) == "alice"


def test_is_authenticated_false_without_cookie():
    assert auth.is_authenticated(_request_with_cookie()) is False
    assert auth.get_username(_request_with_cookie()) is None


def test_is_authenticated_false_with_tampered_signature():
    token = _cookie_token(auth.create_session_cookie("alice"))
    payload, _, signature = token.rpartition(".")
    tampered = f"{payload}.{'0' * len(signature)}"
    req = _request_with_cookie(f"{auth.COOKIE_NAME}={tampered}")

    assert auth.is_authenticated(req) is False
    assert auth.get_username(req) is None


def test_is_authenticated_false_when_expired(monkeypatch):
    monkeypatch.setattr(auth, "_now", lambda: 1_000_000_000)
    token = _cookie_token(auth.create_session_cookie("alice"))

    monkeypatch.setattr(auth, "_now", lambda: 1_000_000_000 + auth.SESSION_LIFETIME_SECONDS + 1)
    req = _request_with_cookie(f"{auth.COOKIE_NAME}={token}")

    assert auth.is_authenticated(req) is False


def test_is_authenticated_false_when_session_secret_missing(monkeypatch):
    token = _cookie_token(auth.create_session_cookie("alice"))
    monkeypatch.delenv("SESSION_SECRET", raising=False)
    req = _request_with_cookie(f"{auth.COOKIE_NAME}={token}")

    assert auth.is_authenticated(req) is False


def test_cleared_session_cookie_is_not_authenticated():
    token = _cookie_token(auth.clear_session_cookie())
    req = _request_with_cookie(f"{auth.COOKIE_NAME}={token}")

    assert auth.is_authenticated(req) is False


def test_two_accounts_get_independent_sessions():
    alice_token = _cookie_token(auth.create_session_cookie("alice"))
    bob_token = _cookie_token(auth.create_session_cookie("bob"))

    assert auth.get_username(_request_with_cookie(f"{auth.COOKIE_NAME}={alice_token}")) == "alice"
    assert auth.get_username(_request_with_cookie(f"{auth.COOKIE_NAME}={bob_token}")) == "bob"


# -- require_session ------------------------------------------------------


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
