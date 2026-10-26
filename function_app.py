"""Azure Functions app: search TVMaze shows and subscribe to episode iCal feeds."""
import concurrent.futures
import json
import logging
import pathlib
import time
from datetime import datetime, timedelta, timezone

import azure.functions as func

import auth
import storage
from ical_builder import build_calendar
from tvmaze_client import (
    EPISODE_HISTORY_DAYS,
    TVMazeBusy,
    TVMazeError,
    get_show_with_episodes,
    network_name,
    search_shows,
    upcoming_episodes,
)

app = func.FunctionApp(http_auth_level=func.AuthLevel.ANONYMOUS)

STATIC_DIR = pathlib.Path(__file__).parent / "static"
MAX_SHOW_NAME_LENGTH = 200
MAX_SHOW_ID = 10**15
MAX_CONCURRENT_TVMAZE_FETCHES = 10
# How long a request's TVMaze lookups may take in total -- waiting on the
# rate limit and downloading -- before it answers without the stragglers.
# For search and the watchlist those just show no airdate (and still finish
# in the background, warming the cache); the search page is interactive, so
# it gets less patience than the watchlist.
SEARCH_LOOKUP_WAIT_SECONDS = 5
WATCHLIST_LOOKUP_WAIT_SECONDS = 20
# The calendar can't skip a show, so it waits as long as a full 100-show
# watchlist needs (~55s paced), within Azure's 230s HTTP request limit.
CALENDAR_LOOKUP_WAIT_SECONDS = 150


def _json_object(req: func.HttpRequest) -> dict | None:
    """The request body as a JSON object, or None if it's anything else
    (malformed, or valid JSON that isn't an object, like a list)."""
    try:
        body = req.get_json()
    except ValueError:
        return None
    return body if isinstance(body, dict) else None


@app.route(route="/", methods=["GET"])
def index(req: func.HttpRequest) -> func.HttpResponse:
    """GET / -> the login form / search + watchlist single-page UI.

    Unauthenticated (no valid session), since this is the one page that has
    to load *before* anyone has a session -- it's what renders the login
    form. The page itself decides client-side whether to show the app or
    the login form, based on GET /auth/status.
    """
    html = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
    return func.HttpResponse(html, status_code=200, mimetype="text/html")


@app.route(route="auth/signup", methods=["POST"])
def auth_signup(req: func.HttpRequest) -> func.HttpResponse:
    """POST /auth/signup  body: {"username", "password", "signup_code"}.

    signup_code gates account creation (see the SIGNUP_CODE app setting) --
    it's shared out-of-band with whoever should get an account, same trust
    model as the old shared password, but only for this one step.
    """
    body = _json_object(req)
    if body is None:
        return func.HttpResponse("Request body must be a JSON object.", status_code=400)

    username = body.get("username")
    password = body.get("password")
    signup_code = body.get("signup_code")

    if (
        not isinstance(username, str)
        or not isinstance(password, str)
        or not isinstance(signup_code, str)
        or not signup_code
    ):
        return func.HttpResponse(
            "Body must include 'username', 'password', and 'signup_code'.", status_code=400
        )
    try:
        auth.validate_new_password(password)
    except auth.InvalidPassword as exc:
        return func.HttpResponse(str(exc), status_code=400)

    try:
        code_ok = auth.check_signup_code(signup_code)
    except auth.AuthConfigError:
        # No SIGNUP_CODE configured means signups are deliberately closed
        # (the documented way to stop accepting new accounts), not a broken
        # server -- say so plainly instead of 500ing on every attempt.
        return func.HttpResponse("Signups are closed.", status_code=403)
    if not code_ok:
        return func.HttpResponse("Invalid signup code.", status_code=401)

    try:
        normalized_username = auth.create_account(username, password)
    except auth.InvalidUsername as exc:
        return func.HttpResponse(str(exc), status_code=400)
    except storage.UsernameTaken as exc:
        return func.HttpResponse(str(exc), status_code=409)
    except storage.TooManyAccounts as exc:
        return func.HttpResponse(str(exc), status_code=403)

    try:
        cookie = auth.create_session_cookie(normalized_username)
    except auth.AuthConfigError:
        logging.error("Signup succeeded but SESSION_SECRET is not configured.")
        return func.HttpResponse("Server auth is not configured.", status_code=500)

    return func.HttpResponse(status_code=204, headers={"Set-Cookie": cookie})


