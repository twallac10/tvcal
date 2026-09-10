import json

import azure.functions as func

import auth
import function_app
import storage
from tvmaze_client import TVMazeError


def _request(method, route, params=None, route_params=None, body=b""):
    return func.HttpRequest(
        method=method,
        url=f"https://example.com/{route}",
        params=params or {},
        route_params=route_params or {},
        body=body,
    )


def test_index_does_not_require_a_session():
    # Has to be reachable logged-out -- it's what renders the login form.
    response = function_app.index(_request("GET", ""))
    assert response.status_code == 200


def test_auth_signup_creates_account_and_sets_cookie(monkeypatch):
    monkeypatch.setattr(auth, "check_signup_code", lambda code: code == "letmein")
    monkeypatch.setattr(auth, "create_account", lambda username, password: username.lower())
    monkeypatch.setattr(
        auth, "create_session_cookie", lambda username: f"tvcal_session={username}; Path=/"
    )

    body = json.dumps(
        {"username": "Alice", "password": "hunter2pass", "signup_code": "letmein"}
    ).encode()
    response = function_app.auth_signup(_request("POST", "auth/signup", body=body))

    assert response.status_code == 204
    assert response.headers["Set-Cookie"] == "tvcal_session=alice; Path=/"


def test_auth_signup_migrates_previous_token_when_given(monkeypatch):
    monkeypatch.setattr(auth, "check_signup_code", lambda code: True)
    monkeypatch.setattr(auth, "create_account", lambda username, password: "alice")
    monkeypatch.setattr(auth, "create_session_cookie", lambda username: "tvcal_session=x; Path=/")

    calls = []
    monkeypatch.setattr(
        storage, "migrate_watchlist", lambda old, new: calls.append((old, new))
    )

    body = json.dumps(
        {
            "username": "alice",
            "password": "hunter2pass",
            "signup_code": "letmein",
            "previous_token": "old-browser-token",
        }
    ).encode()
    response = function_app.auth_signup(_request("POST", "auth/signup", body=body))

    assert response.status_code == 204
    assert calls == [("old-browser-token", "alice")]


def test_auth_signup_rejects_wrong_signup_code(monkeypatch):
    monkeypatch.setattr(auth, "check_signup_code", lambda code: False)

    body = json.dumps(
        {"username": "alice", "password": "hunter2pass", "signup_code": "wrong"}
    ).encode()
    response = function_app.auth_signup(_request("POST", "auth/signup", body=body))

    assert response.status_code == 401


def test_auth_signup_rejects_short_password(monkeypatch):
    monkeypatch.setattr(auth, "check_signup_code", lambda code: True)

    body = json.dumps(
        {"username": "alice", "password": "short", "signup_code": "letmein"}
    ).encode()
    response = function_app.auth_signup(_request("POST", "auth/signup", body=body))

    assert response.status_code == 400


def test_auth_signup_rejects_missing_fields():
    body = json.dumps({"username": "alice"}).encode()
    response = function_app.auth_signup(_request("POST", "auth/signup", body=body))
    assert response.status_code == 400


def test_auth_signup_reports_signups_closed_when_no_signup_code_configured(monkeypatch):
    # No SIGNUP_CODE is the documented way to stop accepting new accounts,
    # so it's a deliberate 403, not a 500 with an ERROR log per attempt.
    def raise_unconfigured(code):
        raise auth.AuthConfigError("nope")

    monkeypatch.setattr(auth, "check_signup_code", raise_unconfigured)

    body = json.dumps(
        {"username": "alice", "password": "hunter2pass", "signup_code": "letmein"}
    ).encode()
    response = function_app.auth_signup(_request("POST", "auth/signup", body=body))

    assert response.status_code == 403


