"""Azure Functions app: search TVMaze shows and subscribe to episode iCal feeds."""
import json
import logging
import pathlib

import azure.functions as func

import storage
from ical_builder import build_calendar
from tvmaze_client import TVMazeError, get_show, get_show_episodes, search_shows

app = func.FunctionApp(http_auth_level=func.AuthLevel.ANONYMOUS)

STATIC_DIR = pathlib.Path(__file__).parent / "static"


@app.route(route="", methods=["GET"])
def index(req: func.HttpRequest) -> func.HttpResponse:
    """GET / -> the search + watchlist single-page UI."""
    html = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
    return func.HttpResponse(html, status_code=200, mimetype="text/html")


@app.route(route="shows/search", methods=["GET"])
def search(req: func.HttpRequest) -> func.HttpResponse:
    """GET /shows/search?q=<name> -> matching shows with their TVMaze IDs."""
    query = req.params.get("q")
    if not query:
        return func.HttpResponse("Query parameter 'q' is required.", status_code=400)

    try:
        shows = search_shows(query)
    except Exception:
        logging.exception("TVMaze search failed for query=%s", query)
        return func.HttpResponse("Failed to reach TVMaze.", status_code=502)

    results = [
        {
            "id": show["id"],
            "name": show["name"],
            "premiered": show.get("premiered"),
            "status": show.get("status"),
            "network": ((show.get("network") or show.get("webChannel")) or {}).get("name"),
            "image": (show.get("image") or {}).get("medium"),
            "summary": show.get("summary"),
        }
        for show in shows
    ]
    return func.HttpResponse(json.dumps(results), status_code=200, mimetype="application/json")


@app.route(route="watchlist", methods=["GET"])
def watchlist_get(req: func.HttpRequest) -> func.HttpResponse:
    """GET /watchlist?list=<token> -> shows currently on that watchlist."""
    token = req.params.get("list")
    if not token:
        return func.HttpResponse("Query parameter 'list' is required.", status_code=400)

    try:
        shows = storage.list_shows(token)
    except storage.InvalidListToken as exc:
        return func.HttpResponse(str(exc), status_code=400)

    return func.HttpResponse(json.dumps(shows), status_code=200, mimetype="application/json")


@app.route(route="watchlist", methods=["POST"])
def watchlist_add(req: func.HttpRequest) -> func.HttpResponse:
    """POST /watchlist?list=<token>  body: {"show_id": int, "show_name": str}."""
    token = req.params.get("list")
    if not token:
        return func.HttpResponse("Query parameter 'list' is required.", status_code=400)

    try:
        body = req.get_json()
    except ValueError:
        return func.HttpResponse("Request body must be JSON.", status_code=400)

    show_id = body.get("show_id")
    show_name = body.get("show_name")
    if not isinstance(show_id, int) or not isinstance(show_name, str) or not show_name:
        return func.HttpResponse(
            "Body must include integer 'show_id' and non-empty 'show_name'.", status_code=400
        )

    try:
        storage.add_show(token, show_id, show_name)
    except storage.InvalidListToken as exc:
        return func.HttpResponse(str(exc), status_code=400)

    return func.HttpResponse(status_code=204)


@app.route(route="watchlist/{show_id}", methods=["DELETE"])
def watchlist_remove(req: func.HttpRequest) -> func.HttpResponse:
    """DELETE /watchlist/{show_id}?list=<token> -> remove a show from the watchlist."""
    token = req.params.get("list")
    if not token:
        return func.HttpResponse("Query parameter 'list' is required.", status_code=400)

    try:
        show_id = int(req.route_params["show_id"])
    except (KeyError, ValueError):
        return func.HttpResponse("show_id must be an integer.", status_code=400)

    try:
        storage.remove_show(token, show_id)
    except storage.InvalidListToken as exc:
        return func.HttpResponse(str(exc), status_code=400)

    return func.HttpResponse(status_code=204)


@app.route(route="calendar.ics", methods=["GET"])
def calendar_feed(req: func.HttpRequest) -> func.HttpResponse:
    """GET /calendar.ics?list=<token> or ?show_ids=<id,id,...> -> iCal feed.

    The URL is meant to be pasted straight into a calendar app (Google
    Calendar, Apple Calendar, Outlook, ...) as a subscription so new episodes
    show up automatically as TVMaze publishes air dates. When built from a
    watchlist token, the same URL keeps working as shows are added/removed --
    no need to re-subscribe.
    """
    list_token = req.params.get("list")
    raw_ids = req.params.get("show_ids")

    if list_token:
        try:
            show_ids = [show["id"] for show in storage.list_shows(list_token)]
        except storage.InvalidListToken as exc:
            return func.HttpResponse(str(exc), status_code=400)
    elif raw_ids:
        try:
            show_ids = [int(value.strip()) for value in raw_ids.split(",") if value.strip()]
        except ValueError:
            return func.HttpResponse(
                "show_ids must be a comma-separated list of TVMaze show IDs.", status_code=400
            )
        if not show_ids:
            return func.HttpResponse("No valid show IDs supplied.", status_code=400)
    else:
        return func.HttpResponse(
            "Query parameter 'list' (watchlist token) or 'show_ids' is required.",
            status_code=400,
        )

    shows_with_episodes = []
    try:
        for show_id in show_ids:
            show = get_show(show_id)
            episodes = get_show_episodes(show_id)
            shows_with_episodes.append((show, episodes))
    except TVMazeError as exc:
        return func.HttpResponse(str(exc), status_code=404)
    except Exception:
        logging.exception("TVMaze lookup failed for show_ids=%s", show_ids)
        return func.HttpResponse("Failed to reach TVMaze.", status_code=502)

    calendar = build_calendar(shows_with_episodes)
    return func.HttpResponse(
        calendar.to_ical(),
        status_code=200,
        mimetype="text/calendar",
        headers={"Content-Disposition": "inline; filename=tv-shows.ics"},
    )
