"""The subscription CLI's own model list (``app.core.claude_cli_models``).

The list comes from the CLI's ``initialize`` reply. On a subscription sign-in
it labels the four aliases, offers the versions it lists (off until the owner
turns them on), and makes a listed id mean itself; in every other mode the
static registry is unchanged.
"""

import json

import pytest

from app.core import claude_cli_models as cm
from app.core import model_registry as m
from app.core.model_effort import effort_capabilities

_ALL = ["low", "medium", "high", "xhigh", "max"]


def _entry(value, label, resolved, levels=_ALL, supports=True):
    return {"value": value, "resolvedModel": resolved, "displayName": label, "description": "d",
            "supportsEffort": supports, "supportedEffortLevels": levels}


# The shape Claude Code 2.1.325 answers ``initialize`` with on a subscription.
REPLY = {
    "claude_code_version": "2.1.325",
    "account": {"email": "a@example.invalid"},
    "models": [
        _entry("default", "Default (recommended)", "claude-opus-5-5"),
        _entry("opus", "Opus 5.5", "claude-opus-5-5"),
        _entry("fable", "Fable 5.2", "claude-fable-5-2"),
        _entry("sonnet", "Sonnet 5.5", "claude-sonnet-5-5"),
        _entry("haiku", "Haiku 5.5", "claude-haiku-5-5"),
        _entry("claude-fable-5-1", "Fable 5.1", "claude-fable-5-1"),
        _entry("claude-opus-5", "Opus 5", "claude-opus-5"),
        _entry("claude-opus-4-6", "Opus 4.6", "claude-opus-4-6", ["low", "medium", "high", "max"]),
        _entry("claude-sonnet-4-6", "Sonnet 4.6", "claude-sonnet-4-6", ["low", "medium", "high", "max"]),
        _entry("claude-haiku-4-5-20251001", "Haiku 4.5", "claude-haiku-4-5-20251001", None, None),
    ],
}


@pytest.fixture
def cli(monkeypatch):
    """A captured list, a subscription sign-in, nothing turned on."""
    from app.core import claude_auth_mode, claude_cli_status

    # The active sign-in is the account REPLY was captured under.
    state = {"mode": "subscription", "enabled": [],
             "status": claude_cli_status.ClaudeCliStatus(installed=True, signed_in=True, email="a@example.invalid",
                                                         cli_version="2.1.325")}
    monkeypatch.setattr(cm, "_snapshot", cm.parse_initialize(REPLY))
    monkeypatch.setattr(cm, "_loaded", True)
    monkeypatch.setattr(claude_auth_mode, "current_mode", lambda: state["mode"])
    monkeypatch.setattr(claude_cli_status, "peek", lambda: state["status"])
    monkeypatch.setattr(claude_cli_status, "get_status", lambda *a, **k: state["status"])
    monkeypatch.setattr(cm, "enabled_models", lambda: list(state["enabled"]))
    monkeypatch.setattr(m, "_providers_payload", lambda: [])
    monkeypatch.setattr(m, "_claude_payload", lambda: {})
    return state


def test_parse_skips_default_and_keeps_effort_levels():
    snap = cm.parse_initialize(REPLY)
    values = [e["value"] for e in snap["models"]]
    assert "default" not in values
    assert values[:4] == ["opus", "fable", "sonnet", "haiku"]
    assert snap["cli_version"] == "2.1.325"
    by = {e["value"]: e for e in snap["models"]}
    assert by["claude-opus-4-6"]["effort_levels"] == ["low", "medium", "high", "max"]
    assert by["claude-haiku-4-5-20251001"]["effort_levels"] == []
    assert by["fable"]["resolved"] == "claude-fable-5-2"
    # The account is kept only as a hash, never the email.
    assert snap["account"] == cm.account_key("a@example.invalid", None) and len(snap["account"]) == 16


@pytest.mark.parametrize("payload", [None, {}, {"models": []}, {"models": "x"}, {"models": [{"value": ""}]}])
def test_parse_without_a_list_is_none(payload):
    assert cm.parse_initialize(payload) is None


def test_aliases_are_labelled_and_sized_as_the_cli_resolves_them(cli):
    assert m.get_model("opus").label == "Opus 5.5"
    assert m.get_model("fable").label == "Fable 5.2"
    assert m.delegated_model_aliases()["opus"] == "claude-opus-5-5"
    assert m.delegated_model_aliases()["fable"] == "claude-fable-5-2"


def test_a_listed_version_means_itself_on_the_subscription_leg(cli):
    assert m.get_model("claude-opus-4-6").label == "Opus 4.6"
    assert m.get_model("claude-opus-4-6").family == "opus"
    assert m.cli_offers("claude-opus-5")
    assert not m.cli_offers("claude-opus-9")
    # The API leg and the host rules keep collapsing old ids (out of scope).
    assert m.normalize_model("claude-opus-4-6") == "opus"
    assert m.resolve_api_model_id("opus") == "claude-opus-5"


