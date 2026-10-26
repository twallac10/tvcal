"""Thin client for the public TVMaze API (https://www.tvmaze.com/api)."""
from __future__ import annotations

import collections
import threading
import time
from datetime import datetime, timezone

import requests

TVMAZE_BASE_URL = "https://api.tvmaze.com"
REQUEST_TIMEOUT_SECONDS = 10

# TVMaze allows at least 20 requests per 10 seconds per IP, then 429s. A
# calendar feed or a search fans out one request per show, so unpaced, a
# watchlist of more than ~20 shows tripped the limit on every fetch -- and
# one failed show 502s the whole feed. Requests are now paced to stay just
# under it, so a 429 should only come from something else sharing the
# outbound IP; those are retried while the caller's deadline allows.
_RATE_LIMIT_CALLS = 18
_RATE_LIMIT_WINDOW_SECONDS = 10.0
_RATE_LIMIT_STATUS = 429
_MAX_RETRIES = 3
_RETRY_BACKOFF_SECONDS = 2.0
_MAX_RETRY_SLEEP_SECONDS = 5.0
DEFAULT_WAIT_SECONDS = 30.0

# Air dates are announced days or weeks ahead, so a show's data barely
# changes within an hour. Caching it (per instance) spares TVMaze -- and the
# rate limit -- the repeats from refined searches, the watchlist page, and
# calendar polls.
_CACHE_TTL_SECONDS = 60 * 60
_CACHE_MAX_ENTRIES = 500


class TVMazeError(Exception):
    """Raised when TVMaze has no data for the requested resource."""


class TVMazeBusy(Exception):
    """Raised when the rate limit leaves no request slot before the caller's
    deadline. Transient, unlike TVMazeError."""


class _RateLimiter:
    """Process-wide sliding window: at most `calls` requests per `window` seconds."""

    def __init__(self, calls: int, window: float):
        self._calls = calls
        self._window = window
        self._sent = collections.deque()
        self._lock = threading.Lock()

    def acquire(self, deadline: float) -> None:
        while True:
            with self._lock:
                now = time.monotonic()
                while self._sent and now - self._sent[0] >= self._window:
                    self._sent.popleft()
                if len(self._sent) < self._calls:
                    self._sent.append(now)
                    return
                wait = self._sent[0] + self._window - now
            if now + wait > deadline:
                raise TVMazeBusy("No TVMaze request slot free before the deadline.")
            time.sleep(wait)


class _ShowCache:
    """Small TTL cache of get_show_with_episodes() results, keyed by show id."""

    def __init__(self, ttl: float, max_entries: int):
        self._ttl = ttl
        self._max_entries = max_entries
        self._entries = {}
        self._lock = threading.Lock()

    def get(self, show_id):
        with self._lock:
            entry = self._entries.get(show_id)
            if entry is not None and entry[0] > time.monotonic():
                return entry[1]
            return None

    def put(self, show_id, value) -> None:
        with self._lock:
            now = time.monotonic()
            if show_id not in self._entries and len(self._entries) >= self._max_entries:
                self._entries = {k: v for k, v in self._entries.items() if v[0] > now}
                while len(self._entries) >= self._max_entries:
                    self._entries.pop(next(iter(self._entries)))  # oldest first
            self._entries[show_id] = (now + self._ttl, value)


_limiter = _RateLimiter(_RATE_LIMIT_CALLS, _RATE_LIMIT_WINDOW_SECONDS)
_cache = _ShowCache(_CACHE_TTL_SECONDS, _CACHE_MAX_ENTRIES)


def _retry_delay(response: requests.Response) -> float:
    retry_after = (response.headers.get("Retry-After") or "").strip()
    delay = float(retry_after) if retry_after.isdigit() else _RETRY_BACKOFF_SECONDS
    return min(delay, _MAX_RETRY_SLEEP_SECONDS)


def _get(path: str, params: dict | None = None, deadline: float | None = None) -> requests.Response:
    """GET from TVMaze, paced under its rate limit. `deadline` (a
    time.monotonic() value) bounds how long this may wait for a slot or a
    retry; past it, a slot wait raises TVMazeBusy and a 429 is returned."""
    if deadline is None:
        deadline = time.monotonic() + DEFAULT_WAIT_SECONDS
    for attempt in range(_MAX_RETRIES + 1):
        _limiter.acquire(deadline)
        response = requests.get(
            f"{TVMAZE_BASE_URL}{path}", params=params, timeout=REQUEST_TIMEOUT_SECONDS
        )
        if response.status_code != _RATE_LIMIT_STATUS or attempt == _MAX_RETRIES:
            return response
        delay = _retry_delay(response)
        if time.monotonic() + delay > deadline:
            return response
        time.sleep(delay)
    return response


def search_shows(query: str) -> list[dict]:
    """Search TVMaze for shows matching ``query``, most relevant first."""
    response = _get("/search/shows", params={"q": query})
    response.raise_for_status()
    return [result["show"] for result in response.json()]


def get_show_with_episodes(show_id: int, deadline: float | None = None) -> tuple[dict, list[dict]]:
    """Fetch a show and its full episode list in a single TVMaze request.

    Results are cached for an hour and shared between callers, so treat
    them as read-only.
    """
    cached = _cache.get(show_id)
    if cached is not None:
        return cached
    response = _get(f"/shows/{show_id}", params={"embed": "episodes"}, deadline=deadline)
    if response.status_code == 404:
        raise TVMazeError(f"No TVMaze show with id {show_id}")
    response.raise_for_status()
    show = response.json()
    episodes = show.pop("_embedded", {}).get("episodes", [])
    _cache.put(show_id, (show, episodes))
    return show, episodes


def network_name(show: dict) -> str | None:
    """Where a show airs: its TV network, or its streaming service."""
    for key in ("network", "webChannel"):
        channel = show.get(key)
        if isinstance(channel, dict) and isinstance(channel.get("name"), str):
            return channel["name"]
    return None


def _int_or_none(value) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def upcoming_episodes(episodes, now: datetime, limit: int = 2) -> list[dict]:
    """The next `limit` episodes airing after `now`, soonest first.

    TVMaze data is untrusted input, so anything malformed (a non-list, a
    non-dict episode, a missing or unparseable airstamp) is skipped rather
    than raised -- this feeds search results, where one odd show shouldn't
    break the page.
    """
    if not isinstance(episodes, list):
        return []

    upcoming = []
    for episode in episodes:
        if not isinstance(episode, dict) or not isinstance(episode.get("airstamp"), str):
            continue
        try:
            airs_at = datetime.fromisoformat(episode["airstamp"])
        except ValueError:
            continue
        if airs_at.tzinfo is None:
            airs_at = airs_at.replace(tzinfo=timezone.utc)
        if airs_at > now:
            upcoming.append((airs_at, episode))

    upcoming.sort(key=lambda pair: pair[0])
    return [
        {
            "airstamp": airs_at.isoformat(),
            "season": _int_or_none(episode.get("season")),
            "number": _int_or_none(episode.get("number")),
        }
        for airs_at, episode in upcoming[:limit]
    ]