def test_auth_signup_rejects_invalid_username(monkeypatch):
    monkeypatch.setattr(auth, "check_signup_code", lambda code: True)

    def raise_invalid(username, password):
        raise auth.InvalidUsername("bad username")

    monkeypatch.setattr(auth, "create_account", raise_invalid)

    body = json.dumps(
        {"username": "ab", "password": "hunter2pass", "signup_code": "letmein"}
    ).encode()
    response = function_app.auth_signup(_request("POST", "auth/signup", body=body))

    assert response.status_code == 400


def test_auth_signup_returns_409_when_username_taken(monkeypatch):
    monkeypatch.setattr(auth, "check_signup_code", lambda code: True)

    def raise_taken(username, password):
        raise storage.UsernameTaken("taken")

    monkeypatch.setattr(auth, "create_account", raise_taken)

    body = json.dumps(
        {"username": "alice", "password": "hunter2pass", "signup_code": "letmein"}
    ).encode()
    response = function_app.auth_signup(_request("POST", "auth/signup", body=body))

    assert response.status_code == 409


def test_auth_signup_returns_403_when_too_many_accounts(monkeypatch):
    monkeypatch.setattr(auth, "check_signup_code", lambda code: True)

    def raise_too_many(username, password):
        raise storage.TooManyAccounts("full")

    monkeypatch.setattr(auth, "create_account", raise_too_many)

    body = json.dumps(
        {"username": "alice", "password": "hunter2pass", "signup_code": "letmein"}
    ).encode()
    response = function_app.auth_signup(_request("POST", "auth/signup", body=body))

    assert response.status_code == 403


def test_auth_login_sets_cookie_on_correct_credentials(monkeypatch):
    monkeypatch.setattr(auth, "verify_login", lambda username, password: password == "right")
    monkeypatch.setattr(
        auth, "create_session_cookie", lambda username: f"tvcal_session={username}; Path=/"
    )

    body = json.dumps({"username": "Alice", "password": "right"}).encode()
    response = function_app.auth_login(_request("POST", "auth/login", body=body))

    assert response.status_code == 204
    assert response.headers["Set-Cookie"] == "tvcal_session=alice; Path=/"


def test_auth_login_rejects_wrong_credentials(monkeypatch):
    monkeypatch.setattr(auth, "verify_login", lambda username, password: False)

    body = json.dumps({"username": "alice", "password": "wrong"}).encode()
    response = function_app.auth_login(_request("POST", "auth/login", body=body))

    assert response.status_code == 401


def test_auth_login_rejects_missing_fields():
    response = function_app.auth_login(_request("POST", "auth/login", body=b"{}"))
    assert response.status_code == 400


def test_auth_login_rejects_malformed_body():
    response = function_app.auth_login(_request("POST", "auth/login", body=b"not json"))
    assert response.status_code == 400


def test_auth_login_returns_500_when_server_auth_unconfigured(monkeypatch):
    def raise_unconfigured(username, password):
        raise auth.AuthConfigError("nope")

    monkeypatch.setattr(auth, "verify_login", raise_unconfigured)

    body = json.dumps({"username": "alice", "password": "whatever"}).encode()
    response = function_app.auth_login(_request("POST", "auth/login", body=body))

    assert response.status_code == 500


def test_auth_logout_clears_cookie(monkeypatch):
    monkeypatch.setattr(auth, "clear_session_cookie", lambda: "tvcal_session=; Max-Age=0")

    response = function_app.auth_logout(_request("POST", "auth/logout"))

    assert response.status_code == 204
    assert response.headers["Set-Cookie"] == "tvcal_session=; Max-Age=0"


def test_auth_status_reports_authenticated(monkeypatch):
    monkeypatch.setattr(auth, "get_username", lambda req: "alice")
    response = function_app.auth_status(_request("GET", "auth/status"))
    assert json.loads(response.get_body()) == {"authenticated": True, "username": "alice"}


def test_auth_status_reports_unauthenticated(monkeypatch):
    monkeypatch.setattr(auth, "get_username", lambda req: None)
    response = function_app.auth_status(_request("GET", "auth/status"))
    assert json.loads(response.get_body()) == {"authenticated": False, "username": None}


