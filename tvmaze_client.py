"""Thin client for the public TVMaze API (https://www.tvmaze.com/api)."""
from __future__ import annotations

import requests

TVMAZE_BASE_URL = "https://api.tvmaze.com"
REQUEST_TIMEOUT_SECONDS = 10


class TVMazeError(Exception):
    """Raised when TVMaze has no data for the requested resource."""


def search_shows(query: str) -> list[dict]:
    """Search TVMaze for shows matching ``query``, most relevant first."""
    response = requests.get(
        f"{TVMAZE_BASE_URL}/search/shows",
        params={"q": query},
        timeout=REQUEST_TIMEOUT_SECONDS,
    )
    response.raise_for_status()
    return [result["show"] for result in response.json()]


def get_show(show_id: int) -> dict:
    """Fetch a single show's details by its TVMaze ID."""
    response = requests.get(
        f"{TVMAZE_BASE_URL}/shows/{show_id}", timeout=REQUEST_TIMEOUT_SECONDS
    )
    if response.status_code == 404:
        raise TVMazeError(f"No TVMaze show with id {show_id}")
    response.raise_for_status()
    return response.json()


def get_show_episodes(show_id: int) -> list[dict]:
    """Fetch the full episode list (aired and upcoming) for a show."""
    response = requests.get(
        f"{TVMAZE_BASE_URL}/shows/{show_id}/episodes",
        timeout=REQUEST_TIMEOUT_SECONDS,
    )
    if response.status_code == 404:
        raise TVMazeError(f"No TVMaze show with id {show_id}")
    response.raise_for_status()
    return response.json()
