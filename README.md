# tvcal

Find TV shows, build a watchlist, and subscribe to one iCal feed of episode
air dates — backed by the [TVMaze API](https://www.tvmaze.com/api). Ships as
a single Python Azure Functions app: HTTP API plus a small static UI, backed
by Azure Table Storage for the watchlist.

Open the Function App's root URL for the UI, or use the HTTP API directly.

## Signing in

Real (if lightweight) accounts: each person signs up with their own
username and password, so signing in from any device reaches the same
watchlist — no more per-browser tokens to copy around. There's still no
external identity provider; accounts live in Table Storage with
PBKDF2-hashed passwords (per-account salt, never stored in plaintext).

Self-serve signup at `/` (the "Need an account? Sign up" link) is gated by
a shared **invite code** — the `SIGNUP_CODE` app setting — so it's still
just the people you give the code to who can create an account, not the
whole internet; `storage.MAX_ACCOUNTS` (10) is a hard backstop on top of
that. Share the code with whoever should get an account, then consider
rotating/blanking `SIGNUP_CODE` once everyone who needs one has signed up.

A session cookie (HttpOnly, 30 days) keeps you signed in after that, with a
"Log out" button to end it early.

**What this deliberately doesn't have**, since it's a two-person app: no
password change, no password reset, and no per-account session revocation.
If a password needs changing you edit (or delete) that account's row in
Table Storage by hand; the only session kill switch is rotating
`SESSION_SECRET`, which signs everyone out at once.

`/calendar.ics` is deliberately **not** behind this login: calendar apps
fetch that URL directly with no cookies at all, so it's gated only by its
own read-only feed token instead (see below) — that's the whole reason the
feed token exists separately from your login.

## How watchlists work

A watchlist belongs to an account (see **Signing in**) — sign in from any
device and you're looking at the same list. Each watchlist also has its own
**feed token** — opaque and server-issued, fetched once via
`GET /watchlist/feed-token` — that can only be used to read the calendar
feed, never to add or remove shows. That's what actually goes in the
`.ics` subscription URL.

The feed token is deliberately separate from your login so that pasting the
calendar URL into Google/Apple/Outlook (which store and periodically
re-fetch it, and which you might share a calendar containing it) never
hands out edit access to your watchlist — only signing in with your
username and password can add or remove shows.

The calendar URL built from a feed token (`/calendar.ics?feed=<token>`)
stays stable as you add or remove shows — subscribe once in your calendar
app and it keeps updating. The UI's "Add to Apple Calendar" / "Add to Google
Calendar" buttons subscribe to it in one tap. If the link ever leaks, "Reset
calendar link" revokes it and issues a new one (calendars on the old link
stop updating and need re-subscribing).

## Endpoints

Every endpoint below except `/`, `/auth/*`, and `/calendar.ics` requires a
valid session cookie (see **Signing in**).

- `POST /auth/signup` — body `{"username", "password", "signup_code"}`.
  Creates an account (`password` must be 8+ characters, `username` 3-50
  characters of letters/digits/`-`/`_`) and signs you in.
  `409` if the username's taken, `401` for a wrong `signup_code`, `403`
  once `MAX_ACCOUNTS` is reached or if no `SIGNUP_CODE` is configured at
  all (signups closed). Passwords are capped at 1024 characters — these
  endpoints are anonymous and always run PBKDF2.
- `POST /auth/login` — body `{"username", "password"}`. Sets the session cookie.
- `POST /auth/logout` — clears the session cookie.
- `GET /auth/status` — `{"authenticated": bool, "username": str | null}` for
  the current cookie.

### `GET /shows/search?q=<name>`

Search TVMaze for shows by name, including each show's next two upcoming
episodes.

```
GET /shows/search?q=traitors
```

```json
[
  {
    "id": 59434,
    "name": "The Traitors",
    "premiered": "2023-01-12",
    "status": "Running",
    "network": "Peacock",
    "image": "https://static.tvmaze.com/uploads/images/medium_portrait/...jpg",
    "summary": "<p>...</p>",
    "next_episodes": [
      {"airstamp": "2026-10-30T01:00:00+00:00", "airdate": null, "season": 4, "number": 2},
      {"airstamp": "2026-11-06T01:00:00+00:00", "airdate": null, "season": 4, "number": 3}
    ]
  }
]
```

