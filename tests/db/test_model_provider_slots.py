"""Saved-account slot classification, atomic writes, and the v93 upgrade."""
import pytest

from app.db import database as db
from app.db.model_provider_slots import (
    SLOT_SETTING_PREFIX, lock_model_provider_slots, model_provider_slot,
    reconcile_model_provider_slots,
)


def account(id, kind="openai", enabled=True):
    return db.create_model_provider(id, id, kind, "https://example.com", enabled=enabled,
                                    models=[{"id": "same-model"}])


@pytest.mark.parametrize("kind,auth,slot", [
    ("openai", None, "openai:key"), ("xai", None, "grok:key"),
    ("openrouter", None, "openrouter:key"), ("chatgpt", None, "openai:subscription"),
    ("grok", None, "grok:subscription"), ("custom", "chatgpt", "openai:subscription"),
    ("openai", "grok", "grok:subscription"), ("grok", "chatgpt", "openai:subscription"),
    ("custom", None, None), ("anthropic", None, None),
])
def test_slot_matches_settings_classification(kind, auth, slot):
    assert model_provider_slot({"provider_type": kind, "auth_mode": auth}) == slot


def test_create_and_update_select_exclusively(mock_db):
    account("a")
    account("b")
    assert not db.get_model_provider("a")["enabled"]
    assert db.get_model_provider("b")["enabled"]
    db.update_model_provider("a", enabled=True)
    assert db.get_model_provider("a")["enabled"]
    assert not db.get_model_provider("b")["enabled"]
    db.set_model_provider_enabled("a", False)
    assert not db.get_enabled_model_providers()
    assert db.resolve_model_provider("b")["id"] == "b"


def test_slots_and_gateways_are_independent(mock_db):
    for id, kind in [("key", "openai"), ("sub", "chatgpt"), ("xai", "xai"),
                     ("grok", "grok"), ("router", "openrouter"), ("gw1", "custom"), ("gw2", "custom")]:
        account(id, kind)
    assert len(db.get_enabled_model_providers()) == 7
    account("replacement", "chatgpt", enabled=False)
    assert db.get_model_provider("sub")["enabled"]
    db.set_model_provider_enabled("replacement", True)
    assert not db.get_model_provider("sub")["enabled"]
    assert db.get_model_provider("key")["enabled"]


def test_reclassifying_enabled_row_selects_its_new_slot(mock_db):
    account("a", "openrouter")
    account("b", "custom")
    db.update_model_provider("b", provider_type="openrouter")
    assert not db.get_model_provider("a")["enabled"]
    assert db.get_model_provider("b")["enabled"]


def test_failed_optimistic_edit_cannot_change_selection(mock_db):
    account("a")
    account("b")
    assert db.update_model_provider("a", enabled=True, expected_revision="stale") is None
    assert db.get_model_provider("b")["enabled"]


def test_slot_update_rolls_back_selection_and_siblings(mock_db, monkeypatch):
    from app.db import model_providers
    account("a")
    account("b", enabled=False)
    def fail(cursor):
        raise RuntimeError("rollback")
    monkeypatch.setattr(model_providers, "reconcile_model_provider_slots", fail)
    with pytest.raises(RuntimeError, match="rollback"):
        db.set_model_provider_enabled("b", True)
    assert db.get_model_provider("a")["enabled"]
    assert not db.get_model_provider("b")["enabled"]
    assert db.get_system_setting(SLOT_SETTING_PREFIX + "openai:key") == "a"


def test_v93_keeps_earliest_enabled_account_and_is_idempotent(mock_db):
    for id in ("z", "a", "off"):
        account(id, enabled=False)
    # Real pre-v93 state: duplicates; equal creation timestamps break by id.
    mock_db.execute("DELETE FROM system_settings WHERE key LIKE 'model_provider_slot:%'")
    mock_db.execute("UPDATE model_providers SET created_at='2026-01-01', enabled=1 WHERE id IN ('z','a')")
    mock_db.commit()
    with db.get_db() as conn:
        lock_model_provider_slots(conn.cursor())
        reconcile_model_provider_slots(conn.cursor(), initialize=True)
    assert [r["id"] for r in db.get_enabled_model_providers()] == ["a"]
    assert len(db.get_all_model_providers()) == 3
    db.set_model_provider_enabled("z", True)
    with db.get_db() as conn:
        reconcile_model_provider_slots(conn.cursor(), initialize=True)
    assert [r["id"] for r in db.get_enabled_model_providers()] == ["z"]
    db.set_model_provider_enabled("z", False)
    with db.get_db() as conn:
        reconcile_model_provider_slots(conn.cursor(), initialize=True)
    assert not db.get_enabled_model_providers()


def test_deleted_pin_does_not_guess_a_slot(mock_db):
    account("a")
    account("b")
    db.delete_model_provider("a")
    assert db.resolve_model_provider("a") is None


def test_reclassified_disabled_account_does_not_resurrect_stale_selection(mock_db):
    account("a")
    account("b")
    db.update_model_provider("b", provider_type="custom", enabled=False)
    db.update_model_provider("b", provider_type="openai")
    assert not db.get_model_provider("b")["enabled"]
    assert not db.get_enabled_model_providers()
