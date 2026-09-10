import re

import pytest
from azure.core.exceptions import ResourceExistsError, ResourceNotFoundError

import storage


class FakeTableClient:
    """In-memory stand-in for azure.data.tables.TableClient."""

    def __init__(self):
        self.rows: dict[tuple[str, str], dict] = {}

    def upsert_entity(self, entity):
        self.rows[(entity["PartitionKey"], entity["RowKey"])] = entity

    def create_entity(self, entity):
        key = (entity["PartitionKey"], entity["RowKey"])
        if key in self.rows:
            raise ResourceExistsError("already exists")
        self.rows[key] = entity

    def get_entity(self, partition_key, row_key):
        key = (partition_key, row_key)
        if key not in self.rows:
            raise ResourceNotFoundError("not found")
        return self.rows[key]

    def delete_entity(self, partition_key, row_key):
        key = (partition_key, row_key)
        if key not in self.rows:
            raise ResourceNotFoundError("not found")
        del self.rows[key]

    def query_entities(self, query_filter, select=None):
        quoted = re.findall(r"'([^']*)'", query_filter)
        partition_key = quoted[0]
        excluded_row_key = quoted[1] if "RowKey ne" in query_filter else None
        entities = [
            entity
            for (partition_key_, row_key_), entity in self.rows.items()
            if partition_key_ == partition_key and row_key_ != excluded_row_key
        ]
        if select is None:
            return entities
        return [{key: entity[key] for key in select} for entity in entities]


@pytest.fixture
def fake_client(monkeypatch):
    client = FakeTableClient()
    monkeypatch.setattr(storage, "_table_client", lambda: client)
    return client


def test_add_and_list_shows(fake_client):
    storage.add_show("abc123", 82, "Fringe")
    storage.add_show("abc123", 143, "Breaking Bad")

    assert storage.list_shows("abc123") == [
        {"id": 143, "name": "Breaking Bad"},
        {"id": 82, "name": "Fringe"},
    ]


def test_list_shows_is_scoped_to_token(fake_client):
    storage.add_show("abc123", 82, "Fringe")
    storage.add_show("other-token", 1, "Girls")

    assert storage.list_shows("abc123") == [{"id": 82, "name": "Fringe"}]


def test_add_show_is_idempotent(fake_client):
    storage.add_show("abc123", 82, "Fringe")
    storage.add_show("abc123", 82, "Fringe")

    assert storage.list_shows("abc123") == [{"id": 82, "name": "Fringe"}]


def test_remove_show(fake_client):
    storage.add_show("abc123", 82, "Fringe")
    storage.remove_show("abc123", 82)

    assert storage.list_shows("abc123") == []


def test_remove_missing_show_is_a_noop(fake_client):
    storage.remove_show("abc123", 999)


@pytest.mark.parametrize("bad_token", ["", "has spaces", "has/slash", "x" * 101])
def test_invalid_token_rejected(fake_client, bad_token):
    with pytest.raises(storage.InvalidListToken):
        storage.add_show(bad_token, 1, "Show")


def test_add_show_rejects_once_watchlist_is_full(fake_client, monkeypatch):
    monkeypatch.setattr(storage, "MAX_WATCHLIST_SIZE", 2)

    storage.add_show("abc123", 1, "Show 1")
    storage.add_show("abc123", 2, "Show 2")

    with pytest.raises(storage.WatchlistFull):
        storage.add_show("abc123", 3, "Show 3")


def test_add_show_re_adding_existing_show_does_not_count_against_cap(fake_client, monkeypatch):
    monkeypatch.setattr(storage, "MAX_WATCHLIST_SIZE", 2)

    storage.add_show("abc123", 1, "Show 1")
    storage.add_show("abc123", 2, "Show 2")

    storage.add_show("abc123", 1, "Show 1 renamed")

    assert storage.list_shows("abc123") == [
        {"id": 1, "name": "Show 1 renamed"},
        {"id": 2, "name": "Show 2"},
    ]


def test_get_or_create_feed_token_round_trips_to_the_write_token(fake_client):
    feed_token = storage.get_or_create_feed_token("abc123")

    assert storage.resolve_feed_token(feed_token) == "abc123"


def test_get_or_create_feed_token_is_idempotent(fake_client):
    first = storage.get_or_create_feed_token("abc123")
    second = storage.get_or_create_feed_token("abc123")

    assert first == second


def test_get_or_create_feed_token_differs_per_watchlist(fake_client):
    token_a = storage.get_or_create_feed_token("abc123")
    token_b = storage.get_or_create_feed_token("xyz789")

    assert token_a != token_b
    assert storage.resolve_feed_token(token_a) == "abc123"
    assert storage.resolve_feed_token(token_b) == "xyz789"


def test_resolve_unknown_feed_token_raises(fake_client):
    with pytest.raises(storage.UnknownFeedToken):
        storage.resolve_feed_token("does-not-exist")


def test_feed_token_metadata_row_is_excluded_from_watchlist_contents(fake_client):
    storage.add_show("abc123", 82, "Fringe")
    storage.get_or_create_feed_token("abc123")

    assert storage.list_shows("abc123") == [{"id": 82, "name": "Fringe"}]


def test_feed_token_metadata_row_does_not_count_against_watchlist_cap(fake_client, monkeypatch):
    monkeypatch.setattr(storage, "MAX_WATCHLIST_SIZE", 1)

    storage.get_or_create_feed_token("abc123")
    storage.add_show("abc123", 82, "Fringe")

    assert storage.list_shows("abc123") == [{"id": 82, "name": "Fringe"}]


@pytest.mark.parametrize("bad_token", ["", "has spaces", "has/slash", "x" * 101])
def test_get_or_create_feed_token_rejects_invalid_write_token(fake_client, bad_token):
    with pytest.raises(storage.InvalidListToken):
        storage.get_or_create_feed_token(bad_token)


@pytest.mark.parametrize("bad_token", ["", "has spaces", "has/slash", "x" * 101])
def test_resolve_feed_token_rejects_invalid_format(fake_client, bad_token):
    with pytest.raises(storage.InvalidListToken):
        storage.resolve_feed_token(bad_token)
