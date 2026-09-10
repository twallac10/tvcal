import json

import azure.functions as func

import function_app
import storage


def _request(method, route, params=None, route_params=None, body=b""):
    return func.HttpRequest(
        method=method,
        url=f"https://example.com/{route}",
        params=params or {},
        route_params=route_params or {},
        body=body,
    )


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


def test_watchlist_remove_calls_storage(monkeypatch):
    calls = []
    monkeypatch.setattr(storage, "remove_show", lambda token, show_id: calls.append((token, show_id)))

    response = function_app.watchlist_remove(
        _request("DELETE", "watchlist/82", params={"list": "abc123"}, route_params={"show_id": "82"})
    )

    assert response.status_code == 204
    assert calls == [("abc123", 82)]


def test_calendar_feed_requires_list_or_show_ids():
    response = function_app.calendar_feed(_request("GET", "calendar.ics"))
    assert response.status_code == 400


def test_calendar_feed_builds_events_from_watchlist_token(monkeypatch):
    monkeypatch.setattr(storage, "list_shows", lambda token: [{"id": 82, "name": "Fringe"}])
    monkeypatch.setattr(
        function_app, "get_show", lambda show_id: {"id": 82, "name": "Fringe", "runtime": 60}
    )
    monkeypatch.setattr(
        function_app,
        "get_show_episodes",
        lambda show_id: [
            {
                "id": 1,
                "name": "Pilot",
                "season": 1,
                "number": 1,
                "airstamp": "2008-09-09T20:00:00+00:00",
                "summary": None,
            }
        ],
    )

    response = function_app.calendar_feed(_request("GET", "calendar.ics", params={"list": "abc123"}))

    assert response.status_code == 200
    assert b"Fringe - S01E01 - Pilot" in response.get_body()


def test_calendar_feed_empty_watchlist_returns_empty_calendar(monkeypatch):
    monkeypatch.setattr(storage, "list_shows", lambda token: [])

    response = function_app.calendar_feed(_request("GET", "calendar.ics", params={"list": "abc123"}))

    assert response.status_code == 200
    assert b"BEGIN:VCALENDAR" in response.get_body()
    assert b"BEGIN:VEVENT" not in response.get_body()
