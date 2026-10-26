import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
import requests

import tvmaze_client
from tvmaze_client import (
    EPISODE_HISTORY_DAYS,
    TVMazeBusy,
    TVMazeError,
    get_show_with_episodes,
    search_shows,
    upcoming_episodes,
)

BASE_URL = "https://api.tvmaze.com"


@pytest.fixture(autouse=True)
def no_retry_sleep(monkeypatch):
    """Keep the 429 retry path from actually sleeping during tests. Swaps
    the module's `time` reference rather than patching the global time
    module, which every other thread in the process shares."""
    monkeypatch.setattr(
        tvmaze_client, "time", SimpleNamespace(monotonic=time.monotonic, sleep=lambda seconds: None)
    )


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
            "_embedded": {"episodes": [{"id": 1, "name": "Pilot", "airstamp": "2099-01-01T01:00:00+00:00"}]},
        },
    )

    show, episodes = get_show_with_episodes(82)

    assert show == {"id": 82, "name": "Fringe"}
    assert episodes == [{"id": 1, "name": "Pilot", "airstamp": "2099-01-01T01:00:00+00:00"}]
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

    with pytest.raises(requests.HTTPError):
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
        {"airstamp": "2026-10-27T01:00:00+00:00", "airdate": None, "season": 2, "number": 1},
        {"airstamp": "2026-11-02T01:00:00+00:00", "airdate": None, "season": 2, "number": 2},
    ]


def test_upcoming_episodes_is_empty_when_everything_has_aired():
    assert upcoming_episodes([_ep("2020-01-01T00:00:00+00:00")], NOW) == []


def test_upcoming_episodes_treats_a_naive_airstamp_as_utc():
    assert upcoming_episodes([_ep("2026-10-27T01:00:00")], NOW) == [
        {"airstamp": "2026-10-27T01:00:00+00:00", "airdate": None, "season": 1, "number": 1}
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
        {"airstamp": "2026-10-27T01:00:00+00:00", "airdate": None, "season": None, "number": None}
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
    monkeypatch.setattr(tvmaze_client, "time", SimpleNamespace(monotonic=fake.monotonic, sleep=fake.sleep))
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

    with pytest.raises(requests.HTTPError):
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


def test_rate_limiter_paces_concurrent_callers(monkeypatch):
    # Production calls come from a thread pool, so check the real thing:
    # many threads, real clock, and no window ever over the limit.
    monkeypatch.setattr(tvmaze_client, "time", time)
    limiter = tvmaze_client._RateLimiter(calls=3, window=0.3)
    stamps, lock = [], threading.Lock()

    def take(_):
        limiter.acquire(deadline=time.monotonic() + 5)
        with lock:
            stamps.append(time.monotonic())

    with ThreadPoolExecutor(max_workers=9) as pool:
        list(pool.map(take, range(9)))

    stamps.sort()
    assert stamps[-1] - stamps[0] >= 0.55  # 9 calls at 3 per 0.3s: two full waits
    assert all(stamps[i + 3] - stamps[i] >= 0.25 for i in range(len(stamps) - 3))


def test_concurrent_lookups_of_one_show_share_a_single_request(requests_mock):
    def slow_response(request, context):
        time.sleep(0.2)
        return {"id": 82, "name": "Fringe"}

    requests_mock.get(f"{BASE_URL}/shows/82", json=slow_response)

    with ThreadPoolExecutor(max_workers=5) as pool:
        results = list(pool.map(lambda _: get_show_with_episodes(82), range(5)))

    assert len(requests_mock.request_history) == 1
    assert all(result == results[0] for result in results)


def test_a_shared_lookup_that_ran_out_of_time_is_retried_within_the_waiters_budget(monkeypatch):
    # A search (short budget) and the calendar (long budget) can want the
    # same show at once. The search giving up mustn't fail the calendar.
    calls, release = [], threading.Event()

    def fake_fetch(show_id, deadline):
        calls.append(show_id)
        if len(calls) == 1:
            release.wait(2)
            raise TVMazeBusy("the first caller's budget ran out")
        return {"id": show_id}, []

    monkeypatch.setattr(tvmaze_client, "_fetch_show_with_episodes", fake_fetch)

    with ThreadPoolExecutor(max_workers=2) as pool:
        impatient = pool.submit(get_show_with_episodes, 82, deadline=time.monotonic() + 0.1)
        while 82 not in tvmaze_client._in_flight:
            time.sleep(0.01)
        patient = pool.submit(get_show_with_episodes, 82, deadline=time.monotonic() + 30)
        time.sleep(0.05)
        release.set()

        with pytest.raises(TVMazeBusy):
            impatient.result()
        assert patient.result() == ({"id": 82}, [])
    assert len(calls) == 2


def test_lookups_keep_only_recent_and_upcoming_episodes(requests_mock):
    now = datetime.now(timezone.utc)

    def ep(ep_id, days):
        return {"id": ep_id, "airstamp": (now + timedelta(days=days)).isoformat()}

    requests_mock.get(
        f"{BASE_URL}/shows/82",
        json={
            "id": 82,
            "_embedded": {
                "episodes": [
                    ep(1, -(EPISODE_HISTORY_DAYS + 1)),  # too old for the calendar
                    ep(2, -1),
                    ep(3, 7),
                    {"id": 4, "airstamp": None},  # unscheduled
                ]
            },
        },
    )

    _, episodes = get_show_with_episodes(82)

    assert [episode["id"] for episode in episodes] == [2, 3]


def test_upcoming_episodes_gives_the_release_date_when_there_is_no_air_time():
    # The UI shows this date as-is, like the calendar's all-day event, instead
    # of converting a placeholder airstamp to local time (maybe the wrong day).
    streaming = {"airstamp": "2026-10-28T12:00:00+00:00", "airdate": "2026-10-28", "airtime": ""}
    broadcast = {**streaming, "airtime": "21:00"}

    assert upcoming_episodes([streaming], NOW)[0]["airdate"] == "2026-10-28"
    assert upcoming_episodes([broadcast], NOW)[0]["airdate"] is None
