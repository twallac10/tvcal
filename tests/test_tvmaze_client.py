import pytest

from tvmaze_client import (
    TVMazeError,
    get_show,
    get_show_episodes,
    get_show_with_episodes,
    search_shows,
)

BASE_URL = "https://api.tvmaze.com"


def test_search_shows_returns_show_objects(requests_mock):
    requests_mock.get(
        f"{BASE_URL}/search/shows",
        json=[{"score": 1.0, "show": {"id": 82, "name": "Fringe"}}],
    )

    results = search_shows("fringe")

    assert results == [{"id": 82, "name": "Fringe"}]
    assert requests_mock.last_request.qs["q"] == ["fringe"]


def test_get_show_returns_json_body(requests_mock):
    requests_mock.get(f"{BASE_URL}/shows/82", json={"id": 82, "name": "Fringe"})

    assert get_show(82) == {"id": 82, "name": "Fringe"}


def test_get_show_raises_tvmaze_error_on_404(requests_mock):
    requests_mock.get(f"{BASE_URL}/shows/999999", status_code=404)

    with pytest.raises(TVMazeError):
        get_show(999999)


def test_get_show_episodes_raises_tvmaze_error_on_404(requests_mock):
    requests_mock.get(f"{BASE_URL}/shows/999999/episodes", status_code=404)

    with pytest.raises(TVMazeError):
        get_show_episodes(999999)


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
