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
    # On a subscription the picker folds no full id onto an alias.
    assert not [k for k in payload["legacy_map"] if k.startswith("claude-")]


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


@pytest.mark.parametrize("concurrent_change", [
    "same", "switch", "sign-out", "organization", "folder", "shared-selection", "invalidated",
])
def test_refresh_checks_the_logical_sign_in(monkeypatch, tmp_path, concurrent_change):
    from dataclasses import replace

    from app.core import claude_cli_home, claude_cli_status, claude_subscriptions as subs

    home = "0123456789abcdef"
    other_home = "fedcba9876543210"
    monkeypatch.setattr(claude_cli_home, "root", lambda: tmp_path)
    path = tmp_path / "claude-cli-models.json"
    monkeypatch.setattr(cm, "_path", lambda: path)
    monkeypatch.setattr(cm, "_snapshot", None)
    monkeypatch.setattr(cm, "_loaded", True)
    monkeypatch.setattr(cm, "subscription_mode", lambda: True)
    monkeypatch.setattr(claude_cli_status, "_announce_account_change", lambda: None)
    for attr in ("_cached", "_cached_at", "_last_account"):
        monkeypatch.setattr(claude_cli_status, attr, getattr(claude_cli_status, attr))
    current = claude_cli_status.ClaudeCliStatus(
        installed=True, signed_in=True, auth_method="claude.ai", email="a@example.invalid",
        org_id="org-a", binary_path="/fake/claude", cli_version="2.1.325",
    )
    target = subs._binding(current)
    current = replace(current, selected_home=(target, home))
    state = {"status": current, "target": target}
    monkeypatch.setattr(subs, "shared_target", lambda: state["target"])
    monkeypatch.setattr(claude_cli_status, "probe", lambda resolver: state["status"])

    def handshake(binary, env):
        assert binary == current.binary_path
        assert env["CLAUDE_CONFIG_DIR"] == str(tmp_path / home)
        # Simulate a forced status read completing during this handshake, or a
        # replicated choice arriving before the next probe has run.
        observed = replace(current)
        if concurrent_change == "switch":
            observed = replace(observed, email="b@example.invalid")
            state["target"] = subs._binding(observed)
            observed = replace(observed, selected_home=(state["target"], other_home))
        elif concurrent_change == "sign-out":
            observed = replace(observed, signed_in=False)
        elif concurrent_change == "organization":
            observed = replace(observed, org_id="org-b")  # same email and display name
        elif concurrent_change == "folder":
            observed = replace(observed, selected_home=(target, other_home))
        elif concurrent_change == "shared-selection":
            state["target"] = "unresolved"
        if concurrent_change == "invalidated":
            claude_cli_status.invalidate()
        elif concurrent_change != "shared-selection":
            state["status"] = observed
            assert claude_cli_status.get_status(force=True) is not current
        return REPLY

    monkeypatch.setattr(cm, "_handshake", handshake)
    if concurrent_change == "same":
        assert cm.refresh()["account"] == cm.account_key(current.email, current.org_name)
        assert path.is_file()
    else:
        with pytest.raises(RuntimeError, match="sign-in changed"):
            cm.refresh()
        assert cm.peek() is None and not path.exists()


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


@pytest.mark.parametrize("saved", ["listed", "no list", "list without it", "other account", "unknown account"])
def test_a_full_id_is_never_folded_when_stored_inherited_or_sent(cli, monkeypatch, saved):
    """Review S1/S5: on a subscription a full id means itself on every path,
    whatever this worker's saved list says; API-key and cloud fold as before."""
    from app.core import claude_cli_status, model_override, subagent_default
    from app.core.agent_provider_models import leg_model_id
    from app.core.task_dispatch import _native_task_model
    from app.db import database

    snap = cm.parse_initialize(REPLY)
    if saved == "no list":
        monkeypatch.setattr(cm, "_snapshot", None)
    elif saved == "list without it":
        monkeypatch.setattr(cm, "_snapshot", {**snap, "models": [e for e in snap["models"]
                                                                if e["value"] != "claude-opus-4-6"]})
    elif saved == "other account":
        cli["status"] = claude_cli_status.ClaudeCliStatus(installed=True, signed_in=True, email="b@example.invalid")
    elif saved == "unknown account":
        cli["status"] = None
    # Saved on another worker; read here.
    stored = {subagent_default.SETTING_KEY: json.dumps({"model": "opus", "version": "claude-opus-4-6"})}
    monkeypatch.setattr(database, "get_system_setting", lambda k: stored.get(k))
    monkeypatch.setattr(database, "set_system_setting", lambda k, v: stored.update({k: v}))
    monkeypatch.setattr(subagent_default, "reset_asked", lambda: None)
    for value in ("claude-opus-4-6", "claude-opus-4-8", "claude-opus-9"):
        assert m.native_choice(value) == value
        assert leg_model_id(None, value) == value
        assert _native_task_model(value) == value
        assert model_override.app_id(None, value) == value
    # Description only: a kept id with no entry of its own reads as its family.
    assert m.get_model_family("claude-opus-5[1m]") == m.get_model_family("claude-opus-4-8") == "opus"
    assert m.get_context_window("claude-opus-4-8") == m.get_context_window("opus")
    assert subagent_default.read().model == "claude-opus-4-6"
    assert subagent_default.effective().model == "claude-opus-4-6"
    # Written here: older releases read ``model`` and run the alias until they update.
    assert subagent_default.write("claude-opus-4-6").model == "claude-opus-4-6"
    assert json.loads(stored[subagent_default.SETTING_KEY]) == {"model": "opus", "version": "claude-opus-4-6"}
    assert subagent_default.write("sonnet").model == "sonnet"
    assert json.loads(stored[subagent_default.SETTING_KEY]) == {"model": "sonnet"}
    # API-key and cloud modes fold exactly as on main.
    for mode in ("api", "cloud"):
        cli["mode"] = mode
        assert subagent_default.write("claude-opus-4-6").model == "opus"
        assert json.loads(stored[subagent_default.SETTING_KEY]) == {"model": "opus"}
        stored[subagent_default.SETTING_KEY] = json.dumps({"model": "opus", "version": "claude-opus-4-6"})
        assert subagent_default.effective().model == "opus"
        assert leg_model_id(None, "claude-opus-4-6") == "opus"
        assert _native_task_model("claude-opus-4-6") == "opus"
        assert model_override.app_id(None, "claude-opus-4-6") == "opus"
        assert m.native_choice("claude-opus-9") is None


