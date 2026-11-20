"""The subscription CLI's own model list (``app.core.claude_cli_models``).

The list comes from the CLI's ``initialize`` reply. On a subscription sign-in
it labels the four aliases, offers every version it lists (Mythos included),
and makes a listed id mean itself; in every other mode the static registry is
unchanged.
"""

import json

import pytest

from app.core import claude_auth_mode as _auth_mode
from app.core import claude_cli_models as cm
from app.core import claude_cli_status as _cli_status
from app.core import model_registry as m
from app.core.model_effort import effort_capabilities

_ALL = ["low", "medium", "high", "xhigh", "max"]
# The unpatched functions, for tests that need the real pin-aware path.
_REAL_CURRENT_MODE = _auth_mode.current_mode
_REAL_CLAUDE_PAYLOAD = m._claude_payload
_REAL_PEEK = _cli_status.peek


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
        _entry("claude-mythos-5-1", "Mythos 5.1", "claude-mythos-5-1"),
        _entry("claude-opus-5", "Opus 5", "claude-opus-5"),
        _entry("claude-opus-4-6", "Opus 4.6", "claude-opus-4-6", ["low", "medium", "high", "max"]),
        _entry("claude-sonnet-4-6", "Sonnet 4.6", "claude-sonnet-4-6", ["low", "medium", "high", "max"]),
        _entry("claude-haiku-4-5-20251001", "Haiku 4.5", "claude-haiku-4-5-20251001", None, None),
    ],
}


@pytest.fixture
def cli(monkeypatch):
    """A captured list and a subscription sign-in."""
    from app.core import claude_auth_mode, claude_cli_status

    # The active sign-in is the account REPLY was captured under.
    state = {"mode": "subscription",
             "status": claude_cli_status.ClaudeCliStatus(installed=True, signed_in=True, email="a@example.invalid",
                                                         cli_version="2.1.325")}
    monkeypatch.setattr(cm, "_snapshot", cm.parse_initialize(REPLY))
    monkeypatch.setattr(cm, "_loaded", True)
    monkeypatch.setattr(claude_auth_mode, "current_mode", lambda: state["mode"])
    monkeypatch.setattr(claude_cli_status, "peek", lambda: state["status"])
    monkeypatch.setattr(claude_cli_status, "get_status", lambda *a, **k: state["status"])
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


def test_payload_offers_the_aliases_then_every_listed_version(cli):
    payload = m.registry_payload()
    assert "versioned_models" not in payload
    assert [e["value"] for e in payload["models"]] == [
        "haiku", "sonnet", "opus", "fable",
        "claude-fable-5-1", "claude-mythos-5-1", "claude-opus-5", "claude-opus-4-6",
        "claude-sonnet-4-6", "claude-haiku-4-5-20251001"]
    by = {e["value"]: e for e in payload["models"]}
    assert by["opus"]["label"] == "Opus 5.5"
    assert by["claude-opus-4-6"]["effort_levels"] == ["low", "medium", "high", "max"]
    assert by["claude-haiku-4-5-20251001"]["context_window"] == 200_000
    # On a subscription the picker folds no full id onto an alias.
    assert not [k for k in payload["legacy_map"] if k.startswith("claude-")]


def test_mythos_is_its_own_family_and_ranks_no_alias(cli):
    from app.core.credential_gate import _claude_alias

    payload = {e["value"]: e for e in m.registry_payload()["models"]}
    assert payload["claude-mythos-5-1"]["label"] == "Mythos 5.1"
    assert payload["claude-mythos-5-1"]["family"] == "mythos"
    assert payload["claude-mythos-5-1"]["context_window"] == 1_000_000
    assert m.native_choice("claude-mythos-5-1") == "claude-mythos-5-1"
    # Usage on a Mythos model never makes "mythos" a default alias.
    assert _claude_alias("claude-mythos-5-1") is None
    assert _claude_alias("claude-opus-4-6") == "opus"


def test_an_unknown_family_is_still_listed_and_selectable(cli, monkeypatch):
    """A family this release does not know yet keeps its row, with no family."""
    reply = {**REPLY, "models": [*REPLY["models"], _entry("claude-nova-6", "Nova 6", "claude-nova-6")]}
    monkeypatch.setattr(cm, "_snapshot", cm.parse_initialize(reply))
    payload = {e["value"]: e for e in m.registry_payload()["models"]}
    assert payload["claude-nova-6"]["label"] == "Nova 6"
    assert payload["claude-nova-6"]["family"] is None
    assert payload["claude-nova-6"]["context_window"] == 1_000_000
    assert m.native_choice("claude-nova-6") == "claude-nova-6"
    assert m.selectable_model_values()[-1] == "claude-nova-6"


