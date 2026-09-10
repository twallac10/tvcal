import pytest
from azure.core.exceptions import ResourceNotFoundError

import storage


class FakeTableClient:
    """In-memory stand-in for azure.data.tables.TableClient."""

    def __init__(self):
        self.rows: dict[tuple[str, str], dict] = {}

    def upsert_entity(self, entity):
        self.rows[(entity["PartitionKey"], entity["RowKey"])] = entity

    def delete_entity(self, partition_key, row_key):
        key = (partition_key, row_key)
        if key not in self.rows:
            raise ResourceNotFoundError("not found")
        del self.rows[key]

    def query_entities(self, query_filter):
        token = query_filter.split("'")[1]
        return [
            entity
            for (partition_key, _row_key), entity in self.rows.items()
            if partition_key == token
        ]


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
