"""Azure Functions app: search TVMaze shows and subscribe to episode iCal feeds."""
import concurrent.futures
import json
import logging
import pathlib

import azure.functions as func

import auth
import storage
from ical_builder import build_calendar
from tvmaze_client import TVMazeError, get_show_with_episodes, search_shows

app = func.FunctionApp(http_auth_level=func.AuthLevel.ANONYMOUS)

STATIC_DIR = pathlib.Path(__file__).parent / "static"
MAX_SHOW_NAME_LENGTH = 200
MAX_SHOW_ID = 10**15
MAX_SHOW_IDS_PARAM = 50
MAX_CONCURRENT_TVMAZE_FETCHES = 10
MIN_PASSWORD_LENGTH = 8


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
    """POST /auth/signup  body: {"username", "password", "signup_code", "previous_token"?}.

    signup_code gates account creation (see the SIGNUP_CODE app setting) --
    it's shared out-of-band with whoever should get an account, same trust
    model as the old shared password, but only for this one step.
    previous_token, if given, is a pre-account browser's watchlist token
    (localStorage) to fold into the new account -- best-effort, ignored if
    missing/invalid so it never blocks signup itself.
    """
    try:
        body = req.get_json()
    except ValueError:
        return func.HttpResponse("Request body must be JSON.", status_code=400)

    username = body.get("username")
    password = body.get("password")
    signup_code = body.get("signup_code")
    previous_token = body.get("previous_token")

    if (
        not isinstance(username, str)
        or not isinstance(password, str)
        or not isinstance(signup_code, str)
        or not signup_code
    ):
        return func.HttpResponse(
            "Body must include 'username', 'password', and 'signup_code'.", status_code=400
        )
    if len(password) < MIN_PASSWORD_LENGTH:
        return func.HttpResponse(
            f"Password must be at least {MIN_PASSWORD_LENGTH} characters.", status_code=400
        )

    try:
        code_ok = auth.check_signup_code(signup_code)
    except auth.AuthConfigError:
        logging.error("Signup attempted but SIGNUP_CODE app setting is not configured.")
        return func.HttpResponse("Server auth is not configured.", status_code=500)
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

    if isinstance(previous_token, str) and previous_token:
        try:
            storage.migrate_watchlist(previous_token, normalized_username)
        except storage.InvalidListToken:
            pass  # stale/malformed previous_token -- signup still succeeds

    try:
        cookie = auth.create_session_cookie(normalized_username)
    except auth.AuthConfigError:
        logging.error("Signup succeeded but SESSION_SECRET is not configured.")
        return func.HttpResponse("Server auth is not configured.", status_code=500)

    return func.HttpResponse(status_code=204, headers={"Set-Cookie": cookie})


@app.route(route="auth/login", methods=["POST"])
def auth_login(req: func.HttpRequest) -> func.HttpResponse:
    """POST /auth/login  body: {"username": str, "password": str} -> sets the session cookie."""
    try:
        body = req.get_json()
    except ValueError:
        return func.HttpResponse("Request body must be JSON.", status_code=400)

    username = body.get("username")
    password = body.get("password")
    if not isinstance(username, str) or not username or not isinstance(password, str) or not password:
        return func.HttpResponse("Body must include 'username' and 'password'.", status_code=400)

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
    """GET /shows/search?q=<name> -> matching shows with their TVMaze IDs."""
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
                "network": ((show.get("network") or show.get("webChannel")) or {}).get("name"),
                "image": (show.get("image") or {}).get("medium"),
                "summary": show.get("summary"),
            }
            for show in shows
        ]
    except Exception:
        logging.exception("TVMaze search failed for query=%s", query)
        return func.HttpResponse("Failed to reach TVMaze.", status_code=502)

    return func.HttpResponse(json.dumps(results), status_code=200, mimetype="application/json")


@app.route(route="watchlist", methods=["GET"])
@auth.require_session
def watchlist_get(req: func.HttpRequest) -> func.HttpResponse:
    """GET /watchlist -> shows on the signed-in account's watchlist."""
    shows = storage.list_shows(auth.get_username(req))
    return func.HttpResponse(json.dumps(shows), status_code=200, mimetype="application/json")


