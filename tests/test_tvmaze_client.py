import pytest

import tvmaze_client
from tvmaze_client import TVMazeError, get_show_with_episodes, search_shows

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