def test_effort_and_window_follow_the_picked_version(cli):
    assert effort_capabilities("claude-opus-4-6", route="delegated")["effort_levels"] == ["low", "medium", "high", "max"]
    assert effort_capabilities("claude-sonnet-4-6", route="delegated")["effort_levels"] == ["low", "medium", "high", "max"]
    assert effort_capabilities("claude-opus-5", route="delegated")["effort_levels"] == _ALL
    haiku = effort_capabilities("claude-haiku-4-5-20251001", route="delegated")
    assert haiku["effort_levels"] == []
    assert m.get_context_window("claude-haiku-4-5-20251001") == 200_000
    assert m.get_context_window("claude-opus-4-6") == 1_000_000
    assert effort_capabilities("opus", route="delegated")["effort_levels"] == _ALL


def test_payload_offers_aliases_and_lists_versions_off_by_default(cli):
    payload = m.registry_payload()
    assert [e["value"] for e in payload["models"]] == ["haiku", "sonnet", "opus", "fable"]
    assert {e["value"]: e["label"] for e in payload["models"]}["opus"] == "Opus 5.5"
    versions = {e["value"]: e for e in payload["versioned_models"]}
    assert set(versions) == {"claude-fable-5-1", "claude-opus-5", "claude-opus-4-6",
                             "claude-sonnet-4-6", "claude-haiku-4-5-20251001"}
    assert not any(e["enabled"] for e in versions.values())
    assert versions["claude-opus-4-6"]["effort_levels"] == ["low", "medium", "high", "max"]
    assert versions["claude-haiku-4-5-20251001"]["context_window"] == 200_000
    # Listed ids are no longer folded onto aliases in the picker; unlisted ones still are.
    assert "claude-opus-4-6" not in payload["legacy_map"]
    assert "claude-opus-5" not in payload["legacy_map"]
    assert payload["legacy_map"]["claude-opus-4-8"] == "opus"
    assert payload["legacy_map"]["claude-opus-4-6[1m]"] == "opus"


def test_turned_on_versions_join_the_picker(cli):
    cli["enabled"] = ["claude-opus-4-6"]
    payload = m.registry_payload()
    assert [e["value"] for e in payload["models"]][-1] == "claude-opus-4-6"
    assert payload["models"][-1]["label"] == "Opus 4.6"


def test_other_modes_keep_the_static_registry(cli):
    cli["mode"] = "api"
    payload = m.registry_payload()
    assert payload["versioned_models"] == []
    assert {e["value"]: e["label"] for e in payload["models"]}["opus"] == "Opus"
    assert payload["legacy_map"]["claude-opus-4-6"] == "opus"
    assert m.delegated_model_aliases()["opus"] == "claude-opus-5"
    assert m.get_model("claude-opus-4-6").value == "opus"


def test_record_ignores_other_modes(cli, monkeypatch):
    stored = []
    monkeypatch.setattr(cm, "store", stored.append)
    cli["mode"] = "cloud"
    cm._record(REPLY)
    assert stored == []
    cli["mode"] = "subscription"
    cm._record(REPLY)
    assert stored and stored[0]["cli_version"] == "2.1.325"


def test_store_writes_once_per_change(cli, monkeypatch, tmp_path):
    from app.core import claude_cli_status

    monkeypatch.setattr(cm, "_path", lambda: tmp_path / "claude-cli-models.json")
    announced = []
    monkeypatch.setattr(claude_cli_status, "announce_account_change", lambda: announced.append(1))
    monkeypatch.setattr(cm, "_snapshot", None)
    snap = cm.parse_initialize(REPLY)
    assert cm.store(snap) is True
    assert cm.store({**snap, "captured_at": "later"}) is False
    assert announced == [1]
    saved = json.loads((tmp_path / "claude-cli-models.json").read_text())
    assert saved["cli_version"] == "2.1.325"


def test_toggling_one_model_keeps_ids_this_worker_does_not_list(monkeypatch):
    """Review S2: worker B toggling its model never drops worker A's."""
    from app.core import claude_cli_status
    from app.db import settings as db_settings

    # Saved by worker A: a version only A's CLI lists.
    saved = {cm.ENABLED_SETTING: json.dumps(["claude-only-on-a"])}
    monkeypatch.setattr(db_settings, "get_system_setting", lambda k: saved.get(k))
    monkeypatch.setattr(db_settings, "set_system_setting", lambda k, v: saved.update({k: v}))
    monkeypatch.setattr(claude_cli_status, "announce_account_change", lambda: None)
    assert cm.set_model_enabled("claude-opus-4-6", True) == ["claude-only-on-a", "claude-opus-4-6"]
    assert cm.set_model_enabled("claude-opus-4-6", True) == ["claude-only-on-a", "claude-opus-4-6"]
    assert cm.set_model_enabled("claude-opus-4-6", False) == ["claude-only-on-a"]
    assert json.loads(saved[cm.ENABLED_SETTING]) == ["claude-only-on-a"]
    with pytest.raises(ValueError):
        cm.set_model_enabled("opus", True)


