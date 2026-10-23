"""Exercise the real source -> DB importer -> typed reader, not seeded fixtures."""
from contextlib import nullcontext

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.core import internal_defaults, legacy_settings_import as legacy, owner_settings
from app.core.config import Settings
from app.db import database


@pytest.fixture(autouse=True)
def isolate_legacy_sources(monkeypatch):
    for aliases in legacy.LEGACY_NAMES.values():
        for name in aliases:
            monkeypatch.delenv(name, raising=False)
            monkeypatch.delenv(name.lower(), raising=False)
    # PostgreSQL's boot advisory lock itself has separate real-DB coverage.
    monkeypatch.setattr(database, "boot_init_lock", nullcontext)


@pytest.mark.parametrize("raw,expected", [("true", True), ("1", True), ("false", False), ("0", False)])
def test_explicit_boolean_import(raw, expected, monkeypatch, tmp_path):
    monkeypatch.setenv("NETWORK_REQUIRE_TOKEN_BINDING", raw)
    legacy.import_legacy_settings(env_file=str(tmp_path / "missing"))
    assert owner_settings.require_token_binding() is expected
    assert database.get_system_setting(owner_settings.PREFIX + "network_require_token_binding") == str(expected).lower()
    assert database.get_system_setting(legacy.MIGRATION_KEY) == str(legacy.VERSION)


@pytest.mark.parametrize("value", [None, "", "  "])
def test_absent_and_blank_do_not_create_setting(value, monkeypatch, tmp_path):
    if value is not None:
        monkeypatch.setenv("SESSION_EXPIRE_DAYS", value)
    legacy.import_legacy_settings(env_file=str(tmp_path / "missing"))
    assert database.get_system_setting(owner_settings.PREFIX + "session_expire_days") is None
    assert owner_settings.session_expire_days() == 30


def test_dotenv_false_zero_and_environment_precedence(tmp_path, monkeypatch):
    dotenv = tmp_path / "legacy.env"
    dotenv.write_text("NETWORK_PEER_TERMINALS=false\nSESSION_EXPIRE_DAYS=0\nNETWORK_REQUIRE_TOKEN_BINDING=true\n")
    monkeypatch.setenv("NETWORK_REQUIRE_TOKEN_BINDING", "false")
    legacy.import_legacy_settings(env_file=str(dotenv))
    assert owner_settings.read() == owner_settings.OwnerPolicy(
        network_require_token_binding=False, network_peer_terminals=False, session_expire_days=0)
    assert database.get_system_setting(owner_settings.PREFIX + "network_peer_terminals") == "false"
    assert database.get_system_setting(owner_settings.PREFIX + "session_expire_days") == "0"


def test_selected_canonical_env_file_is_used_once(tmp_path, monkeypatch):
    dotenv = tmp_path / "canonical.env"
    dotenv.write_text("session_expire_days=7\n")
    monkeypatch.setenv("AI_SHUFFLE_ENV_FILE", str(dotenv))
    legacy.import_legacy_settings()
    assert owner_settings.session_expire_days() == 7
    # Completed versions must not consult either source again.
    def unexpected_read(*args, **kwargs):
        raise AssertionError("completed migration reread legacy environment")
    monkeypatch.setattr(legacy, "_explicit_values", unexpected_read)
    legacy.import_legacy_settings()


def test_explicit_blank_environment_does_not_fall_through_to_dotenv(tmp_path, monkeypatch):
    dotenv = tmp_path / "legacy.env"
    dotenv.write_text("SESSION_EXPIRE_DAYS=7\n")
    monkeypatch.setenv("SESSION_EXPIRE_DAYS", "")
    legacy.import_legacy_settings(env_file=str(dotenv))
    assert database.get_system_setting(owner_settings.PREFIX + "session_expire_days") is None


@pytest.mark.parametrize("stored", ["false", "true", ""])
def test_existing_persisted_key_wins_even_blank(stored, tmp_path, monkeypatch):
    key = owner_settings.PREFIX + "network_require_token_binding"
    database.set_system_setting(key, stored)
    monkeypatch.setenv("NETWORK_REQUIRE_TOKEN_BINDING", "true")
    legacy.import_legacy_settings(env_file=str(tmp_path / "missing"))
    assert database.get_system_setting(key) == stored


def test_run_twice_and_later_ui_choice_wins(monkeypatch, tmp_path):
    monkeypatch.setenv("SESSION_EXPIRE_DAYS", "7")
    legacy.import_legacy_settings(env_file=str(tmp_path / "missing"))
    assert owner_settings.session_expire_days() == 7
    owner_settings.write({"session_expire_days": 14})
    monkeypatch.setenv("SESSION_EXPIRE_DAYS", "90")
    legacy.import_legacy_settings(env_file=str(tmp_path / "missing"))
    assert Settings(_env_file=None).session_expire_days == 14