def test_subagent_model_enums_offer_every_listed_version(cli):
    from app.mcp import config_subagents

    values = m.selectable_model_values()
    assert values[:4] == m.model_values()
    assert "claude-mythos-5-1" in values and "claude-opus-4-6" in values
    tools = {t.name: t for t in config_subagents._create_subagent_tools(True)}
    for name in ("createSubagent", "updateSubagent"):
        assert "claude-mythos-5-1" in tools[name].input_schema["properties"]["model"]["enum"]
    cli["mode"] = "api"
    assert m.selectable_model_values() == m.model_values()


def test_a_refreshed_list_rebuilds_a_pooled_app_config_server(cli, monkeypatch):
    """Review r2: an open chat must not keep advertising the old model enums."""
    from app.core.provider.types import mcp_servers_equivalent
    from app.mcp import config_subagents
    from app.mcp.app_config_sdk import schema_signature

    def server():
        tools = config_subagents._create_subagent_tools(True)
        return {"app-config": {"type": "sdk", "name": "app-config", "instance": object(),
                               "schema_signature": schema_signature(tools)}}

    before = server()
    assert mcp_servers_equivalent(before, server())
    reply = {**REPLY, "models": [*REPLY["models"], _entry("claude-mythos-6", "Mythos 6", "claude-mythos-6")]}
    monkeypatch.setattr(cm, "_snapshot", cm.parse_initialize(reply))
    assert not mcp_servers_equivalent(before, server())


def test_an_inherited_full_id_reaches_a_paired_worker_as_itself(cli, monkeypatch):
    """Review S2: a chat on Mythos starts a paired-worker chat on Mythos.
    An explicit request keeps the alias-only rule."""
    from app.core import model_override, session_orchestration as so

    monkeypatch.setattr(model_override, "_provider_rows", lambda: [])
    for runs_here in (False, True):
        picked = so._inherited_choice("claude-mythos-5-1", runs_here=runs_here)
        assert (picked.model, picked.provider_id) == ("claude-mythos-5-1", None)
    assert so._inherited_choice("opus", runs_here=False).model == "opus"
    with pytest.raises(ValueError):
        model_override.resolve("claude-mythos-5-1", runs_here=False)
    cli["mode"] = "api"
    with pytest.raises(ValueError):
        so._inherited_choice("claude-mythos-5-1", runs_here=False)


def test_other_modes_keep_the_static_registry(cli):
    cli["mode"] = "api"
    payload = m.registry_payload()
    assert [e["value"] for e in payload["models"]] == ["haiku", "sonnet", "opus", "fable"]
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
    "same", "same-default", "switch", "sign-out", "organization", "folder", "shared-selection", "invalidated",
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
    # A verified stock home still has a binding; it is not a legacy probe.
    current = replace(current, selected_home=(target, None if concurrent_change == "same-default" else home))
    state = {"status": current, "target": target}
    monkeypatch.setattr(subs, "shared_target", lambda: state["target"])
    monkeypatch.setattr(claude_cli_status, "probe", lambda resolver: state["status"])

    def handshake(binary, env):
        assert binary == current.binary_path
        if concurrent_change == "same-default":
            assert "CLAUDE_CONFIG_DIR" not in env
        else:
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
    if concurrent_change in {"same", "same-default"}:
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


def test_a_sign_in_switch_mid_payload_never_mixes_two_lists(cli, monkeypatch):
    """Review R7-S1: one payload describes one account's list."""
    from app.core import claude_cli_status

    account_b = claude_cli_status.ClaudeCliStatus(installed=True, signed_in=True, email="b@example.invalid",
                                                  cli_version="2.1.325")
    first_read = m.delegated_model_aliases

    def switch_after_the_aliases():
        aliases = first_read()
        cli["status"] = account_b  # the owner switches accounts mid-projection
        return aliases

    monkeypatch.setattr(m, "delegated_model_aliases", switch_after_the_aliases)
    by = {e["value"]: e for e in m.registry_payload()["models"]}
    # Still entirely account A's list: its alias labels and its versions.
    assert by["opus"]["label"] == "Opus 5.5" and by["fable"]["label"] == "Fable 5.2"
    assert "claude-mythos-5-1" in by and "claude-opus-4-6" in by
    # The pin ends with the payload: the next read is B's (none captured yet).
    assert cm.catalog() is None
    monkeypatch.setattr(m, "delegated_model_aliases", first_read)
    by = {e["value"]: e for e in m.registry_payload()["models"]}
    assert list(by) == ["haiku", "sonnet", "opus", "fable"]