@app.route(route="auth/login", methods=["POST"])
def auth_login(req: func.HttpRequest) -> func.HttpResponse:
    """POST /auth/login  body: {"username": str, "password": str} -> sets the session cookie."""
    body = _json_object(req)
    if body is None:
        return func.HttpResponse("Request body must be a JSON object.", status_code=400)

    username = body.get("username")
    password = body.get("password")
    if not isinstance(username, str) or not username or not isinstance(password, str) or not password:
        return func.HttpResponse("Body must include 'username' and 'password'.", status_code=400)
    if len(password) > auth.MAX_PASSWORD_LENGTH:
        # Reject before hashing -- see auth.MAX_PASSWORD_LENGTH.
        return func.HttpResponse("Incorrect username or password.", status_code=401)

    try:
        valid = auth.verify_login(username, password)
        cookie = auth.create_session_cookie(username.lower()) if valid else None
    except auth.AuthConfigError:
        logging.error("Login attempted but auth app settings are not configured.")
        return func.HttpResponse("Server auth is not configured.", status_code=500)

    if not valid:
        return func.HttpResponse("Incorrect username or password.", status_code=401)

    return func.HttpResponse(status_code=204, headers={"Set-Cookie": cookie})


@app.route(route="auth/logout", methods=["POST"])
def auth_logout(req: func.HttpRequest) -> func.HttpResponse:
    """POST /auth/logout -> clears the session cookie."""
    return func.HttpResponse(status_code=204, headers={"Set-Cookie": auth.clear_session_cookie()})


@app.route(route="auth/change-password", methods=["POST"])
@auth.require_session
def auth_change_password(req: func.HttpRequest) -> func.HttpResponse:
    """POST /auth/change-password  body: {"current_password", "new_password"}.

    Signs the account out on every other device -- the point, if someone
    else might know the old password -- and returns a fresh session cookie
    so this one stays signed in.
    """
    body = _json_object(req)
    if body is None:
        return func.HttpResponse("Request body must be a JSON object.", status_code=400)

    current_password = body.get("current_password")
    new_password = body.get("new_password")
    if not isinstance(current_password, str) or not current_password or not isinstance(new_password, str):
        return func.HttpResponse(
            "Body must include 'current_password' and 'new_password'.", status_code=400
        )
    try:
        auth.validate_new_password(new_password)
    except auth.InvalidPassword as exc:
        return func.HttpResponse(str(exc), status_code=400)

    username = auth.get_username(req)
    # 403, not 401: the session itself is fine (the UI treats 401 as
    # "signed out"). Oversized input is refused before hashing, as at login.
    if len(current_password) > auth.MAX_PASSWORD_LENGTH or not auth.verify_login(
        username, current_password
    ):
        return func.HttpResponse("Current password is incorrect.", status_code=403)

    try:
        auth.change_password(username, new_password)
    except storage.AccountBusy as exc:
        return func.HttpResponse(str(exc), status_code=409)
    return func.HttpResponse(status_code=204, headers={"Set-Cookie": auth.create_session_cookie(username)})


@app.route(route="auth/status", methods=["GET"])
def auth_status(req: func.HttpRequest) -> func.HttpResponse:
    """GET /auth/status -> {"authenticated": bool, "username": str | None}."""
    username = auth.get_username(req)
    return func.HttpResponse(
        json.dumps({"authenticated": username is not None, "username": username}),
        status_code=200,
        mimetype="application/json",
    )


