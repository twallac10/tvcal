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


# -- rate limiting and caching ---------------------------------------------


class FakeClock:
    """Stands in for time.monotonic/time.sleep: sleeping advances the clock."""

    def __init__(self):
        self.now = 1000.0
        self.slept = []

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.slept.append(seconds)
        self.now += seconds


@pytest.fixture
def clock(monkeypatch):
    fake = FakeClock()
    monkeypatch.setattr(tvmaze_client.time, "monotonic", fake.monotonic)
    monkeypatch.setattr(tvmaze_client.time, "sleep", fake.sleep)
    return fake


def test_rate_limiter_paces_requests_instead_of_bursting(clock):
    limiter = tvmaze_client._RateLimiter(calls=3, window=10.0)

    for _ in range(3):
        limiter.acquire(deadline=clock.now + 30)
    assert clock.slept == []

    limiter.acquire(deadline=clock.now + 30)  # 4th must wait for the window
    assert clock.slept == [10.0]


def test_rate_limiter_gives_up_at_the_deadline(clock):
    limiter = tvmaze_client._RateLimiter(calls=1, window=10.0)
    limiter.acquire(deadline=clock.now + 30)

    with pytest.raises(tvmaze_client.TVMazeBusy):
        limiter.acquire(deadline=clock.now + 3)
    assert clock.slept == []


def test_a_watchlist_bigger_than_the_rate_limit_still_fetches_every_show(clock, requests_mock):
    # 40 shows is more than TVMaze allows in one 10s window -- unpaced,
    # this 429'd. Paced, it takes longer but every lookup succeeds.
    for show_id in range(1, 41):
        requests_mock.get(f"{BASE_URL}/shows/{show_id}", json={"id": show_id})

    for show_id in range(1, 41):
        get_show_with_episodes(show_id)

    assert len(requests_mock.request_history) == 40
    assert sum(clock.slept) >= 20  # 40 requests at 18 per 10s


def test_rate_limited_request_is_retried_several_times(clock, requests_mock):
    requests_mock.get(
        f"{BASE_URL}/shows/82",
        [{"status_code": 429}] * 3 + [{"json": {"id": 82, "name": "Fringe"}}],
    )

    show, _ = get_show_with_episodes(82)

    assert show["name"] == "Fringe"
    assert len(requests_mock.request_history) == 4


def test_rate_limit_retries_stop_at_the_deadline(clock, requests_mock):
    requests_mock.get(f"{BASE_URL}/shows/82", status_code=429)

    with pytest.raises(Exception):
        get_show_with_episodes(82, deadline=clock.now + 1)
    assert len(requests_mock.request_history) == 1  # a 2s backoff wouldn't fit


def test_show_lookups_are_cached(clock, requests_mock):
    requests_mock.get(f"{BASE_URL}/shows/82", json={"id": 82, "name": "Fringe"})

    first = get_show_with_episodes(82)
    second = get_show_with_episodes(82)

    assert first == second
    assert len(requests_mock.request_history) == 1


def test_cached_show_lookups_expire(clock, requests_mock):
    requests_mock.get(f"{BASE_URL}/shows/82", json={"id": 82, "name": "Fringe"})

    get_show_with_episodes(82)
    clock.now += tvmaze_client._CACHE_TTL_SECONDS + 1
    get_show_with_episodes(82)

    assert len(requests_mock.request_history) == 2


def test_missing_shows_are_not_cached(requests_mock):
    requests_mock.get(f"{BASE_URL}/shows/82", status_code=404)

    for _ in range(2):
        with pytest.raises(TVMazeError):
            get_show_with_episodes(82)
    assert len(requests_mock.request_history) == 2


def test_show_cache_evicts_oldest_entries_when_full(clock):
    cache = tvmaze_client._ShowCache(ttl=3600, max_entries=2)
    cache.put(1, "one")
    cache.put(2, "two")
    cache.put(3, "three")

    assert cache.get(1) is None
    assert cache.get(2) == "two"
    assert cache.get(3) == "three"


@pytest.mark.parametrize(
    "show, expected",
    [
        ({"network": {"name": "FOX"}, "webChannel": None}, "FOX"),
        ({"network": None, "webChannel": {"name": "Netflix"}}, "Netflix"),
        ({"network": None, "webChannel": None}, None),
        ({"network": "not a dict"}, None),
        ({}, None),
    ],
)
def test_network_name(show, expected):
    assert tvmaze_client.network_name(show) == expected
