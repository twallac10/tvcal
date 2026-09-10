"""Builds an iCalendar feed of episode air dates from TVMaze data."""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone

from icalendar import Calendar, Event

DEFAULT_RUNTIME_MINUTES = 30
_HTML_TAG_RE = re.compile(r"<[^<]+?>")


def build_calendar(shows_with_episodes: list[tuple[dict, list[dict]]]) -> Calendar:
    calendar = Calendar()
    calendar.add("prodid", "-//tvcal//TV Show Episode Tracker//EN")
    calendar.add("version", "2.0")
    calendar.add("x-wr-calname", "TV Show Episodes")
    calendar.add("method", "PUBLISH")

    for show, episodes in shows_with_episodes:
        for episode in episodes:
            event = build_event(show, episode)
            if event is not None:
                calendar.add_component(event)

    return calendar


def build_event(show: dict, episode: dict) -> Event | None:
    airstamp = episode.get("airstamp")
    if not airstamp:
        # Episode has no confirmed air date/time yet.
        return None

    start = datetime.fromisoformat(airstamp)
    runtime_minutes = episode.get("runtime") or show.get("runtime") or DEFAULT_RUNTIME_MINUTES
    end = start + timedelta(minutes=runtime_minutes)

    event = Event()
    event.add("uid", f"tvmaze-episode-{episode['id']}@tvcal")
    event.add("summary", _format_episode_title(show, episode))
    event.add("dtstart", start)
    event.add("dtend", end)
    event.add("dtstamp", datetime.now(timezone.utc))
    event.add("description", _clean_summary(episode.get("summary")))
    if episode.get("url"):
        event.add("url", episode["url"])
    return event


def _format_episode_title(show: dict, episode: dict) -> str:
    code = _format_episode_code(episode)
    name = episode.get("name") or "TBA"
    show_name = show.get("name", "Unknown Show")
    return f"{show_name} - {code} - {name}" if code else f"{show_name} - {name}"


def _format_episode_code(episode: dict) -> str:
    season = episode.get("season")
    number = episode.get("number")
    if season is None or number is None:
        return ""
    return f"S{season:02d}E{number:02d}"


def _clean_summary(summary: str | None) -> str:
    if not summary:
        return ""
    return _HTML_TAG_RE.sub("", summary).strip()