@app.route(route="shows/search", methods=["GET"])
@auth.require_session
def search(req: func.HttpRequest) -> func.HttpResponse:
    """GET /shows/search?q=<name> -> matching shows, with their next episodes."""
    query = req.params.get("q")
    if not query:
        return func.HttpResponse("Query parameter 'q' is required.", status_code=400)

    try:
        shows = search_shows(query)
        results = [
            {
                "id": show["id"],
                "name": show["name"],
                "premiered": show.get("premiered"),
                "status": show.get("status"),
                "network": network_name(show),
                "image": (show.get("image") or {}).get("medium"),
                "summary": show.get("summary"),
            }
            for show in shows
        ]
    except Exception:
        logging.exception("TVMaze search failed for query=%s", query)
        return func.HttpResponse("Failed to reach TVMaze.", status_code=502)

    next_episodes = _next_episodes_by_show(shows)
    for result in results:
        result["next_episodes"] = next_episodes.get(result["id"], [])

    return func.HttpResponse(json.dumps(results), status_code=200, mimetype="application/json")


def _might_have_upcoming_episodes(show: dict) -> bool:
    # TVMaze only includes a nextepisode link when one is actually
    # scheduled, so this skips the extra fetch for ended shows and for
    # running shows between announcements.
    links = show.get("_links")
    if isinstance(links, dict):
        return "nextepisode" in links
    return show.get("status") != "Ended"


def _fetch_shows(show_ids: list[int], deadline: float) -> dict:
    """Look up shows on TVMaze concurrently: show_id -> (show, episodes), or
    the exception that show's lookup raised. A lookup still running at
    `deadline` (a time.monotonic() value) maps to TVMazeBusy -- the deadline
    bounds this call's total time, not just rate-limit waits."""
    found = {}
    if not show_ids:
        return found
    pool = concurrent.futures.ThreadPoolExecutor(max_workers=MAX_CONCURRENT_TVMAZE_FETCHES)
    future_to_show_id = {
        pool.submit(get_show_with_episodes, show_id, deadline=deadline): show_id
        for show_id in show_ids
    }
    try:
        done, _ = concurrent.futures.wait(
            future_to_show_id, timeout=max(0.0, deadline - time.monotonic())
        )
    finally:
        # Don't wait for stragglers: lookups already running finish in the
        # background (and still fill the cache), queued ones are dropped.
        pool.shutdown(wait=False, cancel_futures=True)

    for future, show_id in future_to_show_id.items():
        if future not in done:
            found[show_id] = TVMazeBusy(f"Ran out of time looking up show_id={show_id}.")
            continue
        try:
            found[show_id] = future.result()
        except Exception as exc:
            found[show_id] = exc
    return found


def _upcoming_or_none(show_id: int, result, now: datetime) -> list[dict] | None:
    """A _fetch_shows() result as next_episodes: None ("unknown") for a failed
    lookup, which only ever costs one show its airdates."""
    if isinstance(result, Exception):
        if not isinstance(result, TVMazeBusy):
            logging.warning("Couldn't look up upcoming episodes for show_id=%s", show_id, exc_info=result)
        return None
    _, episodes = result
    return upcoming_episodes(episodes, now)


def _next_episodes_by_show(shows: list[dict]) -> dict:
    """Best-effort upcoming-episode lookup for each search result.

    Search results don't include episodes, so this costs one TVMaze request
    per candidate show, made concurrently. A failed lookup maps to None
    ("unknown"), not [] ("nothing scheduled"), and never fails the search
    itself -- unlike the calendar feed, a search result missing an airdate
    is harmless.
    """
    candidates = [show["id"] for show in shows if _might_have_upcoming_episodes(show)]
    now = datetime.now(timezone.utc)
    fetched = _fetch_shows(candidates, deadline=time.monotonic() + SEARCH_LOOKUP_WAIT_SECONDS)
    return {show_id: _upcoming_or_none(show_id, result, now) for show_id, result in fetched.items()}


