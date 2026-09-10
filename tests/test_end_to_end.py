"""Tests that drive real routes against real auth and real storage logic.

Every other test file stubs one side or the other: test_function_app
monkeypatches storage away, test_storage never goes through a route, and
conftest's autouse fixture bypasses the auth gate entirely. That leaves the
actual multi-tenancy boundary -- session cookie -> username -> Table
Storage PartitionKey -- with no coverage at all, which is precisely the
path a cross-account data leak would travel. These tests use the real
implementations end to end, with only the Table Storage client faked.
"""
import json

import azure.functions as func
import pytest

import auth
import function_app
import storage

SIGNUP_CODE = "letmein"
PASSWORD = "hunter2pass"

_real_is_authenticated = auth.is_authenticated
_real_get_username = auth.get_username


@pytest.fixture(autouse=True)
def use_real_auth(monkeypatch):
    # Undo conftest's autouse bypass -- these tests are about the real gate.
    monkeypatch.setattr(auth, "is_authenticated", _real_is_authenticated)
    monkeypatch.setattr(auth, "get_username", _real_get_username)
    monkeypatch.setenv("SESSION_SECRET", "test-secret-do-not-use-in-prod")
    monkeypatch.setenv("SIGNUP_CODE", SIGNUP_CODE)


def _request(method, route, cookie=None, body=b"", route_params=None, params=None):
    return func.HttpRequest(
        method=method,
        url=f"https://example.com/{route}",
        headers={"Cookie": cookie} if cookie else {},
        params=params or {},
        route_params=route_params or {},
        body=body,
    )


def _cookie_for(username):
    """The Cookie request header a browser would send back after signing in."""
    return auth.create_session_cookie(username).split(";")[0]


def _signup(username):
    body = json.dumps(
        {"username": username, "password": PASSWORD, "signup_code": SIGNUP_CODE}
    ).encode()
    return function_app.auth_signup(_request("POST", "auth/signup", body=body))


def _add_show(cookie, show_id, show_name):
    body = json.dumps({"show_id": show_id, "show_name": show_name}).encode()
    return function_app.watchlist_add(_request("POST", "watchlist", cookie=cookie, body=body))


def _get_watchlist(cookie):
    response = function_app.watchlist_get(_request("GET", "watchlist", cookie=cookie))
    return response.status_code, json.loads(response.get_body())


def test_signup_then_login_round_trip(fake_client):
    assert _signup("alice").status_code == 204

    body = json.dumps({"username": "alice", "password": PASSWORD}).encode()
    assert function_app.auth_login(_request("POST", "auth/login", body=body)).status_code == 204

    wrong = json.dumps({"username": "alice", "password": "not-the-password"}).encode()
    assert function_app.auth_login(_request("POST", "auth/login", body=wrong)).status_code == 401


def test_session_cookie_from_signup_authenticates_subsequent_requests(fake_client):
    response = _signup("alice")
    cookie = response.headers["Set-Cookie"].split(";")[0]

    status, shows = _get_watchlist(cookie)

    assert status == 200
    assert shows == []


def test_two_accounts_have_fully_isolated_watchlists(fake_client):
    _signup("alice")
    _signup("bob")
    alice, bob = _cookie_for("alice"), _cookie_for("bob")

    assert _add_show(alice, 82, "Fringe").status_code == 204
    assert _add_show(bob, 143, "Breaking Bad").status_code == 204

    assert _get_watchlist(alice) == (200, [{"id": 82, "name": "Fringe"}])
    assert _get_watchlist(bob) == (200, [{"id": 143, "name": "Breaking Bad"}])


def test_one_account_cannot_delete_another_accounts_show(fake_client):
    _signup("alice")
    _signup("bob")
    alice, bob = _cookie_for("alice"), _cookie_for("bob")
    _add_show(alice, 82, "Fringe")

    response = function_app.watchlist_remove(
        _request("DELETE", "watchlist/82", cookie=bob, route_params={"show_id": "82"})
    )

    assert response.status_code == 204  # succeeds against Bob's own (empty) list
    assert _get_watchlist(alice) == (200, [{"id": 82, "name": "Fringe"}])


def test_feed_tokens_are_per_account_and_resolve_to_their_own_watchlist(fake_client):
    _signup("alice")
    _signup("bob")
    alice, bob = _cookie_for("alice"), _cookie_for("bob")
    _add_show(alice, 82, "Fringe")

    alice_token = json.loads(
        function_app.watchlist_feed_token(
            _request("GET", "watchlist/feed-token", cookie=alice)
        ).get_body()
    )["feed_token"]
    bob_token = json.loads(
        function_app.watchlist_feed_token(
            _request("GET", "watchlist/feed-token", cookie=bob)
        ).get_body()
    )["feed_token"]

    assert alice_token != bob_token
    assert storage.resolve_feed_token(alice_token) == "alice"
    assert storage.resolve_feed_token(bob_token) == "bob"


def test_requests_without_a_cookie_are_rejected(fake_client):
    _signup("alice")
    _add_show(_cookie_for("alice"), 82, "Fringe")

    assert function_app.watchlist_get(_request("GET", "watchlist")).status_code == 401
    assert function_app.watchlist_add(_request("POST", "watchlist", body=b"{}")).status_code == 401


def test_a_forged_cookie_for_another_user_is_rejected(fake_client):
    _signup("alice")
    _add_show(_cookie_for("alice"), 82, "Fringe")

    # Same shape as a real cookie, but not signed with SESSION_SECRET.
    forged = f"{auth.COOKIE_NAME}=alice.99999999999.{'0' * 64}"

    assert function_app.watchlist_get(_request("GET", "watchlist", cookie=forged)).status_code == 401


def test_signup_is_rejected_without_the_signup_code(fake_client):
    body = json.dumps(
        {"username": "mallory", "password": PASSWORD, "signup_code": "wrong"}
    ).encode()

    assert function_app.auth_signup(_request("POST", "auth/signup", body=body)).status_code == 401


def test_signups_are_closed_when_no_signup_code_is_configured(fake_client, monkeypatch):
    monkeypatch.delenv("SIGNUP_CODE", raising=False)
    body = json.dumps(
        {"username": "mallory", "password": PASSWORD, "signup_code": "anything"}
    ).encode()

    assert function_app.auth_signup(_request("POST", "auth/signup", body=body)).status_code == 403


def test_signup_cannot_steal_another_accounts_watchlist_via_previous_token(fake_client):
    _signup("alice")
    _add_show(_cookie_for("alice"), 82, "Fringe")

    body = json.dumps(
        {
            "username": "mallory",
            "password": PASSWORD,
            "signup_code": SIGNUP_CODE,
            "previous_token": "alice",
        }
    ).encode()
    assert function_app.auth_signup(_request("POST", "auth/signup", body=body)).status_code == 204

    assert _get_watchlist(_cookie_for("mallory")) == (200, [])
    assert _get_watchlist(_cookie_for("alice")) == (200, [{"id": 82, "name": "Fringe"}])
