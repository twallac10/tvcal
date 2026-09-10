"""Thin client for the public TVMaze API (https://www.tvmaze.com/api)."""
from __future__ import annotations

import time

import requests

TVMAZE_BASE_URL = "https://api.tvmaze.com"
REQUEST_TIMEOUT_SECONDS = 10

# TVMaze rate-limits per IP (roughly 20 requests / 10s). A calendar feed
# fans out one request per show concurrently, so a bigger watchlist can
# briefly exceed that and get 429s. Retry once, briefly, rather than
# letting a burst turn into a failed feed.
_RATE_LIMIT_STATUS = 429
_MAX_RETRIES = 1
_RETRY_BACKOFF_SECONDS = 1.0
_MAX_RETRY_SLEEP_SECONDS = 2.0


class TVMazeError(Exception):
    """Raised when TVMaze has no data for the requested resource."""


def _retry_delay(response: requests.Response) -> float:
    retry_after = (response.headers.get("Retry-After") or "").strip()
    delay = float(retry_after) if retry_after.isdigit() else _RETRY_BACKOFF_SECONDS
    return min(delay, _MAX_RETRY_SLEEP_SECONDS)


def _get(path: str, params: dict | None = None) -> requests.Response:
    for attempt in range(_MAX_RETRIES + 1):
        response = requests.get(
            f"{TVMAZE_BASE_URL}{path}", params=params, timeout=REQUEST_TIMEOUT_SECONDS
        )
        if response.status_code != _RATE_LIMIT_STATUS or attempt == _MAX_RETRIES:
            return response
        time.sleep(_retry_delay(response))
    return response


def search_shows(query: str) -> list[dict]:
    """Search TVMaze for shows matching ``query``, most relevant first."""
    response = _get("/search/shows", params={"q": query})
    response.raise_for_status()
    return [result["show"] for result in response.json()]


def get_show_with_episodes(show_id: int) -> tuple[dict, list[dict]]:
    """Fetch a show and its full episode list in a single TVMaze request."""
    response = _get(f"/shows/{show_id}", params={"embed": "episodes"})
    if response.status_code == 404:
        raise TVMazeError(f"No TVMaze show with id {show_id}")
    response.raise_for_status()
    show = response.json()
    episodes = show.pop("_embedded", {}).get("episodes", [])
    return show, episodes