@app.route(route="watchlist", methods=["GET"])
@auth.require_session
def watchlist_get(req: func.HttpRequest) -> func.HttpResponse:
    """GET /watchlist -> shows on the signed-in account's watchlist."""
    shows = storage.list_shows(auth.get_username(req))
    return func.HttpResponse(json.dumps(shows), status_code=200, mimetype="application/json")


@app.route(route="watchlist/upcoming", methods=["GET"])
@auth.require_session
def watchlist_upcoming(req: func.HttpRequest) -> func.HttpResponse:
    """GET /watchlist/upcoming -> [{"id", "status", "next_episodes"}] per watchlist show.

    Separate from GET /watchlist so the list itself renders instantly; this
    costs a TVMaze lookup per show (cached), and the page fills airdates in
    when it arrives. Best-effort per show, like search: status and
    next_episodes are null when that show's lookup failed or ran out of time.
    """
    show_ids = [show["id"] for show in storage.list_shows(auth.get_username(req))]
    now = datetime.now(timezone.utc)
    fetched = _fetch_shows(show_ids, deadline=time.monotonic() + WATCHLIST_LOOKUP_WAIT_SECONDS)

    results = []
    for show_id in show_ids:
        result = fetched[show_id]
        status = None if isinstance(result, Exception) else result[0].get("status")
        results.append(
            {"id": show_id, "status": status, "next_episodes": _upcoming_or_none(show_id, result, now)}
        )
    return func.HttpResponse(json.dumps(results), status_code=200, mimetype="application/json")


@app.route(route="watchlist", methods=["POST"])
@auth.require_session
def watchlist_add(req: func.HttpRequest) -> func.HttpResponse:
    """POST /watchlist  body: {"show_id": int, "show_name": str} -> add to your watchlist."""
    body = _json_object(req)
    if body is None:
        return func.HttpResponse("Request body must be a JSON object.", status_code=400)

    show_id = body.get("show_id")
    show_name = body.get("show_name")
    if (
        not isinstance(show_id, int)
        or isinstance(show_id, bool)
        or not (0 < show_id < MAX_SHOW_ID)
        or not isinstance(show_name, str)
        or not show_name.strip()
        or len(show_name) > MAX_SHOW_NAME_LENGTH
    ):
        return func.HttpResponse(
            "Body must include a positive integer 'show_id' and a non-empty 'show_name' "
            f"(max {MAX_SHOW_NAME_LENGTH} characters).",
            status_code=400,
        )

    try:
        storage.add_show(auth.get_username(req), show_id, show_name)
    except storage.WatchlistFull as exc:
        return func.HttpResponse(str(exc), status_code=409)

    return func.HttpResponse(status_code=204)


@app.route(route="watchlist/feed-token", methods=["GET"])
@auth.require_session
def watchlist_feed_token(req: func.HttpRequest) -> func.HttpResponse:
    """GET /watchlist/feed-token -> the read-only calendar feed token for your watchlist.

    This token (not your login) is what belongs in a calendar subscription
    URL: it can only be used to read episode air dates, never to add or
    remove shows, so leaking it doesn't expose your account.
    """
    feed_token = storage.get_or_create_feed_token(auth.get_username(req))
    return func.HttpResponse(
        json.dumps({"feed_token": feed_token}), status_code=200, mimetype="application/json"
    )


@app.route(route="watchlist/feed-token/reset", methods=["POST"])
@auth.require_session
def watchlist_feed_token_reset(req: func.HttpRequest) -> func.HttpResponse:
    """POST /watchlist/feed-token/reset -> {"feed_token": "..."}, a brand-new one.

    The old calendar link stops working immediately -- the fix for a link
    that leaked, at the cost of re-subscribing every calendar that used it.
    """
    feed_token = storage.reset_feed_token(auth.get_username(req))
    return func.HttpResponse(
        json.dumps({"feed_token": feed_token}), status_code=200, mimetype="application/json"
    )


