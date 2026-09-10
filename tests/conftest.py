import pytest

import auth


@pytest.fixture(autouse=True)
def bypass_auth(monkeypatch):
    """Most tests exercise business logic, not the auth gate itself -- treat
    every request as authenticated by default. Auth-focused tests override
    this within their own body via the same (function-scoped) monkeypatch.
    """
    monkeypatch.setattr(auth, "is_authenticated", lambda req: True)
