from datetime import date, datetime, timezone

from icalendar import Calendar

from ical_builder import build_calendar

SHOW = {"id": 82, "name": "Fringe", "runtime": 60}

AIRED_EPISODE = {
    "id": 1001,
    "name": "Pilot",
    "season": 1,
    "number": 1,
    "airstamp": "2008-09-09T20:00:00+00:00",
    "summary": "<p>A plane lands with everyone on board dead.</p>",
    "url": "https://www.tvmaze.com/episodes/1001/fringe-1x01-pilot",
}

UNSCHEDULED_EPISODE = {
    "id": 1002,
    "name": "TBA",
    "season": 6,
    "number": 1,
    "airstamp": None,
    "summary": None,
}


def test_build_calendar_includes_one_event_per_aired_episode():
    calendar = build_calendar([(SHOW, [AIRED_EPISODE, UNSCHEDULED_EPISODE])])

    events = calendar.walk("VEVENT")
    assert len(events) == 1

    event = events[0]
    assert event["uid"] == "tvmaze-episode-1001@tvcal"
    assert str(event["summary"]) == "Fringe - S01E01 - Pilot"
    assert "plane lands" in str(event["description"])
    assert "<p>" not in str(event["description"])


def test_build_calendar_uses_show_runtime_for_event_duration():
    calendar = build_calendar([(SHOW, [AIRED_EPISODE])])
    event = calendar.walk("VEVENT")[0]

    start = event["dtstart"].dt
    end = event["dtend"].dt
    assert (end - start).total_seconds() == 60 * 60


def test_build_calendar_serializes_to_valid_ical_bytes():
    calendar = build_calendar([(SHOW, [AIRED_EPISODE])])
    raw = calendar.to_ical()

    assert b"BEGIN:VCALENDAR" in raw
    assert Calendar.from_ical(raw) is not None


def test_build_calendar_respects_a_real_zero_minute_episode_runtime():
    episode = {**AIRED_EPISODE, "runtime": 0}
    calendar = build_calendar([(SHOW, [episode])])
    event = calendar.walk("VEVENT")[0]

    start = event["dtstart"].dt
    end = event["dtend"].dt
    assert (end - start).total_seconds() == 0


def test_build_calendar_skips_episode_with_unparseable_airstamp():
    episode = {**AIRED_EPISODE, "airstamp": "not-a-timestamp"}
    calendar = build_calendar([(SHOW, [episode])])

    assert calendar.walk("VEVENT") == []


def test_build_calendar_skips_episode_missing_id():
    episode = {k: v for k, v in AIRED_EPISODE.items() if k != "id"}
    calendar = build_calendar([(SHOW, [episode])])

    assert calendar.walk("VEVENT") == []


def test_build_calendar_skips_episode_with_non_numeric_runtime_but_keeps_others():
    bad_episode = {**AIRED_EPISODE, "id": 2001, "runtime": "60"}
    calendar = build_calendar([(SHOW, [bad_episode, AIRED_EPISODE])])

    events = calendar.walk("VEVENT")
    assert len(events) == 1
    assert events[0]["uid"] == "tvmaze-episode-1001@tvcal"


def test_build_calendar_skips_episode_with_non_numeric_season_but_keeps_others():
    bad_episode = {**AIRED_EPISODE, "id": 2002, "season": "1"}
    calendar = build_calendar([(SHOW, [bad_episode, AIRED_EPISODE])])

    events = calendar.walk("VEVENT")
    assert len(events) == 1
    assert events[0]["uid"] == "tvmaze-episode-1001@tvcal"


def test_build_calendar_unescapes_html_entities_in_description():
    episode = {**AIRED_EPISODE, "summary": "<p>Rock &amp; Roll</p>"}
    calendar = build_calendar([(SHOW, [episode])])
    event = calendar.walk("VEVENT")[0]

    assert str(event["description"]) == "Rock & Roll"


def test_build_calendar_makes_an_all_day_event_when_tvmaze_has_no_air_time():
    # Streaming drops usually have no announced time; their airstamp's hour
    # is a placeholder, so pinning the event to it would be misleading.
    streaming_episode = {**AIRED_EPISODE, "airdate": "2008-09-09", "airtime": ""}

    event = build_calendar([(SHOW, [streaming_episode])]).walk("VEVENT")[0]

    assert event.decoded("dtstart") == date(2008, 9, 9)
    assert event.decoded("dtend") == date(2008, 9, 10)


def test_build_calendar_keeps_a_timed_event_when_tvmaze_has_an_air_time():
    broadcast_episode = {**AIRED_EPISODE, "airdate": "2008-09-09", "airtime": "21:00"}

    event = build_calendar([(SHOW, [broadcast_episode])]).walk("VEVENT")[0]

    assert event.decoded("dtstart") == datetime(2008, 9, 9, 20, 0, tzinfo=timezone.utc)


def test_build_calendar_drops_episodes_that_aired_before_since():
    old = {**AIRED_EPISODE, "id": 1, "airstamp": "2008-09-09T20:00:00+00:00"}
    recent = {**AIRED_EPISODE, "id": 2, "airstamp": "2026-10-20T20:00:00+00:00"}

    calendar = build_calendar(
        [(SHOW, [old, recent])], since=datetime(2026, 9, 26, tzinfo=timezone.utc)
    )

    assert [str(e["uid"]) for e in calendar.walk("VEVENT")] == ["tvmaze-episode-2@tvcal"]


def test_build_calendar_sets_the_network_or_streaming_service_as_location():
    on_tv = {**SHOW, "network": {"name": "FOX"}}
    streaming = {**SHOW, "id": 83, "network": None, "webChannel": {"name": "Netflix"}}

    events = build_calendar(
        [(on_tv, [AIRED_EPISODE]), (streaming, [{**AIRED_EPISODE, "id": 2}])]
    ).walk("VEVENT")

    assert sorted(str(e["location"]) for e in events) == ["FOX", "Netflix"]


def test_build_calendar_uses_the_given_name():
    calendar = build_calendar([], name="TV Cal (alice)")
    assert str(calendar["x-wr-calname"]) == "TV Cal (alice)"
