from datetime import datetime, timezone

import pytest

import tvmaze_client
from tvmaze_client import TVMazeError, get_show_with_episodes, search_shows, upcoming_episodes

BASE_URL = "https://api.tvmaze.com"


@pytest.fixture(autouse=True)
def no_retry_sleep(monkeypatch):
    """Keep the 429 retry path from actually sleeping during tests."""
    monkeypatch.setattr(tvmaze_client.time, "sleep", lambda seconds: None)


def test_search_shows_returns_show_objects(requests_mock):
    requests_mock.get(
        f"{BASE_URL}/search/shows",
        json=[{"score": 1.0, "show": {"id": 82, "name": "Fringe"}}],
    )

    results = search_shows("fringe")

    assert results == [{"id": 82, "name": "Fringe"}]
    assert requests_mock.last_request.qs["q"] == ["fringe"]


def test_get_show_with_episodes_splits_embedded_episodes_from_show(requests_mock):
    requests_mock.get(
        f"{BASE_URL}/shows/82",
        json={
            "id": 82,
            "name": "Fringe",
            "_embedded": {"episodes": [{"id": 1, "name": "Pilot"}]},
        },
    )

    show, episodes = get_show_with_episodes(82)

    assert show == {"id": 82, "name": "Fringe"}
    assert episodes == [{"id": 1, "name": "Pilot"}]
    assert requests_mock.last_request.qs["embed"] == ["episodes"]


def test_get_show_with_episodes_raises_tvmaze_error_on_404(requests_mock):
    requests_mock.get(f"{BASE_URL}/shows/999999", status_code=404)

    with pytest.raises(TVMazeError):
        get_show_with_episodes(999999)


def test_rate_limited_request_is_retried_and_succeeds(requests_mock):
    # A calendar feed fans out one request per show concurrently, so bursts
    # can trip TVMaze's per-IP rate limit; a 429 shouldn't drop the show.
    requests_mock.get(
        f"{BASE_URL}/shows/82",
        [
            {"status_code": 429, "headers": {"Retry-After": "1"}},
            {"json": {"id": 82, "name": "Fringe"}},
        ],
    )

    show, episodes = get_show_with_episodes(82)

    assert show == {"id": 82, "name": "Fringe"}
    assert episodes == []
    assert len(requests_mock.request_history) == 2


def test_persistent_rate_limiting_eventually_raises(requests_mock):
    requests_mock.get(f"{BASE_URL}/shows/82", status_code=429)

    with pytest.raises(Exception):
        get_show_with_episodes(82)


# -- upcoming_episodes ----------------------------------------------------

NOW = datetime(2026, 10, 26, 12, 0, tzinfo=timezone.utc)


def _ep(airstamp, season=1, number=1):
    return {"airstamp": airstamp, "season": season, "number": number}


def test_upcoming_episodes_returns_the_next_two_soonest_first():
    episodes = [
        _ep("2026-11-09T01:00:00+00:00", 2, 3),
        _ep("2026-10-01T01:00:00+00:00", 1, 9),  # already aired
        _ep("2026-11-02T01:00:00+00:00", 2, 2),
        _ep("2026-10-27T01:00:00+00:00", 2, 1),
    ]

    assert upcoming_episodes(episodes, NOW) == [
        {"airstamp": "2026-10-27T01:00:00+00:00", "season": 2, "number": 1},
        {"airstamp": "2026-11-02T01:00:00+00:00", "season": 2, "number": 2},
    ]


def test_upcoming_episodes_is_empty_when_everything_has_aired():
    assert upcoming_episodes([_ep("2020-01-01T00:00:00+00:00")], NOW) == []


def test_upcoming_episodes_treats_a_naive_airstamp_as_utc():
    assert upcoming_episodes([_ep("2026-10-27T01:00:00")], NOW) == [
        {"airstamp": "2026-10-27T01:00:00+00:00", "season": 1, "number": 1}
    ]


def test_upcoming_episodes_skips_malformed_data_instead_of_raising():
    episodes = [
        "not a dict",
        {"no": "airstamp"},
        {"airstamp": None},
        {"airstamp": "not-a-date"},
        _ep("2026-10-27T01:00:00+00:00", season="1", number=True),
    ]

    # The one usable episode survives; its non-int season/number are
    # dropped rather than passed through to the UI.
    assert upcoming_episodes(episodes, NOW) == [
        {"airstamp": "2026-10-27T01:00:00+00:00", "season": None, "number": None}
    ]
    assert upcoming_episodes("not a list", NOW) == []
