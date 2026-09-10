# tvcal

Find TV shows, build a watchlist, and subscribe to one iCal feed of episode
air dates — backed by the [TVMaze API](https://www.tvmaze.com/api). Ships as
a single Python Azure Functions app: HTTP API plus a small static UI, backed
by Azure Table Storage for the watchlist.

Open the Function App's root URL for the UI, or use the HTTP API directly.

## Signing in

The UI and its write API (search, watchlist add/remove, feed-token issuance)
sit behind a single shared password — there's no per-user account system,
just a login wall so a stumbled-upon URL is useless without it. Sign in once
at `/`; a session cookie (HttpOnly, 30 days) keeps you logged in after that,
with a "Log out" button to end it early.

`/calendar.ics` is deliberately **not** behind this login: calendar apps
fetch that URL directly with no cookies at all, so it's gated only by its
own read-only feed token instead (see below) — that's the whole reason the
write and feed tokens are separate.

The password is never stored in plaintext — only its SHA-256 hash, as the
`ACCESS_PASSWORD_HASH` app setting (see **Deploying to Azure** below for how
to set it). To change the password, generate a new hash and update that
setting; existing sessions stay valid until they expire since they're only
signed with `SESSION_SECRET`, not tied to the password itself.

## How watchlists work

There's no login. Each watchlist has two separate tokens:

- A **write token** — an opaque UUID generated in the browser and kept in
  the URL/localStorage (`?list=<token>`) — that can view and edit the
  watchlist. Treat it like a lightweight password to your list; bookmarking
  its URL gets you back to the same watchlist.
- A **feed token** — opaque and server-issued, fetched once via
  `GET /watchlist/feed-token` — that can only be used to read the calendar
  feed. It's what actually goes in the `.ics` subscription URL.

They're deliberately different tokens so that pasting the calendar URL into
Google/Apple/Outlook (which store and periodically re-fetch it, and which
you might share a calendar containing it) never hands out edit access to
the watchlist — only the write token, which never leaves the management
UI, can add or remove shows.

The calendar URL built from a feed token (`/calendar.ics?feed=<token>`)
stays stable as you add or remove shows via the write token — subscribe
once in your calendar app and it keeps updating.

## Endpoints

Every endpoint below except `/calendar.ics` requires a valid session cookie
(see **Signing in**) — `POST /auth/login` with `{"password": "..."}` sets
one, `POST /auth/logout` clears it, `GET /auth/status` reports
`{"authenticated": bool}` for the current cookie.

### `GET /shows/search?q=<name>`

Search TVMaze for shows by name.

```
GET /shows/search?q=fringe
```

```json
[
  {
    "id": 82,
    "name": "Fringe",
    "premiered": "2008-09-09",
    "status": "Ended",
    "network": "FOX",
    "image": "https://static.tvmaze.com/uploads/images/medium_portrait/0/2400.jpg",
    "summary": "<p>...</p>"
  }
]
```

### `GET /watchlist?list=<token>`

Returns the shows currently on that watchlist: `[{"id": 82, "name": "Fringe"}, ...]`.

### `POST /watchlist?list=<token>`

Body: `{"show_id": 82, "show_name": "Fringe"}`. Adds (or re-adds) a show.
`204 No Content` on success. `409 Conflict` once a watchlist hits 100 shows.

### `DELETE /watchlist/{show_id}?list=<token>`

Removes a show from the watchlist. `204 No Content` on success (idempotent).

### `GET /watchlist/feed-token?list=<token>`

Returns (creating on first call) the read-only feed token for a watchlist:
`{"feed_token": "..."}`. Idempotent — repeat calls return the same token.

### `GET /calendar.ics?feed=<feed_token>` or `?show_ids=<id,id,...>`

Returns an `.ics` feed with one event per aired/upcoming episode. Use
`feed=<feed_token>` (from `GET /watchlist/feed-token`) for a stable,
auto-updating feed tied to a watchlist, or `show_ids=82,143` (max 50 IDs)
for a one-off feed built from specific TVMaze show IDs without going
through a watchlist at all.

```
GET /calendar.ics?feed=RmVlZFRva2VuRXhhbXBsZQ
```

Each event's summary is `<Show> - S01E01 - <Episode Title>`, timed at the
episode's air date/time with a duration from the episode (or show) runtime.
If TVMaze can't return data for one show (deleted, renamed ID, transient
error), that show is skipped and logged rather than failing the whole feed.

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

Then open `http://localhost:7071/` for the UI and sign in with `changeme`
(the password baked into `local.settings.json.example` — see **Signing in**
above; fine for local dev, never use it in production). Signed-in API calls
need the session cookie, so for `curl` either drive it through a cookie jar:

```bash
curl -c /tmp/tvcal-cookies -X POST http://localhost:7071/auth/login \
  -H "Content-Type: application/json" -d '{"password":"changeme"}'
curl -b /tmp/tvcal-cookies "http://localhost:7071/shows/search?q=fringe"
```

or use `/calendar.ics?show_ids=...`, which never needs a session:

```bash
curl "http://localhost:7071/calendar.ics?show_ids=82"
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
# ACCESS_PASSWORD_HASH/SESSION_SECRET gate the app behind a login -- see
# "Signing in" above. Generate real values, don't reuse the example ones:
#   python3 -c "import hashlib; print(hashlib.sha256(b'<your password>').hexdigest())"
#   python3 -c "import secrets; print(secrets.token_hex(32))"
az functionapp config appsettings set \
  --resource-group <resource-group> \
  --name <function-app-name> \
  --settings \
    AzureWebJobsDisableHomepage=true \
    ACCESS_PASSWORD_HASH=<sha256 hash from above> \
    SESSION_SECRET=<random hex from above>

func azure functionapp publish <function-app-name> --python
```

Beyond the ones Azure Functions provisions automatically
(`AzureWebJobsStorage`, `FUNCTIONS_WORKER_RUNTIME`), the required app
settings are the three above: `AzureWebJobsDisableHomepage=true` (without it
the UI at `/` is shadowed by Azure's default placeholder page even though
every other route works), and `ACCESS_PASSWORD_HASH`/`SESSION_SECRET` (the
login gate — without them, every session-gated route fails closed and
nobody can sign in at all). `AzureWebJobsStorage` is also where the
watchlist table lives — no separate storage account needed beyond the one
linked at creation.

## Project layout

- `function_app.py` — HTTP-triggered functions (UI, auth, search, watchlist, calendar feed)
- `auth.py` — the shared-password login gate (session cookies, no external identity provider)
- `tvmaze_client.py` — thin wrapper around the TVMaze REST API
- `ical_builder.py` — builds the `.ics` calendar from TVMaze show/episode data
- `storage.py` — watchlist persistence in Azure Table Storage
- `static/index.html` — the login + search + watchlist single-page UI
- `tests/` — unit tests (mocked TVMaze/Table Storage, no network calls)