# Routes deliberately reachable without a session, each with the reason it
# has to be. Anything else registered on the app must carry
# @auth.require_session.
PUBLIC_ROUTES = {
    "index": "renders the login form itself, before anyone can have a session",
    "auth_signup": "creating an account is how you get a session",
    "auth_login": "logging in is how you get a session",
    "auth_logout": "clearing a cookie shouldn't require a valid one",
    "auth_status": "reports whether you have a session",
    "calendar_feed": "calendar apps fetch it with no cookies; gated by its feed token",
}


def _registered_functions():
    # get_functions() populates an internal bindings dict and validates
    # against it, so a second call on the same app raises on "duplicate"
    # names. Reset it so this is safe to call from more than one test.
    function_app.app.functions_bindings = {}
    return function_app.app.get_functions()


def test_every_registered_route_is_gated_or_explicitly_public():
    # Enumerated rather than hand-listed: a list of protected routes stays
    # green when a NEW route forgets @auth.require_session, which is
    # exactly when you'd want a failure.
    for function in _registered_functions():
        name = function.get_function_name()
        gated = getattr(function.get_user_function(), "requires_session", False)
        assert gated or name in PUBLIC_ROUTES, (
            f"Route '{name}' is neither gated by @auth.require_session nor listed in "
            f"PUBLIC_ROUTES. Add the decorator, or add it to PUBLIC_ROUTES with the reason."
        )


def test_public_route_allowlist_has_no_stale_entries():
    registered = {f.get_function_name() for f in _registered_functions()}
    assert set(PUBLIC_ROUTES) <= registered


def test_protected_routes_return_401_without_a_session(monkeypatch):
    monkeypatch.setattr(auth, "is_authenticated", lambda req: False)

    protected = [
        lambda: function_app.watchlist_get(_request("GET", "watchlist")),
        lambda: function_app.watchlist_add(_request("POST", "watchlist", body=b"{}")),
        lambda: function_app.watchlist_feed_token(_request("GET", "watchlist/feed-token")),
        lambda: function_app.watchlist_remove(
            _request("DELETE", "watchlist/1", route_params={"show_id": "1"})
        ),
        lambda: function_app.search(_request("GET", "shows/search", params={"q": "fringe"})),
    ]
    for call in protected:
        assert call().status_code == 401


def test_calendar_feed_does_not_require_a_session(monkeypatch):
    # Calendar apps fetch this URL directly with no cookies -- it must stay
    # reachable without a session, gated only by its own feed token.
    monkeypatch.setattr(auth, "is_authenticated", lambda req: False)
    monkeypatch.setattr(storage, "resolve_feed_token", lambda token: "abc123")
    monkeypatch.setattr(storage, "list_shows", lambda token: [])

    response = function_app.calendar_feed(_request("GET", "calendar.ics", params={"feed": "tok"}))

    assert response.status_code == 200


def test_search_returns_502_when_tvmaze_result_is_missing_required_fields(monkeypatch):
    # A TVMaze result missing "id"/"name" should degrade to the same 502 the
    # fetch-failure path uses, not an unhandled 500 from the response shaping.
    monkeypatch.setattr(function_app, "search_shows", lambda query: [{"name": "no id"}])

    response = function_app.search(_request("GET", "shows/search", params={"q": "fringe"}))

    assert response.status_code == 502


def test_watchlist_get_returns_shows_for_the_signed_in_user(monkeypatch):
    calls = []

    def fake_list_shows(username):
        calls.append(username)
        return [{"id": 82, "name": "Fringe"}]

    monkeypatch.setattr(storage, "list_shows", fake_list_shows)

    response = function_app.watchlist_get(_request("GET", "watchlist"))

    assert response.status_code == 200
    assert json.loads(response.get_body()) == [{"id": 82, "name": "Fringe"}]
    assert calls == ["testuser"]  # conftest's bypass_auth stubs the session username


