import re

import pytest
import itertools

from azure.core import MatchConditions
from azure.core.exceptions import ResourceExistsError, ResourceModifiedError, ResourceNotFoundError
from azure.data.tables import UpdateMode

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
    monkeypatch.setattr(tvmaze_client, "_in_flight", {})
    monkeypatch.setattr(
        tvmaze_client,
        "_limiter",
        tvmaze_client._RateLimiter(
            tvmaze_client._RATE_LIMIT_CALLS, tvmaze_client._RATE_LIMIT_WINDOW_SECONDS
        ),
    )


class FakeEntity(dict):
    """A dict with TableEntity's .metadata (just the etag)."""

    def __init__(self, data, etag):
        super().__init__(data)
        self.metadata = {"etag": etag}


class FakeTableClient:
    """In-memory stand-in for azure.data.tables.TableClient, matching the
    real one where the code relies on it: etags and conditional updates,
    MERGE (the default) vs REPLACE, entities returned as copies, and
    deleting a missing row being a no-op."""

    def __init__(self):
        self.rows: dict[tuple[str, str], dict] = {}
        self._etags = itertools.count(1)

    def _store(self, entity, mode):
        key = (entity["PartitionKey"], entity["RowKey"])
        existing = self.rows.get(key) if mode == UpdateMode.MERGE else None
        merged = {**(existing or {}), **entity}
        self.rows[key] = FakeEntity(merged, f"etag-{next(self._etags)}")

    def upsert_entity(self, entity, mode=UpdateMode.MERGE):
        self._store(entity, mode)

    def create_entity(self, entity):
        key = (entity["PartitionKey"], entity["RowKey"])
        if key in self.rows:
            raise ResourceExistsError("already exists")
        self._store(entity, UpdateMode.REPLACE)

    def update_entity(self, entity, mode=UpdateMode.MERGE, *, etag=None, match_condition=None):
        key = (entity["PartitionKey"], entity["RowKey"])
        if key not in self.rows:
            raise ResourceNotFoundError("not found")
        if match_condition == MatchConditions.IfNotModified and etag != self.rows[key].metadata["etag"]:
            raise ResourceModifiedError("etag mismatch")
        self._store(entity, mode)

    def get_entity(self, partition_key, row_key):
        key = (partition_key, row_key)
        if key not in self.rows:
            raise ResourceNotFoundError("not found")
        stored = self.rows[key]
        return FakeEntity(stored, stored.metadata["etag"])

    def delete_entity(self, partition_key, row_key):
        self.rows.pop((partition_key, row_key), None)

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
