# tvcal

Find TV shows, build a watchlist, and subscribe to one iCal feed of episode
air dates — backed by the [TVMaze API](https://www.tvmaze.com/api). Ships as
a single Python Azure Functions app: HTTP API plus a small static UI, backed
by Azure Table Storage for the watchlist. No separate hosting, no
authentication system to build.

Open the Function App's root URL for the UI, or use the HTTP API directly.

## How watchlists work

There's no login. Each watchlist is identified by an opaque, random token
(a UUID generated in the browser and kept in the URL/localStorage) — anyone
holding the token can view or edit that list, the same trust model as an
unguessable calendar subscription link. The UI creates a token on first
visit and appends it to the page URL (`?list=<token>`), so bookmarking that
URL gets you back to the same watchlist.

The calendar URL built from a token (`/calendar.ics?list=<token>`) stays
stable as you add or remove shows — subscribe once in your calendar app and
it keeps updating.

## Endpoints

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
`204 No Content` on success.

### `DELETE /watchlist/{show_id}?list=<token>`

Removes a show from the watchlist. `204 No Content` on success (idempotent).

### `GET /calendar.ics?list=<token>` or `?show_ids=<id,id,...>`

Returns an `.ics` feed with one event per aired/upcoming episode. Use
`list=<token>` for a stable, auto-updating feed tied to a watchlist, or
`show_ids=82,143` for a one-off feed built from specific TVMaze show IDs
without going through the watchlist at all.

```
GET /calendar.ics?list=9f2c6e2a-3b34-4b1a-9a2b-3a0d9d7b6b2a
```

Each event's summary is `<Show> - S01E01 - <Episode Title>`, timed at the
episode's air date/time with a duration from the episode (or show) runtime.

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

Then open `http://localhost:7071/` for the UI, or:

```bash
curl "http://localhost:7071/shows/search?q=fringe"
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
az functionapp create \
  --resource-group <resource-group> \
  --consumption-plan-location <region> \
  --runtime python \
  --runtime-version 3.11 \
  --functions-version 4 \
  --name <function-app-name> \
  --storage-account <storage-account-name> \
  --os-type Linux

func azure functionapp publish <function-app-name>
```

The app has no required app settings beyond the ones Azure Functions
provisions automatically (`AzureWebJobsStorage`, `FUNCTIONS_WORKER_RUNTIME`).
`AzureWebJobsStorage` is also where the watchlist table lives — no separate
storage account needed.

## Project layout

- `function_app.py` — HTTP-triggered functions (UI, search, watchlist, calendar feed)
- `tvmaze_client.py` — thin wrapper around the TVMaze REST API
- `ical_builder.py` — builds the `.ics` calendar from TVMaze show/episode data
- `storage.py` — watchlist persistence in Azure Table Storage
- `static/index.html` — the search + watchlist single-page UI
- `tests/` — unit tests (mocked TVMaze/Table Storage, no network calls)