TVMaze's search doesn't include episodes, so `next_episodes` costs one
extra TVMaze request per result. Those requests run concurrently, and only
for shows TVMaze reports as having a next episode scheduled, which skips
ended shows. `next_episodes` is `[]` when nothing is scheduled and `null`
when the lookup failed or didn't finish within a few seconds (see **TVMaze
rate limits** below). A failed lookup only blanks that one show's airdates;
the search itself still succeeds.

`airdate` (`YYYY-MM-DD`) is set only for an episode with no announced air
time, typical of streaming releases. Its `airstamp` hour is then a
placeholder, so show `airdate` as-is rather than converting `airstamp` to
local time, which can land on the wrong day. The calendar uses the same
date for its all-day event.

### `GET /watchlist`

Returns the shows on your watchlist: `[{"id": 82, "name": "Fringe"}, ...]`.

### `GET /watchlist/upcoming`

The next episodes for each show on your watchlist:
`[{"id": 82, "status": "Ended", "next_episodes": []}, ...]`, with
`next_episodes` shaped as in search. Separate from `GET /watchlist` because
it costs a TVMaze lookup per show, so the UI renders the list first and
fills airdates in when this answers. Best-effort per show: `status` and
`next_episodes` are `null` when that show's lookup failed or ran past its
time budget.

### `POST /watchlist`

Body: `{"show_id": 82, "show_name": "Fringe"}`. Adds (or re-adds) a show.
`204 No Content` on success. `409 Conflict` once a watchlist hits 100 shows.

### `DELETE /watchlist/{show_id}`

Removes a show from your watchlist. `204 No Content` on success (idempotent).

### `GET /watchlist/feed-token`

Returns (creating on first call) the read-only feed token for your
watchlist: `{"feed_token": "..."}`. Idempotent — repeat calls return the
same token.

### `POST /watchlist/feed-token/reset`

Replaces your feed token with a new one and revokes the old one:
`{"feed_token": "..."}`. Calendars subscribed with the old link stop
updating.

### `GET /calendar.ics?feed=<feed_token>`

Returns an `.ics` feed with one event per aired/upcoming episode on the
watchlist that `feed_token` (from `GET /watchlist/feed-token`) belongs to.
Stable and auto-updating: subscribe once and it follows the watchlist.