@app.route(route="watchlist/{show_id}", methods=["DELETE"])
@auth.require_session
def watchlist_remove(req: func.HttpRequest) -> func.HttpResponse:
    """DELETE /watchlist/{show_id} -> remove a show from your watchlist."""
    try:
        show_id = int(req.route_params["show_id"])
    except (KeyError, ValueError):
        return func.HttpResponse("show_id must be an integer.", status_code=400)

    # Same bounds as watchlist_add: an oversized value would otherwise reach
    # Table Storage as a RowKey past its 1024-char limit and 500.
    if not (0 < show_id < MAX_SHOW_ID):
        return func.HttpResponse("show_id is out of range.", status_code=400)

    storage.remove_show(auth.get_username(req), show_id)
    return func.HttpResponse(status_code=204)


@app.route(route="calendar.ics", methods=["GET"])
def calendar_feed(req: func.HttpRequest) -> func.HttpResponse:
    """GET /calendar.ics?feed=<feed_token> -> iCal feed for that watchlist.

    The URL is meant to be pasted straight into a calendar app (Google
    Calendar, Apple Calendar, Outlook, ...) as a subscription so new episodes
    show up automatically as TVMaze publishes air dates. The same URL keeps
    working as shows are added/removed -- no need to re-subscribe. The feed
    token is read-only by design -- it is not the account's login, so it
    can't be used to add or remove shows even if the URL leaks.

    Deliberately not @auth.require_session: calendar apps fetch this URL
    directly with no cookies at all, so a session gate would just break
    subscriptions. The feed token is this endpoint's only gate, and it's
    the only way in -- an unauthenticated caller with no token can't make
    this endpoint reach TVMaze at all.
    """
    feed_token = req.params.get("feed")
    if not feed_token:
        return func.HttpResponse(
            "Query parameter 'feed' (calendar feed token) is required.", status_code=400
        )

    try:
        write_token = storage.resolve_feed_token(feed_token)
        show_ids = [show["id"] for show in storage.list_shows(write_token)]
    except storage.InvalidListToken as exc:
        return func.HttpResponse(str(exc), status_code=400)
    except storage.UnknownFeedToken as exc:
        return func.HttpResponse(str(exc), status_code=404)

    # Concurrent: fetching a large watchlist one show at a time would risk
    # the platform's request timeout.
    shows_with_episodes = []
    transient_failures = 0
    fetched = _fetch_shows(show_ids, deadline=time.monotonic() + CALENDAR_LOOKUP_WAIT_SECONDS)
    for show_id, result in fetched.items():
        if isinstance(result, TVMazeError):
            # The show is genuinely gone from TVMaze. Skipping it is
            # correct and permanent, not a blip -- one dead ID must not
            # take down the rest of the feed.
            logging.warning("Skipping unknown TVMaze show_id=%s", show_id)
        elif isinstance(result, Exception):
            logging.error("TVMaze lookup failed for show_id=%s", show_id, exc_info=result)
            transient_failures += 1
        else:
            shows_with_episodes.append(result)

    if transient_failures:
        # Never serve a partial feed as 200. Calendar apps treat a success
        # as authoritative and delete every event missing from it, so a
        # TVMaze outage or rate-limit would silently wipe the subscriber's
        # episodes -- or flap them in and out as different shows fail on
        # each poll. An error response makes clients keep what they have.
        logging.error(
            "Refusing to serve a partial calendar: %d of %d TVMaze lookups failed.",
            transient_failures,
            len(show_ids),
        )
        return func.HttpResponse(
            "Couldn't reach TVMaze for some shows; refusing to serve an incomplete calendar.",
            status_code=502,
        )

    calendar = build_calendar(
        shows_with_episodes,
        since=datetime.now(timezone.utc) - timedelta(days=EPISODE_HISTORY_DAYS),
    )
    return func.HttpResponse(
        calendar.to_ical(),
        status_code=200,
        mimetype="text/calendar",
        headers={"Content-Disposition": "inline; filename=tv-shows.ics"},
    )
