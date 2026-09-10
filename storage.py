"""Watchlist persistence backed by Azure Table Storage.

A "watchlist" has no identity of its own beyond its token: it is simply the
set of rows sharing a PartitionKey. Tokens are opaque, random, client
generated strings -- anyone holding a token can read/modify that list, the
same trust model as an unguessable calendar subscription URL.
"""
from __future__ import annotations

import os
import re
import threading

from azure.core.exceptions import ResourceNotFoundError
from azure.data.tables import TableServiceClient

TABLE_NAME = "watchlistshows"
MAX_WATCHLIST_SIZE = 100
_TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]{1,100}$")

_table_client_singleton = None
_table_client_lock = threading.Lock()


class InvalidListToken(Exception):
    """Raised when a list token fails basic format validation."""


class WatchlistFull(Exception):
    """Raised when a watchlist already has MAX_WATCHLIST_SIZE shows."""


def validate_token(list_token: str) -> None:
    if not _TOKEN_RE.match(list_token or ""):
        raise InvalidListToken(
            "list token must be 1-100 characters of letters, digits, '-' or '_'."
        )


def _table_client():
    global _table_client_singleton
    if _table_client_singleton is None:
        with _table_client_lock:
            if _table_client_singleton is None:
                connection_string = os.environ["AzureWebJobsStorage"]
                service = TableServiceClient.from_connection_string(connection_string)
                service.create_table_if_not_exists(TABLE_NAME)
                _table_client_singleton = service.get_table_client(TABLE_NAME)
    return _table_client_singleton


def add_show(list_token: str, show_id: int, show_name: str) -> None:
    validate_token(list_token)
    client = _table_client()
    row_key = str(show_id)

    existing_keys = {
        entity["RowKey"]
        for entity in client.query_entities(f"PartitionKey eq '{list_token}'", select=["RowKey"])
    }
    if row_key not in existing_keys and len(existing_keys) >= MAX_WATCHLIST_SIZE:
        raise WatchlistFull(f"A watchlist can have at most {MAX_WATCHLIST_SIZE} shows.")

    client.upsert_entity(
        {"PartitionKey": list_token, "RowKey": row_key, "ShowName": show_name}
    )


def remove_show(list_token: str, show_id: int) -> None:
    validate_token(list_token)
    client = _table_client()
    try:
        client.delete_entity(partition_key=list_token, row_key=str(show_id))
    except ResourceNotFoundError:
        pass


def list_shows(list_token: str) -> list[dict]:
    validate_token(list_token)
    client = _table_client()
    entities = client.query_entities(f"PartitionKey eq '{list_token}'")
    return sorted(
        (
            {"id": int(entity["RowKey"]), "name": entity.get("ShowName", "")}
            for entity in entities
        ),
        key=lambda show: show["name"].lower(),
    )
