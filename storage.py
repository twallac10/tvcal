"""Watchlist and account persistence backed by Azure Table Storage.

A "watchlist" has no identity of its own beyond its owning token: it is
simply the set of show rows sharing a PartitionKey. That token is normally
an account's (lowercased) username -- accounts are the unit of identity now
(see auth.py), so signing in from any device reaches the same watchlist.
The legacy shape (a random, client-generated token with no account behind
it) still works structurally and is what migrate_watchlist() reads from
when a browser's pre-account watchlist is folded into a new account at
signup.

The calendar feed uses a separate, server-issued *feed token*
(get_or_create_feed_token / resolve_feed_token below) that can only be used
to read episode data for the watchlist -- never to add/remove shows, and
not tied to a login session at all. This way a feed URL leaking (shared
calendars, sync logs, browser history on a shared device) only exposes what
someone is watching, not write access to their list.
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
MAX_ACCOUNTS = 10
_TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]{1,100}$")

# Reserved RowKey (under a watchlist's own PartitionKey) holding that
# watchlist's feed token, and the PartitionKey used to index feed tokens
# back to the token they were issued for. Neither collides with a show's
# RowKey, which is always a plain integer. Accounts get their own entirely
# separate reserved partition.
_META_ROW_KEY = "__meta__"
_FEED_INDEX_PARTITION = "__feed_index__"
_ACCOUNTS_PARTITION = "__accounts__"

# A username IS a watchlist's PartitionKey now (see module docstring), so
# none of these reserved sentinels -- each used elsewhere as a PartitionKey
# or RowKey in this same table -- can ever be valid as a token/username.
# Letting one through would let an account named e.g. "__accounts__"
# silently read/corrupt the partition that stores every account's password
# hash via ordinary watchlist calls (add_show/list_shows).
RESERVED_TOKENS = frozenset({_META_ROW_KEY, _FEED_INDEX_PARTITION, _ACCOUNTS_PARTITION})

_table_client_singleton = None
_table_client_lock = threading.Lock()


class InvalidListToken(Exception):
    """Raised when a list or feed token fails basic format validation."""


class WatchlistFull(Exception):
    """Raised when a watchlist already has MAX_WATCHLIST_SIZE shows."""


class UnknownFeedToken(Exception):
    """Raised when a feed token doesn't resolve to any watchlist."""


class UsernameTaken(Exception):
    """Raised when an account already exists for a given username."""


class UnknownAccount(Exception):
    """Raised when no account exists for a given username."""


class TooManyAccounts(Exception):
    """Raised once MAX_ACCOUNTS accounts already exist."""


def validate_token(token: str) -> None:
    if not _TOKEN_RE.match(token or "") or token in RESERVED_TOKENS:
        raise InvalidListToken(
            "token must be 1-100 characters of letters, digits, '-' or '_', "
            "and not a reserved name."
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


def _ensure_feed_index(client, feed_token: str, list_token: str) -> None:
    """Point `feed_token` at `list_token` in the lookup index, idempotently."""
    try:
        client.create_entity(
            {"PartitionKey": _FEED_INDEX_PARTITION, "RowKey": feed_token, "WriteToken": list_token}
        )
    except ResourceExistsError:
        pass


def get_or_create_feed_token(list_token: str) -> str:
    """Return the read-only calendar-feed token for a watchlist, creating one if needed."""
    validate_token(list_token)
    client = _table_client()

    try:
        meta = client.get_entity(partition_key=list_token, row_key=_META_ROW_KEY)
    except ResourceNotFoundError:
        pass
    else:
        feed_token = meta["FeedToken"]
        # Self-heal: an older build wrote the meta row before the index row,
        # so a crash or a transient Table Storage error between the two left
        # a token that resolve_feed_token() can never resolve -- a calendar
        # URL that 404s forever, with retries returning the same dead token.
        _ensure_feed_index(client, feed_token, list_token)
        return feed_token

    feed_token = secrets.token_urlsafe(24)
    # Index row first, deliberately: an orphaned index row is harmless (its
    # token is never handed out), whereas an orphaned meta row bricks this
    # watchlist's calendar URL permanently.
    _ensure_feed_index(client, feed_token, list_token)
    try:
        client.create_entity(
            {"PartitionKey": list_token, "RowKey": _META_ROW_KEY, "FeedToken": feed_token}
        )
    except ResourceExistsError:
        # Lost a race with a concurrent request creating this watchlist's
        # feed token; use whichever one actually got written first.
        meta = client.get_entity(partition_key=list_token, row_key=_META_ROW_KEY)
        return meta["FeedToken"]

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


def create_account(username: str, password_hash: str, salt: str, iterations: int) -> None:
    """Create an account record. `username` must already be normalized
    (lowercased) and validated by the caller (see auth.validate_username)."""
    validate_token(username)
    client = _table_client()

    existing = sum(
        1 for _ in client.query_entities(f"PartitionKey eq '{_ACCOUNTS_PARTITION}'", select=["RowKey"])
    )
    if existing >= MAX_ACCOUNTS:
        raise TooManyAccounts(f"This app already has the maximum of {MAX_ACCOUNTS} accounts.")

    try:
        client.create_entity(
            {
                "PartitionKey": _ACCOUNTS_PARTITION,
                "RowKey": username,
                "PasswordHash": password_hash,
                "Salt": salt,
                "Iterations": iterations,
            }
        )
    except ResourceExistsError:
        raise UsernameTaken(f"Username '{username}' is already taken.")


def get_account(username: str) -> dict:
    validate_token(username)
    client = _table_client()
    try:
        entity = client.get_entity(partition_key=_ACCOUNTS_PARTITION, row_key=username)
    except ResourceNotFoundError:
        raise UnknownAccount(f"No account for username '{username}'.")
    return {
        "password_hash": entity["PasswordHash"],
        "salt": entity["Salt"],
        "iterations": entity["Iterations"],
    }


def migrate_watchlist(old_token: str, new_username: str) -> None:
    """Best-effort copy of shows from a pre-account watchlist token into an
    account's watchlist (used once at signup to carry a browser's existing
    list forward). Leaves the old rows in place rather than deleting them --
    harmless, and safer if something goes wrong partway through.

    `old_token` must NOT be a real account's username: since a username IS
    a watchlist's PartitionKey, a naive copy would let anyone who knows (or
    guesses) another user's username pass it as their own "previous_token"
    at signup and silently copy that user's private watchlist into their
    own account. This only ever migrates a genuinely anonymous, unclaimed
    legacy token.
    """
    validate_token(old_token)
    validate_token(new_username)
    if old_token == new_username:
        return

    try:
        get_account(old_token)
    except UnknownAccount:
        pass
    else:
        # old_token belongs to a real account -- refuse to touch it.
        return

    for show in list_shows(old_token):
        try:
            add_show(new_username, show["id"], show["name"])
        except WatchlistFull:
            break