def test_a_picked_version_is_never_folded_when_stored_inherited_or_sent(cli, monkeypatch):
    """Review S1: subagent default, inheritance and Task keep a listed version."""
    from app.core import model_override, subagent_default
    from app.core.agent_provider_models import leg_model_id
    from app.core.task_dispatch import _native_task_model
    from app.db import database

    saved = {}
    monkeypatch.setattr(database, "set_system_setting", lambda k, v: saved.update({k: v}))
    monkeypatch.setattr(subagent_default, "reset_asked", lambda: None)
    assert subagent_default.write("claude-opus-4-6").model == "claude-opus-4-6"
    assert json.loads(saved[subagent_default.SETTING_KEY])["model"] == "claude-opus-4-6"
    assert subagent_default.usable("claude-haiku-4-5-20251001") == "claude-haiku-4-5-20251001"
    assert leg_model_id(None, "claude-opus-4-6") == "claude-opus-4-6"
    assert _native_task_model("claude-opus-4-6") == "claude-opus-4-6"
    # A parent chat's model handed to StartSession on another worker.
    assert model_override.app_id(None, "claude-opus-4-6") == "claude-opus-4-6"
    assert model_override.resolve("claude-opus-4-6", runs_here=False).model == "claude-opus-4-6"
    # A default saved before versions could be picked still means today's alias.
    monkeypatch.setattr(database, "get_system_setting",
                        lambda k: json.dumps({"model": "claude-opus-4-6"}))
    assert subagent_default.read().model == "opus"
    monkeypatch.setattr(database, "get_system_setting", lambda k: saved.get(k))
    assert subagent_default.read().model == "claude-opus-4-6"
    # A legacy id no CLI listed folds as before; aliases are unchanged.
    assert model_override.app_id(None, "claude-opus-4-8") == "opus"
    assert subagent_default.write("opus").model == "opus"
    # After a switch to an account whose list is not known yet, a picked version
    # is refused (the owner is asked), never rewritten to an alias.
    cli["status"] = None
    assert model_override.app_id(None, "claude-opus-4-6") == "claude-opus-4-6"
    assert subagent_default.usable("claude-opus-4-6") is None
    assert _native_task_model("claude-opus-4-6") is None
    with pytest.raises(ValueError):
        model_override.resolve("claude-opus-4-6", runs_here=False)
    # API-key and cloud modes fold as before.
    for mode in ("api", "cloud"):
        cli["mode"] = mode
        assert subagent_default.write("claude-opus-4-6").model == "opus"
        assert model_override.app_id(None, "claude-opus-4-6") == "opus"
        assert _native_task_model("claude-opus-4-6") == "opus"


def test_the_list_belongs_to_the_account_it_was_captured_under(cli, monkeypatch):
    """Review S3: another sign-in never sees, keeps or overwrites the list."""
    from app.core import claude_cli_status, claude_subscriptions

    stored = []
    monkeypatch.setattr(cm, "store", stored.append)
    account_a = cli["status"]
    account_b = claude_cli_status.ClaudeCliStatus(installed=True, signed_in=True, email="b@example.invalid",
                                                  cli_version="2.1.325")
    assert cm.catalog() is not None
    assert cm.peek()["account"] == cm.account_key("a@example.invalid", None)
    assert "a@example" not in json.dumps(cm.peek())  # a hash, never the email
    # Switched to B (or restarted signed in as B): A's list is not served.
    cli["status"] = account_b
    assert cm.catalog() is None
    assert m.cli_offers("claude-opus-4-6") is False
    # A handshake or chat started under A that answers now is not stored.
    cm._record(REPLY)
    assert stored == []
    # Startup after the switch asks the CLI again, though its version is unchanged.
    refreshed = []
    monkeypatch.setattr(cm, "refresh", lambda: refreshed.append(1))
    cm.refresh_if_stale("2.1.325")
    assert refreshed == [1]
    # Signed out, or the account not known yet: nothing is served.
    cli["status"] = None
    assert cm.catalog() is None
    cli["status"] = claude_cli_status.ClaudeCliStatus(installed=True, signed_in=False)
    assert cm.catalog() is None
    # Back on A: served again. A reply from a superseded CLI is not stored.
    cli["status"] = account_a
    assert cm.catalog() is not None
    cm.refresh_if_stale("2.1.325")
    assert refreshed == [1]
    cm._record({**REPLY, "claude_code_version": "2.1.300"})
    assert stored == []
    cm._record(REPLY)
    assert len(stored) == 1
    # A list saved without an account (an earlier build) is never served.
    monkeypatch.setattr(cm, "_snapshot", {**cm.parse_initialize(REPLY), "account": None})
    assert cm.catalog() is None
    # Two accounts with the same list are still two snapshots.
    assert not cm._same(cm.parse_initialize(REPLY),
                        cm.parse_initialize({**REPLY, "account": {"email": "b@example.invalid"}}))
    # A saved-login switch asks the CLI again.
    asked = []
    monkeypatch.setattr(cm, "refresh_soon", lambda: asked.append(1))
    monkeypatch.setattr(claude_cli_status, "invalidate", lambda: None)
    monkeypatch.setattr(claude_cli_status, "announce_account_change", lambda: None)
    claude_subscriptions._switched()
    assert asked == [1]