def test_watchlist_add_calls_storage_with_signed_in_user(monkeypatch):
    calls = []
    monkeypatch.setattr(
        storage, "add_show", lambda username, show_id, show_name: calls.append((username, show_id, show_name))
    )

    body = json.dumps({"show_id": 82, "show_name": "Fringe"}).encode()
    response = function_app.watchlist_add(_request("POST", "watchlist", body=body))

    assert response.status_code == 204
    assert calls == [("testuser", 82, "Fringe")]


def test_watchlist_add_rejects_malformed_body():
    response = function_app.watchlist_add(_request("POST", "watchlist", body=b"not json"))
    assert response.status_code == 400


def test_watchlist_add_rejects_missing_fields():
    body = json.dumps({"show_id": 82}).encode()
    response = function_app.watchlist_add(_request("POST", "watchlist", body=body))
    assert response.status_code == 400


def test_watchlist_add_rejects_boolean_show_id():
    # bool is a subclass of int in Python -- isinstance(True, int) is True --
    # so this must be rejected explicitly or it corrupts the RowKey.
    body = json.dumps({"show_id": True, "show_name": "Fringe"}).encode()
    response = function_app.watchlist_add(_request("POST", "watchlist", body=body))
    assert response.status_code == 400


def test_watchlist_add_rejects_oversized_show_name():
    body = json.dumps({"show_id": 82, "show_name": "x" * 500}).encode()
    response = function_app.watchlist_add(_request("POST", "watchlist", body=body))
    assert response.status_code == 400


def test_watchlist_add_rejects_show_id_too_large_for_table_storage_row_key():
    # Table Storage RowKeys are capped at 1024 chars; anything absurdly large
    # should be rejected with a clean 400 instead of failing inside storage.
    body = json.dumps({"show_id": 10**16, "show_name": "Fringe"}).encode()
    response = function_app.watchlist_add(_request("POST", "watchlist", body=body))
    assert response.status_code == 400


def test_watchlist_add_returns_409_when_watchlist_full(monkeypatch):
    def raise_full(username, show_id, show_name):
        raise storage.WatchlistFull("full")

    monkeypatch.setattr(storage, "add_show", raise_full)

    body = json.dumps({"show_id": 82, "show_name": "Fringe"}).encode()
    response = function_app.watchlist_add(_request("POST", "watchlist", body=body))
    assert response.status_code == 409


def test_watchlist_remove_calls_storage_with_signed_in_user(monkeypatch):
    calls = []
    monkeypatch.setattr(
        storage, "remove_show", lambda username, show_id: calls.append((username, show_id))
    )

    response = function_app.watchlist_remove(
        _request("DELETE", "watchlist/82", route_params={"show_id": "82"})
    )

    assert response.status_code == 204
    assert calls == [("testuser", 82)]


def test_watchlist_feed_token_returns_storage_value_for_signed_in_user(monkeypatch):
    calls = []

    def fake_get_or_create_feed_token(username):
        calls.append(username)
        return "feed-tok-123"

    monkeypatch.setattr(storage, "get_or_create_feed_token", fake_get_or_create_feed_token)

    response = function_app.watchlist_feed_token(_request("GET", "watchlist/feed-token"))

    assert response.status_code == 200
    assert json.loads(response.get_body()) == {"feed_token": "feed-tok-123"}
    assert calls == ["testuser"]


def test_calendar_feed_requires_a_feed_token():
    response = function_app.calendar_feed(_request("GET", "calendar.ics"))
    assert response.status_code == 400


def test_calendar_feed_write_token_alone_is_rejected(monkeypatch):
    # The old ?list=<write_token> shape must not work for the calendar feed
    # -- that would defeat the whole point of splitting read/write tokens.
    monkeypatch.setattr(storage, "list_shows", lambda token: [{"id": 82, "name": "Fringe"}])

    response = function_app.calendar_feed(_request("GET", "calendar.ics", params={"list": "abc123"}))

    assert response.status_code == 400


