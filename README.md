# tvcal

Find TV shows and subscribe to an iCal feed of their episode air dates, backed
by the [TVMaze API](https://www.tvmaze.com/api). Ships as a Python Azure
Functions app with two HTTP endpoints — no database, no auth, nothing to
provision besides the Function App itself.

## Endpoints

### `GET /api/shows/search?q=<name>`

Search TVMaze for shows by name. Returns JSON with each show's TVMaze `id`,
which you need for the calendar feed.

```
GET /api/shows/search?q=fringe
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

### `GET /api/calendar.ics?show_ids=<id,id,...>`

Returns an `.ics` feed with one event per aired/upcoming episode across all
listed shows (comma-separated TVMaze show IDs). Paste this URL into Google
Calendar, Apple Calendar, or Outlook as a **subscription** (not a one-time
import) so new episodes appear automatically as TVMaze publishes air dates.

```
GET /api/calendar.ics?show_ids=82,143
```

Each event's summary is `<Show> - S01E01 - <Episode Title>`, timed at the
episode's air date/time with a duration from the episode (or show) runtime.

## Running locally

Requires [Azure Functions Core Tools v4](https://learn.microsoft.com/azure/azure-functions/functions-run-local)
and Python 3.10+.

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements-dev.txt
cp local.settings.json.example local.settings.json
func start
```

Then try:

```bash
curl "http://localhost:7071/api/shows/search?q=fringe"
curl "http://localhost:7071/api/calendar.ics?show_ids=82"
```

## Tests

```bash
pip install -r requirements-dev.txt
pytest
```

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

## Project layout

- `function_app.py` — HTTP-triggered functions (search, calendar feed)
- `tvmaze_client.py` — thin wrapper around the TVMaze REST API
- `ical_builder.py` — builds the `.ics` calendar from TVMaze show/episode data
- `tests/` — unit tests (mocked TVMaze responses, no network calls)