def test_a_mode_switch_mid_payload_never_mixes_subscription_rows_with_cloud(cli, monkeypatch):
    """Review R8-S1: rows, auth metadata and the legacy map share one mode."""
    from app.core import claude_auth_mode

    # Keep the real projection pin and runtime choice, backed by a switchable setting.
    monkeypatch.setattr(claude_auth_mode, "current_mode", _REAL_CURRENT_MODE)
    monkeypatch.setattr(claude_auth_mode, "_stored_choice", lambda *, cached: (cli["mode"], {"mode": cli["mode"]}))
    monkeypatch.setattr(m, "_claude_payload", _REAL_CLAUDE_PAYLOAD)
    first_read = m.delegated_model_aliases

    def switch_after_the_aliases():
        aliases = first_read()
        cli["mode"] = claude_auth_mode.MODE_CLOUD  # the owner turns on Cloud mid-projection
        assert claude_auth_mode.runtime_choice()[0] == claude_auth_mode.MODE_CLOUD
        return aliases

    monkeypatch.setattr(m, "delegated_model_aliases", switch_after_the_aliases)
    payload = m.registry_payload()
    by = {e["value"]: e for e in payload["models"]}
    assert "claude-mythos-5-1" in by and "claude-opus-4-6" in by
    assert payload["claude"]["auth_kind"] == "subscription"
    assert "claude-opus-4-6" not in payload["legacy_map"]  # still means itself
    # After the payload the switch shows: Cloud, no subscription rows.
    monkeypatch.setattr(m, "delegated_model_aliases", first_read)
    payload = m.registry_payload()
    assert not any(e["value"].startswith("claude-") for e in payload["models"])
    assert payload["claude"]["auth_kind"] != "subscription"


def test_an_account_switch_mid_payload_keeps_the_plan_and_list_together(cli, monkeypatch):
    """Review R9-S1: the plan in the payload is the listed account's."""
    from app.core import claude_cli_status

    account_a = claude_cli_status.ClaudeCliStatus(installed=True, signed_in=True, email="a@example.invalid",
                                                  cli_version="2.1.325", plan="max")
    account_b = claude_cli_status.ClaudeCliStatus(installed=True, signed_in=True, email="b@example.invalid",
                                                  cli_version="2.1.325", plan="pro")
    # The real pin-aware peek, over the module's cache.
    monkeypatch.setattr(claude_cli_status, "peek", _REAL_PEEK)
    monkeypatch.setattr(claude_cli_status, "_cached", account_a)
    monkeypatch.setattr(m, "_claude_payload", _REAL_CLAUDE_PAYLOAD)
    first_read = m.delegated_model_aliases

    def switch_after_the_aliases():
        aliases = first_read()
        monkeypatch.setattr(claude_cli_status, "_cached", account_b)  # a saved-login switch lands
        return aliases

    monkeypatch.setattr(m, "delegated_model_aliases", switch_after_the_aliases)
    payload = m.registry_payload()
    assert "claude-mythos-5-1" in {e["value"] for e in payload["models"]}
    assert payload["claude"]["plan_type"] == "max"
    monkeypatch.setattr(m, "delegated_model_aliases", first_read)
    payload = m.registry_payload()
    assert "claude-mythos-5-1" not in {e["value"] for e in payload["models"]}
    assert payload["claude"]["plan_type"] == "pro"


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


def test_a_preview_never_refreshes_the_cli_list():
    """Refresh starts the CLI: owner only, even on a throwaway preview."""
    from unittest.mock import patch

    from starlette.requests import Request

    from app.api.auth import _preview_may_change_dev_data

    refresh = Request({"type": "http", "method": "POST", "path": "/api/v1/models/claude/refresh", "headers": []})
    for disposable in (True, False):
        with patch("app.preview_run.disposable_preview_data", return_value=disposable):
            assert _preview_may_change_dev_data(refresh) is False


