import string

import pytest

import auth
import storage

_URI_UNRESERVED = frozenset(string.ascii_letters + string.digits + "-._~")

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
def fake_accounts(fake_client):
    """Accounts in the real storage code, over the in-memory table."""
    return fake_client


@pytest.fixture
def accounts(fake_client):
    """Existing accounts to hold sessions for -- created straight in storage
    rather than through auth.create_account, which would spend real PBKDF2
    time on each."""
    for username in ("alice", "bob", "alice-under_score"):
        storage.create_account(username, f"hash-of-{username}", "00", 1)
    return fake_client


def _cookie_for(username):
    """A Set-Cookie for `username`'s current password, as login issues it."""
    return auth.create_session_cookie(username, storage.get_account(username)["password_hash"])


def _change_password(username, new_password):
    return auth.change_password(username, new_password, storage.get_account(username)["etag"])


def _request_with_cookie(cookie_header=None):
    import azure.functions as func

    headers = {"Cookie": cookie_header} if cookie_header else {}
    return func.HttpRequest(method="GET", url="https://example.com/", headers=headers, body=b"")


def _cookie_token(set_cookie_header):
    # "tvcal_session=<user>.<fingerprint>.<expiry>.<sig>; Path=/; ..." -> "<user>.<fingerprint>.<expiry>.<sig>"
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


def test_create_account_and_check_login_round_trip(fake_accounts):
    _, password_hash = auth.create_account("Alice", "hunter2pass")

    account = auth.check_login("Alice", "hunter2pass")  # case-insensitive
    assert account["username"] == "alice"
    assert account["password_hash"] == password_hash


def test_check_login_rejects_wrong_password(fake_accounts):
    auth.create_account("alice", "hunter2pass")
    assert auth.check_login("alice", "wrong") is None


def test_check_login_rejects_unknown_username(fake_accounts):
    assert auth.check_login("nobody", "whatever") is None


def test_create_account_rejects_invalid_username(fake_accounts):
    with pytest.raises(auth.InvalidUsername):
        auth.create_account("ab", "hunter2pass")


def test_create_account_normalizes_to_lowercase(fake_accounts):
    normalized, _ = auth.create_account("Alice", "hunter2pass")
    assert normalized == "alice"
    assert ("__accounts__", "alice") in fake_accounts.rows
    assert ("__accounts__", "Alice") not in fake_accounts.rows


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


def test_session_cookie_value_contains_only_uri_unreserved_characters(accounts):
    # Azure Functions' host re-serializes Set-Cookie through its own .NET
    # cookie writer, which percent-encodes anything outside the URI
    # "unreserved" set (letters, digits, -._~) -- e.g. ":" silently became
    # "%3A" in production, and our own parser never URL-decodes what it
    # reads back, so a session could be issued (200/204) yet never actually
    # authenticate on any later request. Any future change to the cookie
    # format must keep every character in this safe set.
    token = _cookie_token(_cookie_for("alice-under_score"))
    assert set(token) <= _URI_UNRESERVED


def test_session_cookie_round_trips_to_username(accounts):
    token = _cookie_token(_cookie_for("alice"))
    req = _request_with_cookie(f"{auth.COOKIE_NAME}={token}")

    assert auth.is_authenticated(req) is True
    assert auth.get_username(req) == "alice"


def test_is_authenticated_false_without_cookie():
    assert auth.is_authenticated(_request_with_cookie()) is False
    assert auth.get_username(_request_with_cookie()) is None


def test_is_authenticated_false_with_tampered_signature(accounts):
    token = _cookie_token(_cookie_for("alice"))
    payload, _, signature = token.rpartition(".")
    tampered = f"{payload}.{'0' * len(signature)}"
    req = _request_with_cookie(f"{auth.COOKIE_NAME}={tampered}")

    assert auth.is_authenticated(req) is False
    assert auth.get_username(req) is None


def test_is_authenticated_false_when_expired(accounts, monkeypatch):
    monkeypatch.setattr(auth, "_now", lambda: 1_000_000_000)
    token = _cookie_token(_cookie_for("alice"))

    monkeypatch.setattr(auth, "_now", lambda: 1_000_000_000 + auth.SESSION_LIFETIME_SECONDS + 1)
    req = _request_with_cookie(f"{auth.COOKIE_NAME}={token}")

    assert auth.is_authenticated(req) is False