def test_calendar_feed_rejects_unknown_feed_token(monkeypatch):
    def raise_unknown(feed_token):
        raise storage.UnknownFeedToken("nope")

    monkeypatch.setattr(storage, "resolve_feed_token", raise_unknown)

    response = function_app.calendar_feed(_request("GET", "calendar.ics", params={"feed": "bogus"}))

    assert response.status_code == 404


_PILOT_EPISODE = {
    "id": 1,
    "name": "Pilot",
    "season": 1,
    "number": 1,
    "airstamp": "2008-09-09T20:00:00+00:00",
    "summary": None,
}


def _use_feed_token(monkeypatch, feed_token="feed-tok", write_token="abc123"):
    def fake_resolve_feed_token(token):
        if token != feed_token:
            raise storage.UnknownFeedToken("nope")
        return write_token

    monkeypatch.setattr(storage, "resolve_feed_token", fake_resolve_feed_token)
    return feed_token


def test_calendar_feed_builds_events_from_feed_token(monkeypatch):
    feed_token = _use_feed_token(monkeypatch)
    monkeypatch.setattr(storage, "list_shows", lambda token: [{"id": 82, "name": "Fringe"}])
    monkeypatch.setattr(
        function_app,
        "get_show_with_episodes",
        lambda show_id: ({"id": 82, "name": "Fringe", "runtime": 60}, [_PILOT_EPISODE]),
    )

    response = function_app.calendar_feed(_request("GET", "calendar.ics", params={"feed": feed_token}))

    assert response.status_code == 200
    assert b"Fringe - S01E01 - Pilot" in response.get_body()


def test_calendar_feed_empty_watchlist_returns_empty_calendar(monkeypatch):
    feed_token = _use_feed_token(monkeypatch)
    monkeypatch.setattr(storage, "list_shows", lambda token: [])

    response = function_app.calendar_feed(_request("GET", "calendar.ics", params={"feed": feed_token}))

    assert response.status_code == 200
    assert b"BEGIN:VCALENDAR" in response.get_body()
    assert b"BEGIN:VEVENT" not in response.get_body()


def test_calendar_feed_skips_a_broken_show_but_keeps_the_rest(monkeypatch):
    feed_token = _use_feed_token(monkeypatch)
    monkeypatch.setattr(
        storage,
        "list_shows",
        lambda token: [{"id": 999, "name": "Gone"}, {"id": 82, "name": "Fringe"}],
    )

    def fake_get_show_with_episodes(show_id):
        if show_id == 999:
            raise TVMazeError("gone")
        return {"id": 82, "name": "Fringe", "runtime": 60}, [_PILOT_EPISODE]

    monkeypatch.setattr(function_app, "get_show_with_episodes", fake_get_show_with_episodes)

    response = function_app.calendar_feed(_request("GET", "calendar.ics", params={"feed": feed_token}))

    assert response.status_code == 200
    assert b"Fringe - S01E01 - Pilot" in response.get_body()


def test_calendar_feed_concurrent_fetch_isolates_multiple_failures(monkeypatch):
    feed_token = _use_feed_token(monkeypatch)
    good_ids = {82, 143, 144}
    bad_ids = {999, 1000}
    monkeypatch.setattr(
        storage,
        "list_shows",
        lambda token: [{"id": i, "name": str(i)} for i in sorted(good_ids | bad_ids)],
    )

    def fake_get_show_with_episodes(show_id):
        if show_id in bad_ids:
            raise TVMazeError("gone")
        return {"id": show_id, "name": f"Show {show_id}", "runtime": 60}, [
            {**_PILOT_EPISODE, "id": show_id}
        ]

    monkeypatch.setattr(function_app, "get_show_with_episodes", fake_get_show_with_episodes)

    response = function_app.calendar_feed(_request("GET", "calendar.ics", params={"feed": feed_token}))
    body = response.get_body()

    assert response.status_code == 200
    for show_id in good_ids:
        assert f"tvmaze-episode-{show_id}@tvcal".encode() in body


