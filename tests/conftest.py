import re

import pytest
from azure.core.exceptions import ResourceExistsError, ResourceNotFoundError

import auth
import storage
import tvmaze_client


@pytest.fixture(autouse=True)
def bypass_auth(monkeypatch):
    """Most tests exercise business logic, not the auth gate itself -- treat
    every request as an authenticated "testuser" by default. Auth-focused
    tests override this within their own body via the same (function-scoped)
    monkeypatch.
    """
    monkeypatch.setattr(auth, "is_authenticated", lambda req: True)
    monkeypatch.setattr(auth, "get_username", lambda req: "testuser")


@pytest.fixture(autouse=True)
def fresh_tvmaze_state(monkeypatch):
    """The TVMaze client's cache and rate limiter are process-wide, so give
    each test its own -- otherwise one test's cached show (or spent request
    slots) leaks into the next."""
    monkeypatch.setattr(
        tvmaze_client,
        "_cache",
        tvmaze_client._ShowCache(tvmaze_client._CACHE_TTL_SECONDS, tvmaze_client._CACHE_MAX_ENTRIES),
    )
    monkeypatch.setattr(
        tvmaze_client,
        "_limiter",
        tvmaze_client._RateLimiter(
            tvmaze_client._RATE_LIMIT_CALLS, tvmaze_client._RATE_LIMIT_WINDOW_SECONDS
        ),
    )


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
