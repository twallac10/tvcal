"""Builds an iCalendar feed of episode air dates from TVMaze data."""
from __future__ import annotations

import html
import logging
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
            try:
                event = build_event(show, episode)
            except (TypeError, ValueError):
                # TVMaze data is untrusted input -- an episode with a
                # malformed field (e.g. a non-numeric runtime/season/number)
                # shouldn't take down every other show's events with it.
                logging.warning(
                    "Skipping malformed episode %r for show %r",
                    episode.get("id"),
                    show.get("id"),
                )
                continue
            if event is not None:
                calendar.add_component(event)

    return calendar


def build_event(show: dict, episode: dict) -> Event | None:
    episode_id = episode.get("id")
    airstamp = episode.get("airstamp")
    if episode_id is None or not airstamp:
        # Episode has no confirmed air date/time yet, or isn't a real episode row.
        return None

    try:
        start = datetime.fromisoformat(airstamp)
    except ValueError:
        logging.warning("Skipping episode %s with unparseable airstamp %r", episode_id, airstamp)
        return None

    runtime_minutes = episode.get("runtime")
    if runtime_minutes is None:
        runtime_minutes = show.get("runtime")
    if runtime_minutes is None:
        runtime_minutes = DEFAULT_RUNTIME_MINUTES
    end = start + timedelta(minutes=runtime_minutes)

    event = Event()
    event.add("uid", f"tvmaze-episode-{episode_id}@tvcal")
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
    return html.unescape(_HTML_TAG_RE.sub("", summary)).strip()