def test_absent_on_first_run_cannot_import_on_second_run(monkeypatch, tmp_path):
    legacy.import_legacy_settings(env_file=str(tmp_path / "missing"))
    monkeypatch.setenv("SESSION_EXPIRE_DAYS", "7")
    legacy.import_legacy_settings(env_file=str(tmp_path / "missing"))
    assert owner_settings.session_expire_days() == 30


def test_invalid_values_and_credentials_are_never_logged_or_ingested(monkeypatch, tmp_path, caplog):
    dotenv = tmp_path / "legacy.env"
    dotenv.write_text("SESSION_EXPIRE_DAYS=private-invalid-value\nANTHROPIC_API_KEY=never-ingest-this\n")
    legacy.import_legacy_settings(env_file=str(dotenv))
    assert "SESSION_EXPIRE_DAYS" in caplog.text
    assert "private-invalid-value" not in caplog.text
    assert "never-ingest-this" not in caplog.text
    assert database.get_all_system_settings().keys() == {legacy.MIGRATION_KEY}


def test_atomic_seed_does_not_overwrite_concurrent_ui_write(monkeypatch, tmp_path):
    monkeypatch.setenv("SESSION_EXPIRE_DAYS", "7")
    seed = database.get_or_create_system_setting

    def raced(key, factory):
        database.set_system_setting(key, "14")
        return seed(key, factory)

    monkeypatch.setattr(database, "get_or_create_system_setting", raced)
    legacy.import_legacy_settings(env_file=str(tmp_path / "missing"))
    assert owner_settings.session_expire_days() == 14


def test_migrated_fields_are_not_env_addressable(monkeypatch, tmp_path):
    names = set(legacy.LEGACY_NAMES) | {n.lower() for n in vars(internal_defaults) if n.isupper()}
    dotenv = tmp_path / "legacy.env"
    dotenv.write_text("\n".join(name.upper() + "=invalid-override" for name in names))
    for name in names:
        monkeypatch.setenv(name.upper(), "invalid-override")
    config = Settings(_env_file=dotenv)
    assert not names.intersection(Settings.model_fields)
    assert config.session_expire_days == 30
    assert config.network_require_token_binding is False
    assert config.network_peer_terminals is False
    assert config.network_token_binding_enabled is True
    assert config.file_logging_enabled is True
    assert config.error_log_max_warning_rows == 1000
    assert config.single_install_sweep_interval_seconds == 60


def test_later_version_runs_without_reopening_completed_batch(monkeypatch, tmp_path):
    monkeypatch.setattr(legacy, "MIGRATIONS", {1: {"network_peer_terminals": ("NETWORK_PEER_TERMINALS",)}})
    legacy.import_legacy_settings(env_file=str(tmp_path / "missing"))
    monkeypatch.setenv("NETWORK_PEER_TERMINALS", "true")
    monkeypatch.setenv("SESSION_EXPIRE_DAYS", "7")
    monkeypatch.setitem(legacy.MIGRATIONS, 2, {"session_expire_days": ("SESSION_EXPIRE_DAYS",)})
    legacy.import_legacy_settings(env_file=str(tmp_path / "missing"))
    assert owner_settings.peer_terminals_enabled() is False
    assert owner_settings.session_expire_days() == 7
    assert database.get_system_setting("migration:legacy_owner_policy:2") == "2"


def test_owner_policy_api_uses_existing_auth_and_storage():
    from app.api import settings as api
    from app.api.auth import require_auth, require_admin

    app = FastAPI()
    app.include_router(api.router)
    app.dependency_overrides[require_auth] = lambda: "owner"
    app.dependency_overrides[require_admin] = lambda: "owner"
    client = TestClient(app)
    assert client.get("/api/v1/settings/owner-policy").json() == owner_settings.OwnerPolicy().model_dump()
    response = client.put("/api/v1/settings/owner-policy", json={"session_expire_days": 7})
    assert response.status_code == 200
    assert response.json()["session_expire_days"] == 7
    assert owner_settings.session_expire_days() == 7
    assert client.put("/api/v1/settings/owner-policy", json={"session_expire_days": -1}).status_code == 400
    assert client.put("/api/v1/settings/owner-policy", json={"network_peer_terminals": None}).status_code == 400
    assert owner_settings.session_expire_days() == 7
