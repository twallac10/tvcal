import re

import pytest
from azure.core.exceptions import ResourceExistsError, ResourceNotFoundError

import auth
import storage


@pytest.fixture(autouse=True)
def bypass_auth(monkeypatch):
    """Most tests exercise business logic, not the auth gate itself -- treat
    every request as an authenticated "testuser" by default. Auth-focused
    tests override this within their own body via the same (function-scoped)
    monkeypatch.
    """
    monkeypatch.setattr(auth, "is_authenticated", lambda req: True)
    monkeypatch.setattr(auth, "get_username", lambda req: "testuser")


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
