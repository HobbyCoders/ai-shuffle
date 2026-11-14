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
    "account": {"email": "x"},
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
    state = {"mode": "subscription", "enabled": []}
    from app.core import claude_auth_mode

    monkeypatch.setattr(cm, "_snapshot", cm.parse_initialize(REPLY))
    monkeypatch.setattr(cm, "_loaded", True)
    monkeypatch.setattr(claude_auth_mode, "current_mode", lambda: state["mode"])
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
    assert "account" not in snap


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


def test_set_enabled_drops_aliases_and_duplicates(monkeypatch):
    from app.core import claude_cli_status
    from app.db import settings as db_settings

    saved = {}
    monkeypatch.setattr(db_settings, "set_system_setting", lambda k, v: saved.update({k: v}))
    monkeypatch.setattr(claude_cli_status, "announce_account_change", lambda: None)
    assert cm.set_enabled_models(["opus", "claude-opus-4-6", "claude-opus-4-6", 3]) == ["claude-opus-4-6"]
    assert json.loads(saved[cm.ENABLED_SETTING]) == ["claude-opus-4-6"]
