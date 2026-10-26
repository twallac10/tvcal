"""Builds an iCalendar feed of episode air dates from TVMaze data."""
from __future__ import annotations

import html
import logging
import re
from datetime import date, datetime, timedelta, timezone

from icalendar import Calendar, Event

from tvmaze_client import is_all_day, network_name

DEFAULT_RUNTIME_MINUTES = 30
_HTML_TAG_RE = re.compile(r"<[^<]+?>")


def _safe_id(value) -> object:
    """Best-effort id for a log line, for values that may not be dicts at all."""
    return value.get("id") if isinstance(value, dict) else None


def build_calendar(
    shows_with_episodes: list[tuple[dict, list[dict]]],
    name: str = "TV Cal",
    since: datetime | None = None,
) -> Calendar:
    """`since`, if given, drops episodes that aired before it -- otherwise a
    long-running show fills the calendar with years of past episodes."""
    calendar = Calendar()
    calendar.add("prodid", "-//tvcal//TV Show Episode Tracker//EN")
    calendar.add("version", "2.0")
    calendar.add("x-wr-calname", name)
    calendar.add("method", "PUBLISH")

    for show, episodes in shows_with_episodes:
        if not isinstance(episodes, list):
            logging.warning("Skipping show %r with a malformed episode list", _safe_id(show))
            continue

        for episode in episodes:
            try:
                event = build_event(show, episode, since)
            except Exception:
                # TVMaze data is untrusted input -- an episode with a
                # malformed field shouldn't take down every other show's
                # events with it. Deliberately broad: a non-numeric runtime
                # raises TypeError, an out-of-range one OverflowError, and a
                # non-dict episode AttributeError, all of which would
                # otherwise 500 the entire feed.
                logging.warning(
                    "Skipping malformed episode %r for show %r",
                    _safe_id(episode),
                    _safe_id(show),
                )
                continue
            if event is not None:
                calendar.add_component(event)

    return calendar


def build_event(show: dict, episode: dict, since: datetime | None = None) -> Event | None:
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
    if start.tzinfo is None:
        start = start.replace(tzinfo=timezone.utc)
    if since is not None and start < since:
        return None

    event = Event()
    event.add("uid", f"tvmaze-episode-{episode_id}@tvcal")
    event.add("summary", _format_episode_title(show, episode))

    if is_all_day(episode):
        # The airstamp's hour is a placeholder, so an all-day event on the
        # release date is the honest version.
        day = date.fromisoformat(episode["airdate"])
        event.add("dtstart", day)
        event.add("dtend", day + timedelta(days=1))
    else:
        runtime_minutes = episode.get("runtime")
        if runtime_minutes is None:
            runtime_minutes = show.get("runtime")
        if runtime_minutes is None:
            runtime_minutes = DEFAULT_RUNTIME_MINUTES
        event.add("dtstart", start)
        event.add("dtend", start + timedelta(minutes=runtime_minutes))

    event.add("dtstamp", datetime.now(timezone.utc))
    event.add("description", _clean_summary(episode.get("summary")))
    where = network_name(show)
    if where:
        event.add("location", where)
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
