import importlib.util
import pathlib

import pytest

import azure.functions as func

import auth
import storage

_SCRIPT = pathlib.Path(__file__).resolve().parent.parent / "scripts" / "reset_password.py"


@pytest.fixture
def script(monkeypatch):
    monkeypatch.setenv("AzureWebJobsStorage", "UseDevelopmentStorage=true")  # storage itself is faked
    spec = importlib.util.spec_from_file_location("reset_password", _SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _type_passwords(monkeypatch, script, *answers):
    replies = iter(answers)
    monkeypatch.setattr(script.getpass, "getpass", lambda prompt="": next(replies))


def _signed_in(cookie_header):
    req = func.HttpRequest("GET", "https://example.com/", headers={"Cookie": cookie_header}, body=b"")
    return auth._parse_session_cookie(req) is not None


def test_resets_the_password_and_signs_the_account_out(script, fake_client, monkeypatch):
    monkeypatch.setenv("SESSION_SECRET", "test-secret")
    username, password_hash = auth.create_account("partner", "forgotten-password")
    old_session = auth.create_session_cookie(username, password_hash).split(";")[0]
    assert _signed_in(old_session)
    _type_passwords(monkeypatch, script, "temporary-password", "temporary-password")

    assert script.main(["reset_password.py", "Partner"]) == 0

    assert auth.check_login("partner", "temporary-password") is not None
    assert auth.check_login("partner", "forgotten-password") is None
    assert not _signed_in(old_session)


def test_mismatched_passwords_change_nothing(script, fake_client, monkeypatch):
    auth.create_account("partner", "forgotten-password")
    _type_passwords(monkeypatch, script, "temporary-password", "temporary-passwrod")

    assert script.main(["reset_password.py", "partner"]) == 1
    assert auth.check_login("partner", "forgotten-password") is not None


def test_a_too_short_password_changes_nothing(script, fake_client, monkeypatch):
    auth.create_account("partner", "forgotten-password")
    _type_passwords(monkeypatch, script, "short", "short")

    assert script.main(["reset_password.py", "partner"]) == 1
    assert auth.check_login("partner", "forgotten-password") is not None


def test_an_unknown_account_is_reported_before_asking_for_a_password(script, fake_client, monkeypatch):
    monkeypatch.setattr(script.getpass, "getpass", lambda prompt="": pytest.fail("prompted"))
    assert script.main(["reset_password.py", "nobody"]) == 1


def test_it_explains_a_missing_connection_string(script, monkeypatch, capsys):
    monkeypatch.delenv("AzureWebJobsStorage")
    assert script.main(["reset_password.py", "partner"]) == 2
    assert "AzureWebJobsStorage" in capsys.readouterr().err


def test_it_needs_exactly_one_username(script):
    assert script.main(["reset_password.py"]) == 2


def test_a_change_made_while_the_script_waits_for_input_is_not_overwritten(script, fake_client, monkeypatch):
    auth.create_account("partner", "forgotten-password")
    answers = iter(["temporary-password", "temporary-password"])

    def type_password(prompt=""):
        answer = next(answers)
        if prompt.startswith("Same"):
            # They remember it and change it themselves while you're typing.
            account = storage.get_account("partner")
            auth.change_password("partner", "their-own-choice", account["etag"])
        return answer

    monkeypatch.setattr(script.getpass, "getpass", type_password)

    assert script.main(["reset_password.py", "partner"]) == 1
    assert auth.check_login("partner", "their-own-choice") is not None
