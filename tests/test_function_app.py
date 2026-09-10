import json

import azure.functions as func

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


def test_search_returns_502_when_tvmaze_result_is_missing_required_fields(monkeypatch):
    # A TVMaze result missing "id"/"name" should degrade to the same 502 the
    # fetch-failure path uses, not an unhandled 500 from the response shaping.
    monkeypatch.setattr(function_app, "search_shows", lambda query: [{"name": "no id"}])

    response = function_app.search(_request("GET", "shows/search", params={"q": "fringe"}))

    assert response.status_code == 502


def test_watchlist_get_requires_list_param():
    response = function_app.watchlist_get(_request("GET", "watchlist"))
    assert response.status_code == 400


def test_watchlist_get_returns_shows(monkeypatch):
    monkeypatch.setattr(storage, "list_shows", lambda token: [{"id": 82, "name": "Fringe"}])

    response = function_app.watchlist_get(_request("GET", "watchlist", params={"list": "abc123"}))

    assert response.status_code == 200
    assert json.loads(response.get_body()) == [{"id": 82, "name": "Fringe"}]


def test_watchlist_add_calls_storage_with_parsed_body(monkeypatch):
    calls = []
    monkeypatch.setattr(
        storage, "add_show", lambda token, show_id, show_name: calls.append((token, show_id, show_name))
    )

    body = json.dumps({"show_id": 82, "show_name": "Fringe"}).encode()
    response = function_app.watchlist_add(
        _request("POST", "watchlist", params={"list": "abc123"}, body=body)
    )

    assert response.status_code == 204
    assert calls == [("abc123", 82, "Fringe")]


def test_watchlist_add_rejects_malformed_body():
    response = function_app.watchlist_add(
        _request("POST", "watchlist", params={"list": "abc123"}, body=b"not json")
    )
    assert response.status_code == 400


def test_watchlist_add_rejects_missing_fields():
    body = json.dumps({"show_id": 82}).encode()
    response = function_app.watchlist_add(
        _request("POST", "watchlist", params={"list": "abc123"}, body=body)
    )
    assert response.status_code == 400


def test_watchlist_add_rejects_boolean_show_id():
    # bool is a subclass of int in Python -- isinstance(True, int) is True --
    # so this must be rejected explicitly or it corrupts the RowKey.
    body = json.dumps({"show_id": True, "show_name": "Fringe"}).encode()
    response = function_app.watchlist_add(
        _request("POST", "watchlist", params={"list": "abc123"}, body=body)
    )
    assert response.status_code == 400


def test_watchlist_add_rejects_oversized_show_name():
    body = json.dumps({"show_id": 82, "show_name": "x" * 500}).encode()
    response = function_app.watchlist_add(
        _request("POST", "watchlist", params={"list": "abc123"}, body=body)
    )
    assert response.status_code == 400


def test_watchlist_add_rejects_show_id_too_large_for_table_storage_row_key():
    # Table Storage RowKeys are capped at 1024 chars; anything absurdly large
    # should be rejected with a clean 400 instead of failing inside storage.
    body = json.dumps({"show_id": 10**16, "show_name": "Fringe"}).encode()
    response = function_app.watchlist_add(
        _request("POST", "watchlist", params={"list": "abc123"}, body=body)
    )
    assert response.status_code == 400


def test_watchlist_add_returns_409_when_watchlist_full(monkeypatch):
    def raise_full(token, show_id, show_name):
        raise storage.WatchlistFull("full")

    monkeypatch.setattr(storage, "add_show", raise_full)

    body = json.dumps({"show_id": 82, "show_name": "Fringe"}).encode()
    response = function_app.watchlist_add(
        _request("POST", "watchlist", params={"list": "abc123"}, body=body)
    )
    assert response.status_code == 409


def test_watchlist_remove_calls_storage(monkeypatch):
    calls = []
    monkeypatch.setattr(storage, "remove_show", lambda token, show_id: calls.append((token, show_id)))

    response = function_app.watchlist_remove(
        _request("DELETE", "watchlist/82", params={"list": "abc123"}, route_params={"show_id": "82"})
    )

    assert response.status_code == 204
    assert calls == [("abc123", 82)]


def test_watchlist_feed_token_requires_list_param():
    response = function_app.watchlist_feed_token(_request("GET", "watchlist/feed-token"))
    assert response.status_code == 400


def test_watchlist_feed_token_returns_storage_value(monkeypatch):
    monkeypatch.setattr(storage, "get_or_create_feed_token", lambda token: "feed-tok-123")

    response = function_app.watchlist_feed_token(
        _request("GET", "watchlist/feed-token", params={"list": "abc123"})
    )

    assert response.status_code == 200
    assert json.loads(response.get_body()) == {"feed_token": "feed-tok-123"}


def test_calendar_feed_requires_feed_or_show_ids():
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


def test_calendar_feed_caps_show_ids_param():
    ids = ",".join(str(i) for i in range(1, function_app.MAX_SHOW_IDS_PARAM + 2))
    response = function_app.calendar_feed(
        _request("GET", "calendar.ics", params={"show_ids": ids})
    )
    assert response.status_code == 400