@app.route(route="watchlist", methods=["POST"])
@auth.require_session
def watchlist_add(req: func.HttpRequest) -> func.HttpResponse:
    """POST /watchlist  body: {"show_id": int, "show_name": str} -> add to your watchlist."""
    try:
        body = req.get_json()
    except ValueError:
        return func.HttpResponse("Request body must be JSON.", status_code=400)

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


@app.route(route="watchlist/{show_id}", methods=["DELETE"])
@auth.require_session
def watchlist_remove(req: func.HttpRequest) -> func.HttpResponse:
    """DELETE /watchlist/{show_id} -> remove a show from your watchlist."""
    try:
        show_id = int(req.route_params["show_id"])
    except (KeyError, ValueError):
        return func.HttpResponse("show_id must be an integer.", status_code=400)

    storage.remove_show(auth.get_username(req), show_id)
    return func.HttpResponse(status_code=204)


@app.route(route="calendar.ics", methods=["GET"])
def calendar_feed(req: func.HttpRequest) -> func.HttpResponse:
    """GET /calendar.ics?feed=<feed_token> or ?show_ids=<id,id,...> -> iCal feed.

    The URL is meant to be pasted straight into a calendar app (Google
    Calendar, Apple Calendar, Outlook, ...) as a subscription so new episodes
    show up automatically as TVMaze publishes air dates. When built from a
    feed token (see GET /watchlist/feed-token), the same URL keeps working
    as shows are added/removed -- no need to re-subscribe. The feed token is
    read-only by design -- it is not the watchlist's write token, so it
    can't be used to add or remove shows even if the URL leaks.

    Deliberately not @auth.require_session: calendar apps fetch this URL
    directly with no cookies at all, so a session gate would just break
    subscriptions. The feed token is this endpoint's only gate.
    """
    feed_token = req.params.get("feed")
    raw_ids = req.params.get("show_ids")

    if feed_token:
        try:
            write_token = storage.resolve_feed_token(feed_token)
            show_ids = [show["id"] for show in storage.list_shows(write_token)]
        except storage.InvalidListToken as exc:
            return func.HttpResponse(str(exc), status_code=400)
        except storage.UnknownFeedToken as exc:
            return func.HttpResponse(str(exc), status_code=404)
    elif raw_ids:
        try:
            show_ids = list(
                dict.fromkeys(int(value.strip()) for value in raw_ids.split(",") if value.strip())
            )
        except ValueError:
            return func.HttpResponse(
                "show_ids must be a comma-separated list of TVMaze show IDs.", status_code=400
            )
        if not show_ids:
            return func.HttpResponse("No valid show IDs supplied.", status_code=400)
        if len(show_ids) > MAX_SHOW_IDS_PARAM:
            return func.HttpResponse(
                f"show_ids supports at most {MAX_SHOW_IDS_PARAM} shows per request.",
                status_code=400,
            )
    else:
        return func.HttpResponse(
            "Query parameter 'feed' (calendar feed token) or 'show_ids' is required.",
            status_code=400,
        )

    # Best-effort and concurrent: a single missing/broken show shouldn't blank
    # out the whole subscription for every other show, and fetching shows one
    # at a time would risk the platform's request timeout on a large watchlist.
    shows_with_episodes = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=MAX_CONCURRENT_TVMAZE_FETCHES) as pool:
        future_to_show_id = {pool.submit(get_show_with_episodes, show_id): show_id for show_id in show_ids}
        for future in concurrent.futures.as_completed(future_to_show_id):
            show_id = future_to_show_id[future]
            try:
                shows_with_episodes.append(future.result())
            except TVMazeError:
                logging.warning("Skipping unknown TVMaze show_id=%s", show_id)
            except Exception:
                logging.exception("TVMaze lookup failed for show_id=%s", show_id)

    calendar = build_calendar(shows_with_episodes)
    return func.HttpResponse(
        calendar.to_ical(),
        status_code=200,
        mimetype="text/calendar",
        headers={"Content-Disposition": "inline; filename=tv-shows.ics"},
    )