def test_calendar_feed_refuses_to_serve_a_partial_feed_on_transient_failure(monkeypatch):
    # Calendar clients treat a 200 as authoritative and delete events
    # missing from it, so silently dropping a show that failed for a
    # transient reason would wipe (or flap) the subscriber's episodes.
    feed_token = _use_feed_token(monkeypatch)
    monkeypatch.setattr(
        storage,
        "list_shows",
        lambda token: [{"id": 82, "name": "Fringe"}, {"id": 143, "name": "Breaking Bad"}],
    )

    def fake_get_show_with_episodes(show_id):
        if show_id == 143:
            raise ConnectionError("TVMaze unreachable")
        return {"id": 82, "name": "Fringe", "runtime": 60}, [_PILOT_EPISODE]

    monkeypatch.setattr(function_app, "get_show_with_episodes", fake_get_show_with_episodes)

    response = function_app.calendar_feed(_request("GET", "calendar.ics", params={"feed": feed_token}))

    assert response.status_code == 502


def test_calendar_feed_still_skips_permanently_missing_shows(monkeypatch):
    # A show genuinely gone from TVMaze (404 -> TVMazeError) is different
    # from a transient failure: skip it and serve the rest.
    feed_token = _use_feed_token(monkeypatch)
    monkeypatch.setattr(
        storage,
        "list_shows",
        lambda token: [{"id": 999, "name": "Gone"}, {"id": 82, "name": "Fringe"}],
    )

    def fake_get_show_with_episodes(show_id):
        if show_id == 999:
            raise TVMazeError("gone")
        return {"id": 82, "name": "Fringe", "runtime": 60}, [_PILOT_EPISODE]

    monkeypatch.setattr(function_app, "get_show_with_episodes", fake_get_show_with_episodes)

    response = function_app.calendar_feed(_request("GET", "calendar.ics", params={"feed": feed_token}))

    assert response.status_code == 200
    assert b"Fringe - S01E01 - Pilot" in response.get_body()


def test_watchlist_remove_rejects_out_of_range_show_id():
    response = function_app.watchlist_remove(
        _request("DELETE", "watchlist/x", route_params={"show_id": str(10**16)})
    )
    assert response.status_code == 400


def test_auth_login_rejects_an_oversized_password_without_hashing(monkeypatch):
    def fail_if_called(username, password):
        raise AssertionError("should reject before hashing")

    monkeypatch.setattr(auth, "verify_login", fail_if_called)

    body = json.dumps({"username": "alice", "password": "a" * 100_000}).encode()
    response = function_app.auth_login(_request("POST", "auth/login", body=body))

    assert response.status_code == 401


def test_auth_signup_rejects_an_oversized_password(monkeypatch):
    monkeypatch.setattr(auth, "check_signup_code", lambda code: True)

    body = json.dumps(
        {"username": "alice", "password": "a" * 100_000, "signup_code": "letmein"}
    ).encode()
    response = function_app.auth_signup(_request("POST", "auth/signup", body=body))

    assert response.status_code == 400


def test_calendar_feed_ignores_the_removed_show_ids_param(monkeypatch):
    # ?show_ids= was an anonymous, tokenless way to make this endpoint fan
    # out one TVMaze request per listed ID -- an amplification vector
    # guarding functionality nothing used. It must stay gone: without a
    # feed token, no caller reaches TVMaze through here at all.
    def fail_if_called(show_id):
        raise AssertionError("no TVMaze fetch should happen without a feed token")

    monkeypatch.setattr(function_app, "get_show_with_episodes", fail_if_called)

    response = function_app.calendar_feed(
        _request("GET", "calendar.ics", params={"show_ids": "82,143"})
    )

    assert response.status_code == 400