def test_is_authenticated_false_when_session_secret_missing(accounts, monkeypatch):
    token = _cookie_token(_cookie_for("alice"))
    monkeypatch.delenv("SESSION_SECRET", raising=False)
    req = _request_with_cookie(f"{auth.COOKIE_NAME}={token}")

    assert auth.is_authenticated(req) is False


def test_cleared_session_cookie_is_not_authenticated():
    token = _cookie_token(auth.clear_session_cookie())
    req = _request_with_cookie(f"{auth.COOKIE_NAME}={token}")

    assert auth.is_authenticated(req) is False


def test_two_accounts_get_independent_sessions(accounts):
    alice_token = _cookie_token(_cookie_for("alice"))
    bob_token = _cookie_token(_cookie_for("bob"))

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


def test_a_cookie_with_a_non_ascii_signature_is_rejected_not_500ed():
    req = _request_with_cookie("tvcal_session=alice.9999999999.é")
    assert auth.get_username(req) is None



# -- sessions bound to the password ---------------------------------------


def _session_request(username):
    token = _cookie_token(_cookie_for(username))
    return lambda: _request_with_cookie(f"{auth.COOKIE_NAME}={token}")


def test_changing_the_password_signs_out_existing_sessions(accounts):
    old_session = _session_request("alice")
    assert auth.get_username(old_session()) == "alice"

    _change_password("alice", "a-brand-new-password")

    assert auth.get_username(old_session()) is None
    assert auth.get_username(_session_request("alice")()) == "alice"  # a new one works


def test_changing_one_accounts_password_leaves_other_accounts_signed_in(accounts):
    bob = _session_request("bob")
    _change_password("alice", "a-brand-new-password")
    assert auth.get_username(bob()) == "bob"


def test_a_deleted_accounts_sessions_stop_working(accounts):
    session = _session_request("alice")
    accounts.delete_entity("__accounts__", "alice")
    assert auth.get_username(session()) is None


def test_recreating_a_deleted_account_does_not_revive_its_old_sessions(fake_accounts):
    # Deleting the account row and signing up again is how a forgotten
    # password used to be handled; the old account's cookies must stay dead.
    auth.create_account("alice", "first-password")
    old_session = _session_request("alice")
    fake_accounts.delete_entity("__accounts__", "alice")

    auth.create_account("alice", "first-password")  # even with the same password

    assert auth.get_username(old_session()) is None


def test_a_cookie_in_an_older_format_is_rejected(accounts):
    # Correctly signed, but in formats earlier builds used: username.expiry,
    # and username.version.expiry.
    for payload in (f"alice.{auth._now() + 60}", f"alice.0.{auth._now() + 60}"):
        req = _request_with_cookie(f"{auth.COOKIE_NAME}={auth._sign(payload)}")
        assert auth.get_username(req) is None, payload


def test_a_cookie_never_reveals_the_password_hash(accounts):
    token = _cookie_token(_cookie_for("alice"))
    assert "hash-of-alice" not in token


def test_change_password_then_login_uses_the_new_password(fake_accounts):
    auth.create_account("alice", "the-old-password")

    _change_password("alice", "the-new-password")

    assert auth.check_login("alice", "the-new-password") is not None
    assert auth.check_login("alice", "the-old-password") is None


def test_change_password_refuses_if_the_account_changed_since_it_was_checked(fake_accounts):
    # Someone who knew the old password, changing it in the moment after its
    # owner did, must not overwrite the owner's new one.
    auth.create_account("alice", "old-password")
    attacker_checked = auth.check_login("alice", "old-password")
    _change_password("alice", "owners-new-password")

    with pytest.raises(storage.AccountChanged):
        auth.change_password("alice", "attackers-password", attacker_checked["etag"])

    assert auth.check_login("alice", "owners-new-password") is not None


@pytest.mark.parametrize("password", ["short", "x" * (auth.MAX_PASSWORD_LENGTH + 1)])
def test_change_password_enforces_the_length_rules(accounts, password):
    with pytest.raises(auth.InvalidPassword):
        _change_password("alice", password)
    assert storage.get_account("alice")["password_hash"] == "hash-of-alice"


def test_a_request_checks_its_session_against_storage_only_once(accounts, monkeypatch):
    # require_session and the handler both ask for the username; checking
    # the account twice per request would double the storage reads.
    req = _session_request("alice")()
    reads = []
    real_get_account = storage.get_account
    monkeypatch.setattr(storage, "get_account", lambda u: reads.append(u) or real_get_account(u))

    assert auth.is_authenticated(req) is True
    assert auth.get_username(req) == "alice"
    assert reads == ["alice"]
