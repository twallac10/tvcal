"""Azure Functions app: search TVMaze shows and subscribe to episode iCal feeds."""
import json
import logging

import azure.functions as func

from ical_builder import build_calendar
from tvmaze_client import TVMazeError, get_show, get_show_episodes, search_shows

app = func.FunctionApp(http_auth_level=func.AuthLevel.ANONYMOUS)


@app.route(route="shows/search", methods=["GET"])
def search(req: func.HttpRequest) -> func.HttpResponse:
    """GET /api/shows/search?q=<name> -> matching shows with their TVMaze IDs."""
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


@app.route(route="calendar.ics", methods=["GET"])
def calendar_feed(req: func.HttpRequest) -> func.HttpResponse:
    """GET /api/calendar.ics?show_ids=<id,id,...> -> iCal feed of episode air dates.

    The URL is meant to be pasted straight into a calendar app (Google
    Calendar, Apple Calendar, Outlook, ...) as a subscription so new episodes
    show up automatically as TVMaze publishes air dates.
    """
    raw_ids = req.params.get("show_ids")
    if not raw_ids:
        return func.HttpResponse(
            "Query parameter 'show_ids' is required, e.g. ?show_ids=82,143.",
            status_code=400,
        )

    try:
        show_ids = [int(value.strip()) for value in raw_ids.split(",") if value.strip()]
    except ValueError:
        return func.HttpResponse(
            "show_ids must be a comma-separated list of TVMaze show IDs.", status_code=400
        )

    if not show_ids:
        return func.HttpResponse("No valid show IDs supplied.", status_code=400)

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
