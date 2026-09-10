"""Watchlist persistence backed by Azure Table Storage.

A "watchlist" has no identity of its own beyond its write token: it is
simply the set of show rows sharing a PartitionKey. The write token is an
opaque, random, client-generated string -- anyone holding it can view or
edit that list, so it's kept private to the browser managing the list (URL
+ localStorage), never embedded in a calendar subscription URL.

The calendar feed instead uses a separate, server-issued *feed token*
(get_or_create_feed_token / resolve_feed_token below) that can only be used
to read episode data for the watchlist -- never to add/remove shows. This
way a feed URL leaking (shared calendars, sync logs, browser history on a
shared device) only exposes what someone is watching, not write access to
their list.
"""
from __future__ import annotations

import os
import re
import secrets
import threading

from azure.core.exceptions import ResourceExistsError, ResourceNotFoundError
from azure.data.tables import TableServiceClient

TABLE_NAME = "watchlistshows"
MAX_WATCHLIST_SIZE = 100
_TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]{1,100}$")

# Reserved RowKey (under a watchlist's own PartitionKey) holding that
# watchlist's feed token, and the PartitionKey used to index feed tokens
# back to the write token they were issued for. Neither collides with a
# show's RowKey, which is always a plain integer.
_META_ROW_KEY = "__meta__"
_FEED_INDEX_PARTITION = "__feed_index__"

_table_client_singleton = None
_table_client_lock = threading.Lock()


class InvalidListToken(Exception):
    """Raised when a list or feed token fails basic format validation."""


class WatchlistFull(Exception):
    """Raised when a watchlist already has MAX_WATCHLIST_SIZE shows."""


class UnknownFeedToken(Exception):
    """Raised when a feed token doesn't resolve to any watchlist."""


def validate_token(token: str) -> None:
    if not _TOKEN_RE.match(token or ""):
        raise InvalidListToken(
            "token must be 1-100 characters of letters, digits, '-' or '_'."
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


def _shows_filter(list_token: str) -> str:
    # Excludes the reserved feed-token metadata row, which shares the
    # watchlist's PartitionKey but isn't a show.
    return f"PartitionKey eq '{list_token}' and RowKey ne '{_META_ROW_KEY}'"


def add_show(list_token: str, show_id: int, show_name: str) -> None:
    validate_token(list_token)
    client = _table_client()
    row_key = str(show_id)

    existing_keys = {
        entity["RowKey"]
        for entity in client.query_entities(_shows_filter(list_token), select=["RowKey"])
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
    entities = client.query_entities(_shows_filter(list_token))
    return sorted(
        (
            {"id": int(entity["RowKey"]), "name": entity.get("ShowName", "")}
            for entity in entities
        ),
        key=lambda show: show["name"].lower(),
    )


def get_or_create_feed_token(list_token: str) -> str:
    """Return the read-only calendar-feed token for a watchlist, creating one if needed."""
    validate_token(list_token)
    client = _table_client()

    try:
        meta = client.get_entity(partition_key=list_token, row_key=_META_ROW_KEY)
        return meta["FeedToken"]
    except ResourceNotFoundError:
        pass

    feed_token = secrets.token_urlsafe(24)
    try:
        client.create_entity(
            {"PartitionKey": list_token, "RowKey": _META_ROW_KEY, "FeedToken": feed_token}
        )
    except ResourceExistsError:
        # Lost a race with a concurrent request creating this watchlist's
        # feed token; use whichever one actually got written first.
        meta = client.get_entity(partition_key=list_token, row_key=_META_ROW_KEY)
        return meta["FeedToken"]

    client.create_entity(
        {"PartitionKey": _FEED_INDEX_PARTITION, "RowKey": feed_token, "WriteToken": list_token}
    )
    return feed_token


def resolve_feed_token(feed_token: str) -> str:
    """Return the write token (watchlist) that a feed token was issued for."""
    validate_token(feed_token)
    client = _table_client()
    try:
        entity = client.get_entity(partition_key=_FEED_INDEX_PARTITION, row_key=feed_token)
    except ResourceNotFoundError:
        raise UnknownFeedToken("This calendar link is no longer valid.")
    return entity["WriteToken"]
