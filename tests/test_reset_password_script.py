import importlib.util
import pathlib

import pytest

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


def test_resets_the_password_and_signs_the_account_out(script, fake_client, monkeypatch):
    auth.create_account("partner", "forgotten-password")
    _type_passwords(monkeypatch, script, "temporary-password", "temporary-password")

    assert script.main(["reset_password.py", "Partner"]) == 0

    assert auth.verify_login("partner", "temporary-password") is True
    assert auth.verify_login("partner", "forgotten-password") is False
    assert storage.get_account("partner")["session_version"] == 1


def test_mismatched_passwords_change_nothing(script, fake_client, monkeypatch):
    auth.create_account("partner", "forgotten-password")
    _type_passwords(monkeypatch, script, "temporary-password", "temporary-passwrod")

    assert script.main(["reset_password.py", "partner"]) == 1
    assert auth.verify_login("partner", "forgotten-password") is True


def test_a_too_short_password_changes_nothing(script, fake_client, monkeypatch):
    auth.create_account("partner", "forgotten-password")
    _type_passwords(monkeypatch, script, "short", "short")

    assert script.main(["reset_password.py", "partner"]) == 1
    assert auth.verify_login("partner", "forgotten-password") is True


def test_an_unknown_account_is_reported_before_asking_for_a_password(script, fake_client, monkeypatch):
    monkeypatch.setattr(script.getpass, "getpass", lambda prompt="": pytest.fail("prompted"))
    assert script.main(["reset_password.py", "nobody"]) == 1


def test_it_explains_a_missing_connection_string(script, monkeypatch, capsys):
    monkeypatch.delenv("AzureWebJobsStorage")
    assert script.main(["reset_password.py", "partner"]) == 2
    assert "AzureWebJobsStorage" in capsys.readouterr().err


def test_it_needs_exactly_one_username(script):
    assert script.main(["reset_password.py"]) == 2