A valid feed token is the only way in. This endpoint takes no session
cookie (calendar apps can't send one), so the token is what stops an
anonymous caller from making it fan out requests to TVMaze.

```
GET /calendar.ics?feed=RmVlZFRva2VuRXhhbXBsZQ
```

Each event's summary is `<Show> - S01E01 - <Episode Title>`, timed at the
episode's air date/time with a duration from the episode (or show) runtime,
and its location is the network or streaming service. An episode TVMaze has
no air *time* for (typical of streaming releases) is an all-day event on
its release date instead, since its hour would be a placeholder. Episodes
that aired more than 30 days ago are left out, so adding a long-running show
doesn't fill the calendar with years of history. Since calendar apps delete
events that drop out of a feed, that also means episodes disappear from
your calendar about a month after they air.

A show that TVMaze no longer knows about (deleted or renumbered ID) is
skipped and logged — one dead ID shouldn't take down the rest of the feed.
A *transient* failure (TVMaze down, rate-limiting, a network error) is
different: the whole request returns `502` rather than a `200` missing
those shows, because calendar apps treat a success as authoritative and
delete every event absent from it — so serving a partial feed would
silently wipe or flap the subscriber's episodes.

### TVMaze rate limits

TVMaze allows at least 20 requests per 10 seconds per IP. The calendar feed,
search, and watchlist airdates all fan out one request per show, so the
client:
- paces its requests to stay just under that limit,
- retries a 429 with backoff,
- shares one request between concurrent lookups of the same show, and
- caches each show's recent and upcoming episodes for an hour.

The cache is per Function instance, and idle instances are recycled, so it
mostly helps bursts (refining a search, browsing the watchlist), not
calendar polls hours apart. A cold calendar fetch for a big watchlist is
slower (about 5 seconds per 10 shows) rather than failing.

Each request has a total time budget for its lookups, covering both the
rate-limit wait and the download: 5 seconds for search, 20 for the
watchlist's airdates, and 150 for the calendar. Search and watchlist
lookups still running at the deadline show no airdate (and finish in the
background, warming the cache). The calendar answers `502` instead, for the
same reason it never serves a partial feed.

## Running locally

Requires [Azure Functions Core Tools v4](https://learn.microsoft.com/azure/azure-functions/functions-run-local),
Python 3.10+, and [Azurite](https://learn.microsoft.com/azure/storage/common/storage-use-azurite)
(the watchlist uses Azure Table Storage; Azurite emulates it locally).

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements-dev.txt
cp local.settings.json.example local.settings.json

# in a separate terminal: start the storage emulator
azurite --silent --location .azurite &

func start
```

Then open `http://localhost:7071/` for the UI, click "Need an account? Sign
up", and use invite code `changeme` (the value baked into
`local.settings.json.example` — see **Signing in** above; fine for local
dev, never use it in production) to create yourself an account. Every API
call except `/calendar.ics` needs the session cookie, so drive `curl`
through a cookie jar:

```bash
curl -c /tmp/tvcal-cookies -X POST http://localhost:7071/auth/signup \
  -H "Content-Type: application/json" \
  -d '{"username":"me","password":"a-real-password","signup_code":"changeme"}'

curl -b /tmp/tvcal-cookies "http://localhost:7071/shows/search?q=fringe"

# the calendar feed takes no cookie -- grab its token, then fetch it
curl -b /tmp/tvcal-cookies "http://localhost:7071/watchlist/feed-token"
curl "http://localhost:7071/calendar.ics?feed=<feed_token from above>"
```

## Tests

```bash
pip install -r requirements-dev.txt
pytest
```

Tests mock TVMaze and Table Storage, so `pytest` doesn't require Azurite or
network access.

## Deploying to Azure

```bash
# storage account names: 3-24 chars, lowercase letters + digits only, globally unique
az storage account create \
  --name <storage-account-name> \
  --resource-group <resource-group> \
  --location <region> \
  --sku Standard_LRS

az functionapp create \
  --resource-group <resource-group> \
  --consumption-plan-location <region> \
  --runtime python \
  --runtime-version 3.11 \
  --functions-version 4 \
  --name <function-app-name> \
  --storage-account <storage-account-name> \
  --os-type Linux

# Azure Functions serves a built-in "your app is up and running" placeholder
# at the bare root URL by default, which overrides the UI's route="/" function
# even though it's registered correctly. This setting disables that.
#
# SIGNUP_CODE is the invite code people need to create an account (share it
# out-of-band with them, then consider rotating/blanking it once everyone
# who needs an account has one). SESSION_SECRET signs session cookies.
# Generate real values, don't reuse the local-dev example ones:
#   python3 -c "import secrets; print(secrets.token_urlsafe(16))"   # SIGNUP_CODE
#   python3 -c "import secrets; print(secrets.token_hex(32))"       # SESSION_SECRET
az functionapp config appsettings set \
  --resource-group <resource-group> \
  --name <function-app-name> \
  --settings \
    AzureWebJobsDisableHomepage=true \
    SIGNUP_CODE=<value from above> \
    SESSION_SECRET=<random hex from above>

func azure functionapp publish <function-app-name> --python
```

Beyond the ones Azure Functions provisions automatically
(`AzureWebJobsStorage`, `FUNCTIONS_WORKER_RUNTIME`), the required app
settings are the three above: `AzureWebJobsDisableHomepage=true` (without it
the UI at `/` is shadowed by Azure's default placeholder page even though
every other route works), `SIGNUP_CODE` (without it nobody can create an
account), and `SESSION_SECRET` (signs session cookies — without it every
session-gated route fails closed). `AzureWebJobsStorage` is also where the
watchlist and account tables live — no separate storage account needed
beyond the one linked at creation.

After the first account or two are created, consider rotating `SIGNUP_CODE`
to something only you know — or blanking it entirely, which closes signups
(`/auth/signup` then returns `403 Signups are closed.`) — so the invite
code can't be reused by someone who came across it once:

```bash
az functionapp config appsettings set \
  --resource-group <resource-group> \
  --name <function-app-name> \
  --settings SIGNUP_CODE=<new value>
```

## Project layout

- `function_app.py` — HTTP-triggered functions (UI, auth, search, watchlist, calendar feed)
- `auth.py` — account login (PBKDF2 password hashing, signup-code-gated signup, session cookies, no external identity provider)
- `tvmaze_client.py` — thin wrapper around the TVMaze REST API
- `ical_builder.py` — builds the `.ics` calendar from TVMaze show/episode data
- `storage.py` — watchlist and account persistence in Azure Table Storage
- `static/index.html` — the login + search + watchlist single-page UI
- `tests/` — unit tests (mocked TVMaze/Table Storage, no network calls)