def test_the_list_belongs_to_the_account_it_was_captured_under(cli, monkeypatch):
    """Review S3: another sign-in never sees, keeps or overwrites the list."""
    from app.core import claude_cli_status, claude_subscriptions

    monkeypatch.setattr(cm, "_path", lambda: None)
    monkeypatch.setattr(claude_cli_status, "announce_account_change", lambda: None)
    account_a = cli["status"]
    account_b = claude_cli_status.ClaudeCliStatus(installed=True, signed_in=True, email="b@example.invalid",
                                                  cli_version="2.1.325")
    key_a = cm.account_key("a@example.invalid", None)
    assert cm.catalog() is not None
    assert cm.peek()["account"] == key_a
    assert "a@example" not in json.dumps(cm.peek())  # a hash, never the email
    # Switched to B (or restarted signed in as B): A's list is not served.
    cli["status"] = account_b
    assert cm.catalog() is None
    assert cm.entry("claude-opus-4-6") is None
    # A handshake or chat started under A that answers now is not stored.
    monkeypatch.setattr(cm, "_snapshot", None)
    cm._record(REPLY)
    assert cm.peek() is None
    # Startup after the switch asks the CLI again, though its version is unchanged.
    refreshed = []
    monkeypatch.setattr(cm, "refresh", lambda: refreshed.append(1))
    cm.refresh_if_stale("2.1.325")
    assert refreshed == [1]
    # Signed out, or the account not known yet: nothing is served or stored.
    for status in (None, claude_cli_status.ClaudeCliStatus(installed=True, signed_in=False)):
        cli["status"] = status
        assert cm.store(cm.parse_initialize(REPLY)) is None
    # Back on A: stored and served. A reply from a superseded CLI is not stored.
    cli["status"] = account_a
    assert cm.store(cm.parse_initialize({**REPLY, "claude_code_version": "2.1.300"})) is None
    cm._record(REPLY)
    assert cm.peek()["account"] == key_a and cm.catalog() is not None
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
    claude_subscriptions._switched()
    assert asked == [1]


def test_a_capture_from_the_previous_account_never_replaces_the_active_one(cli, monkeypatch):
    """Review S6: A's capture is checked at the write, after B's list landed."""
    from app.core import claude_cli_status

    monkeypatch.setattr(cm, "_path", lambda: None)
    monkeypatch.setattr(claude_cli_status, "announce_account_change", lambda: None)
    monkeypatch.setattr(cm, "_snapshot", None)
    account_a = cli["status"]
    reply_b = {**REPLY, "account": {"email": "b@example.invalid"}, "models": REPLY["models"][:5]}

    def switch_while_a_is_in_flight(*args, **kwargs):
        # A's capture knows A is active; then the owner switches to B and
        # B's refresh stores B's list before A's capture writes.
        cli["status"] = claude_cli_status.ClaudeCliStatus(
            installed=True, signed_in=True, email="b@example.invalid", cli_version="2.1.325")
        assert cm.store(cm.parse_initialize(reply_b)) is True
        return account_a

    monkeypatch.setattr(claude_cli_status, "get_status", switch_while_a_is_in_flight)
    cm._record(REPLY)
    assert cm.peek()["account"] == cm.account_key("b@example.invalid", None)
    assert len(cm.catalog()["models"]) == 4  # B's list (aliases only), not A's
    # A handshake that ends before B's status is known is refused too.
    cli["status"] = None
    monkeypatch.setattr(claude_cli_status, "get_status", lambda *a, **k: account_a)
    cm._record(REPLY)
    assert cm.peek()["account"] == cm.account_key("b@example.invalid", None)


def test_a_throwaway_preview_may_turn_models_on_and_off_and_a_real_install_may_not():
    """The dev loop's journeys drive the Models tab through the preview cookie."""
    from unittest.mock import patch

    from starlette.requests import Request

    from app.api.auth import _preview_may_change_dev_data
    from app.core.preview_access import mutation_allowed

    request = Request({"type": "http", "method": "PUT", "path": "/api/v1/models/claude/enabled", "headers": []})
    refresh = Request({"type": "http", "method": "POST", "path": "/api/v1/models/claude/refresh", "headers": []})
    assert mutation_allowed("/api/v1/models/claude/enabled", "PUT")
    for disposable in (True, False):
        with patch("app.preview_run.disposable_preview_data", return_value=disposable):
            assert _preview_may_change_dev_data(request) == disposable
            assert _preview_may_change_dev_data(refresh) is False