def test_subscription_curation_filters_pickers_and_defaults_but_keeps_metadata(cli, mock_db, monkeypatch, tmp_path):
    """S1 + S2 + S3: independent per-model keys, hidden_models metadata, and no-enabled-default refusal."""
    import asyncio
    from unittest.mock import patch

    from fastapi import Response
    from starlette.requests import Request

    from app.api import command_context, query as query_api
    from app.api.auth import _preview_may_change_dev_data
    from app.api.model_registry import (
        ToggleClaudeSubscriptionModelRequest,
        list_claude_subscription_models,
        toggle_claude_subscription_model,
    )
    from app.core import automation_service, credential_gate, generation_options, query_options
    from app.db import database
    from app.mcp import config_subagents
    from app.network.policy import is_shared_setting_key

    # Default: all models enabled.
    full = cm.subscription_catalog_rows()
    assert full["captured"] is True
    assert all(row["enabled"] is True for row in full["models"])

    # S3: disable sonnet and claude-mythos-5-1 -> two independent shared system_settings rows.
    writes = []
    real_set = database.set_system_setting
    monkeypatch.setattr(database, "set_system_setting", lambda k, v: (writes.append((k, v)), real_set(k, v))[1])
    assert cm.set_model_enabled("sonnet", False) == ["sonnet"]
    assert cm.set_model_enabled("sonnet", False) == ["sonnet"]  # no-op suppressed
    assert cm.set_model_enabled("claude-mythos-5-1", False) == ["claude-mythos-5-1", "sonnet"]
    assert writes == [
        (f"{cm.DISABLED_MODEL_PREFIX}sonnet", "1"),
        (f"{cm.DISABLED_MODEL_PREFIX}claude-mythos-5-1", "1"),
    ]
    assert is_shared_setting_key(f"{cm.DISABLED_MODEL_PREFIX}sonnet") is True
    assert is_shared_setting_key(f"{cm.DISABLED_MODEL_PREFIX}claude-mythos-5-1") is True

    # Re-enabling writes "0" (preserving LWW revision) and no-op repeat writes nothing.
    assert cm.set_model_enabled("sonnet", True) == ["claude-mythos-5-1"]
    assert cm.set_model_enabled("sonnet", True) == ["claude-mythos-5-1"]
    assert writes[-1] == (f"{cm.DISABLED_MODEL_PREFIX}sonnet", "0")
    assert len(writes) == 3
    # Re-disable sonnet for the remaining assertions.
    assert cm.set_model_enabled("sonnet", False) == ["claude-mythos-5-1", "sonnet"]

    # Pickers & selectable enums hide disabled models; S2 hidden_models retains full live metadata.
    payload = m.registry_payload()
    offered = [item["value"] for item in payload["models"]]
    assert "sonnet" not in offered
    assert "claude-mythos-5-1" not in offered
    assert "haiku" in offered and "opus" in offered and "claude-opus-4-6" in offered
    hidden_by_value = {item["value"]: item for item in payload["hidden_models"]}
    assert set(hidden_by_value) == {"sonnet", "claude-mythos-5-1"}
    assert hidden_by_value["sonnet"]["label"] == "Sonnet 5.5"
    assert hidden_by_value["claude-mythos-5-1"]["label"] == "Mythos 5.1"
    assert hidden_by_value["claude-mythos-5-1"]["family"] == "mythos"
    assert hidden_by_value["claude-mythos-5-1"]["context_window"] == 1_000_000
    assert hidden_by_value["claude-mythos-5-1"]["effort_levels"] == _ALL
    selectable = m.selectable_model_values()
    assert "sonnet" not in selectable and "claude-mythos-5-1" not in selectable

    # Metadata/resolution helpers still work on disabled models for existing chats.
    assert m.get_model("sonnet").label == "Sonnet 5.5"
    assert m.get_model("claude-mythos-5-1").label == "Mythos 5.1"
    assert m.get_model_family("claude-mythos-5-1") == "mythos"
    assert m.get_context_window("claude-mythos-5-1") == 1_000_000
    assert effort_capabilities("claude-mythos-5-1", route="delegated")["effort_levels"] == _ALL
    assert m.normalize_model("claude-sonnet-4-6") == "sonnet"
    assert m.keeps_own_id("claude-mythos-5-1") is True
    assert m.native_choice("claude-mythos-5-1") == "claude-mythos-5-1"
    assert m.delegated_model_aliases()["sonnet"] == "claude-sonnet-5-5"

    # Default model falls back to the first enabled model when sonnet is disabled.
    monkeypatch.setattr(credential_gate, "_claude_ready_for_default", lambda probe: True)
    monkeypatch.setattr(credential_gate, "_default_candidate_rows", lambda servable_only=False: [])
    assert credential_gate._claude_default("sonnet", probe=False) == (None, "haiku")
    assert credential_gate.default_run_selection(probe=False) == "haiku"

    # Unfiltered Settings endpoint still lists every model with its enabled flag.
    sub_list = asyncio.run(list_claude_subscription_models(Response(), "admin"))
    by_id = {row["id"]: row for row in sub_list["models"]}
    assert by_id["sonnet"]["enabled"] is False
    assert by_id["claude-mythos-5-1"]["enabled"] is False
    assert by_id["haiku"]["enabled"] is True

    # S1: disable EVERY subscription model on a Claude-only worker -> no fabricated "sonnet" default!
    for row in sub_list["models"]:
        asyncio.run(toggle_claude_subscription_model(
            ToggleClaudeSubscriptionModelRequest(model=row["id"], enabled=False), "admin"
        ))
    assert m.selectable_model_values() == []
    all_disabled_payload = m.registry_payload()
    assert all_disabled_payload["models"] == []
    assert len(all_disabled_payload["hidden_models"]) == len(sub_list["models"])
    assert all_disabled_payload["default"] == ""
    assert credential_gate._claude_default("sonnet", probe=False) is None
    assert credential_gate.default_run_model(probe=False) == (None, None)
    assert credential_gate.default_run_selection(probe=False) == ""

    # S1: implicit new-run paths refuse cleanly while explicit model selections still pass through.
    implicit_profile = {"id": "p1", "config": {"cwd": str(tmp_path), "tools_preset": "claude_code"}}
    with patch.object(query_options, "get_security_instructions", return_value=""):
        with pytest.raises(credential_gate.AgentSetupRequired, match="No models are enabled"):
            query_options.build_options_from_profile(profile=implicit_profile)
        explicit_profile = {"id": "p2", "config": {"model": "claude-mythos-5-1", "cwd": str(tmp_path), "tools_preset": "claude_code"}}
        opts, _ = query_options.build_options_from_profile(profile=explicit_profile)
        assert opts.model == "claude-mythos-5-1"
    with pytest.raises(credential_gate.AgentSetupRequired, match="No models are enabled"):
        generation_options.text_generation_config()
    monkeypatch.setattr(database, "get_profile", lambda pid: {"id": pid, "config": {}})
    with pytest.raises(credential_gate.AgentSetupRequired, match="No models are enabled"):
        query_api._request_binding("default", {}, None)
    assert query_api._request_binding("default", {"model": "sonnet"}, None) == (None, "sonnet")
    assert command_context.command_provider_kind(None, None, None, None, None) == "unavailable"
    auto_model = automation_service.resolve_automation_model({"id": "a1", "profile_id": "default"})
    assert auto_model["model"] is None and "No models are enabled" in (auto_model["error"] or "")

    tools = {t.name: t for t in config_subagents._create_subagent_tools(True)}
    for name in ("createSubagent", "updateSubagent"):
        assert "enum" not in tools[name].input_schema["properties"]["model"]

    # Cloud mode is unaffected by subscription disabled models.
    cli["mode"] = "cloud"
    assert [item["value"] for item in m.registry_payload()["models"]] == ["haiku", "sonnet", "opus", "fable"]

    # Disposable preview allows curation PUTs (never on real installs).
    for path in ("/api/v1/models/claude/subscription/models", "/api/v1/models/claude/api-key/acct-1/models"):
        req = Request({"type": "http", "method": "PUT", "path": path, "headers": []})
        with patch("app.preview_run.disposable_preview_data", return_value=True):
            assert _preview_may_change_dev_data(req) is True
        with patch("app.preview_run.disposable_preview_data", return_value=False):
            assert _preview_may_change_dev_data(req) is False
