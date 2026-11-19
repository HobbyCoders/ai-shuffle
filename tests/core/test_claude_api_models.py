"""A saved Anthropic API key's model list (``app.core.claude_api_models``).

The list comes from Anthropic's Models API for that key, here the dev loop's
fake (``scripts/devloop/fake_wire.py``) over real HTTP. It is kept per key;
in API-key mode the pickers offer the aliases plus the checked versions the
selected key lists, and a full id runs as itself whatever the list says.
"""

import json
from functools import partial

import httpx
import pytest

from app.core import claude_api_models as am
from app.core import model_registry as m
from app.core.model_effort import effort_capabilities
from scripts.devloop.fake_wire import FakeProviderServer

KEY_A = "devloop-fake-key-alpha"
KEY_B = "devloop-fake-key-beta"


@pytest.fixture
def fake(monkeypatch):
    with FakeProviderServer() as server:
        monkeypatch.setattr(am, "models_url", lambda: server.url + am.FAKE_PATH)
        yield server


@pytest.fixture
def keys(monkeypatch):
    """Two saved keys (A selected) in API-key mode, nothing checked."""
    from app.core import claude_accounts, claude_auth_mode, claude_cli_status

    state = {"mode": "api_key", "saved": {"acc-a": KEY_A, "acc-b": KEY_B}, "active": "acc-a",
             "enabled": [], "announced": 0}
    monkeypatch.setattr(claude_accounts, "saved_api_keys", lambda: [
        (i, k, i == state["active"]) for i, k in state["saved"].items()])
    monkeypatch.setattr(claude_auth_mode, "current_mode", lambda: state["mode"])
    monkeypatch.setattr(am, "active_key_id", lambda: (
        am.key_id(state["saved"][state["active"]]) if state["active"] in state["saved"] else None))
    monkeypatch.setattr(am, "enabled_models", lambda: list(state["enabled"]))
    monkeypatch.setattr(am, "_path", lambda: None)
    monkeypatch.setattr(claude_cli_status, "announce_account_change",
                        lambda: state.update(announced=state["announced"] + 1))
    monkeypatch.setattr(m, "_providers_payload", lambda: [])
    monkeypatch.setattr(m, "_claude_payload", lambda: {})
    monkeypatch.setattr(m, "_default_selection", lambda: "sonnet")
    return state


# ─── Fetching ───────────────────────────────────────────────────────────────


def test_fetch_pages_with_the_key_and_version_headers(fake):
    models = am.fetch(KEY_A)
    values = [e["value"] for e in models]
    assert values[0] == "claude-opus-5-5" and values[-1] == "claude-haiku-4-5-20251001"
    assert len(values) == 7  # Three pages of at most three.
    assert len(fake.requests) == 3
    first, second = fake.requests[0], fake.requests[1]
    assert {k.lower(): v for k, v in first.headers.items()}["x-api-key"] == KEY_A
    assert {k.lower(): v for k, v in first.headers.items()}["anthropic-version"] == am.ANTHROPIC_VERSION
    assert "limit=1000" in first.path and "after_id" not in first.path
    assert "after_id=claude-haiku-5-5" in second.path
    by = {e["value"]: e for e in models}
    assert by["claude-opus-4-6"]["label"] == "Claude Opus 4.6"
    assert by["claude-haiku-4-5-20251001"]["context_window"] == 200_000


def test_each_key_gets_its_own_list(fake):
    assert {e["value"] for e in am.fetch(KEY_B)} == {"claude-opus-5-5", "claude-sonnet-5", "claude-opus-4-1"}


def _mock(monkeypatch, handler):
    real = httpx.Client
    monkeypatch.setattr(httpx, "Client", partial(real, transport=httpx.MockTransport(handler)))


@pytest.mark.parametrize("status,reason", [
    (401, "rejected this API key"), (403, "rejected this API key"),
    (429, "rate-limited"), (500, "HTTP 500"), (404, "HTTP 404")])
def test_failures_are_plain_and_never_echo_the_key_or_reply(monkeypatch, status, reason):
    _mock(monkeypatch, lambda request: httpx.Response(status, text=f"echo {request.headers['x-api-key']}"))
    with pytest.raises(am.CatalogError) as caught:
        am.fetch(KEY_A)
    assert reason in str(caught.value)
    assert KEY_A not in str(caught.value) and "echo" not in str(caught.value)


def test_network_errors_oversized_and_malformed_replies_are_refused(monkeypatch):
    def down(request):
        raise httpx.ConnectError("connection refused to " + request.headers["x-api-key"])

    _mock(monkeypatch, down)
    with pytest.raises(am.CatalogError, match="Could not reach Anthropic") as caught:
        am.fetch(KEY_A)
    assert KEY_A not in str(caught.value)
    _mock(monkeypatch, lambda request: httpx.Response(200, content=b"x" * (am._MAX_BYTES + 1)))
    with pytest.raises(am.CatalogError, match="too large"):
        am.fetch(KEY_A)
    _mock(monkeypatch, lambda request: httpx.Response(200, json={"models": []}))
    with pytest.raises(am.CatalogError, match="unexpected"):
        am.fetch(KEY_A)


def test_paging_is_bounded_and_never_follows_redirects(monkeypatch):
    calls = []

    def endless(request):
        calls.append(str(request.url))
        n = len(calls)
        return httpx.Response(200, json={"data": [{"id": f"claude-opus-{n}"}], "has_more": True,
                                         "last_id": f"claude-opus-{n}"})

    _mock(monkeypatch, endless)
    assert len(am.fetch(KEY_A)) == am._MAX_PAGES and len(calls) == am._MAX_PAGES
    assert all(url.startswith(am.MODELS_URL) for url in calls)
    _mock(monkeypatch, lambda request: httpx.Response(302, headers={"location": "https://elsewhere.invalid/"}))
    with pytest.raises(am.CatalogError, match="HTTP 302"):
        am.fetch(KEY_A)


def test_effort_levels_come_from_the_capabilities():
    caps = {"effort": {"supported": True, "low": {"supported": True}, "high": {"supported": True},
                       "max": {"supported": False}}}
    models, _, _ = am.parse_page({"data": [
        {"id": "claude-opus-4-6", "capabilities": caps},
        {"id": "claude-haiku-4-5", "capabilities": {"effort": {"supported": False}}},
        {"id": "claude-opus-5"}]})
    assert [e["effort_levels"] for e in models] == [["low", "high"], [], None]


# ─── Cache: per key ─────────────────────────────────────────────────────────


def test_a_list_belongs_to_the_key_it_was_fetched_with(fake, keys):
    am.refresh_key(KEY_B)
    assert am.cached(am.key_id(KEY_A)) is None  # Fetched for B only.
    assert am.catalog() is None  # A is selected and has no list.
    am.refresh_key(KEY_A)
    assert {e["value"] for e in am.catalog()["models"]} >= {"claude-opus-4-6"}
    # The key is never kept, only its hash.
    assert KEY_A not in json.dumps(am._cache) and KEY_B not in json.dumps(am._cache)


def test_a_key_removed_while_listing_keeps_nothing(fake, keys, monkeypatch):
    real = am.fetch

    def fetch_then_delete(key):
        models = real(key)
        keys["saved"].pop("acc-b")
        return models

    monkeypatch.setattr(am, "fetch", fetch_then_delete)
    with pytest.raises(am.CatalogError, match="removed"):
        am.refresh_key(KEY_B)
    assert am.cached(am.key_id(KEY_B)) is None


def test_lists_of_keys_no_longer_saved_are_dropped_on_write(fake, keys, tmp_path, monkeypatch):
    monkeypatch.setattr(am, "_path", lambda: tmp_path / "claude-api-models.json")
    am.refresh_key(KEY_B)
    keys["saved"].pop("acc-b")
    am.refresh_key(KEY_A)
    saved = json.loads((tmp_path / "claude-api-models.json").read_text())
    assert list(saved) == [am.key_id(KEY_A)]
    assert KEY_A not in (tmp_path / "claude-api-models.json").read_text()


def test_switching_keys_during_a_refresh_lists_under_the_key_used(fake, keys, monkeypatch):
    real = am.fetch

    def fetch_then_switch(key):
        models = real(key)
        keys["active"] = "acc-a"  # The owner selected A while B was being listed.
        return models

    keys["active"] = "acc-b"
    monkeypatch.setattr(am, "fetch", fetch_then_switch)
    am.refresh_key(KEY_B)
    assert am.catalog() is None  # A has no list; B's never stands in.
    assert am.cached(am.key_id(KEY_B)) is not None


def test_settings_payload_never_includes_the_key_or_its_hash(fake, keys, monkeypatch):
    from app.core import claude_accounts

    monkeypatch.setattr(claude_accounts, "api_key_for", lambda i: (keys["saved"][i], i == keys["active"]))
    payload = am.refresh_account("acc-b")
    text = json.dumps(payload)
    assert KEY_B not in text and am.key_id(KEY_B) not in text
    assert payload["active"] is False and payload["catalog"]["models"][-1]["value"] == "claude-opus-4-1"
    assert [a["runs"] for a in payload["aliases"]] == [m.API_MODEL_IDS[v] for v in m.model_values()]
    assert am.account_payload("acc-a")["catalog"] is None


# ─── Pickers ────────────────────────────────────────────────────────────────


def test_pickers_offer_checked_versions_the_selected_key_lists(fake, keys):
    am.refresh_key(KEY_A)
    am.refresh_key(KEY_B)
    keys["enabled"] = ["claude-opus-4-6", "claude-opus-4-1"]
    values = [e["value"] for e in m.registry_payload()["models"]]
    assert values == ["haiku", "sonnet", "opus", "fable", "claude-opus-4-6"]
    offered = m.registry_payload()["models"][-1]
    assert offered["label"] == "Claude Opus 4.6" and offered["family"] == "opus"
    # Selecting B offers B's checked version, never A's.
    keys["active"] = "acc-b"
    assert [e["value"] for e in m.registry_payload()["models"]][4:] == ["claude-opus-4-1"]
    assert m.get_context_window("claude-opus-4-1") == 200_000
    # The CLI list's field stays the subscription's.
    assert m.registry_payload()["versioned_models"] == []


@pytest.mark.parametrize("mode", ["subscription", "cloud", "off"])
def test_other_modes_offer_no_api_versions(fake, keys, mode):
    am.refresh_key(KEY_A)
    keys["enabled"] = ["claude-opus-4-6"]
    keys["mode"] = mode
    assert [e["value"] for e in m.registry_payload()["models"]] == ["haiku", "sonnet", "opus", "fable"]


def test_listed_effort_levels_reach_the_picker(keys):
    snap = am.store(am.key_id(KEY_A), [{"value": "claude-opus-4-6", "label": "Opus 4.6",
                                        "effort_levels": ["low", "high"]}], [am.key_id(KEY_A)])
    assert snap is not None
    assert effort_capabilities("claude-opus-4-6", route="delegated")["effort_levels"] == ["low", "high"]


@pytest.mark.parametrize("listed", ["listed and checked", "unchecked", "not listed", "other key", "no list"])
def test_a_full_id_runs_as_itself_in_api_key_mode(fake, keys, listed):
    """A picked version is never folded onto an alias, whatever the list says."""
    from app.core import model_override
    from app.core.agent_provider_models import leg_model_id
    from app.core.task_dispatch import _native_task_model

    if listed != "no list":
        am.refresh_key(KEY_B if listed == "other key" else KEY_A)
    if listed == "listed and checked":
        keys["enabled"] = ["claude-opus-4-6"]
    for value in ("claude-opus-4-6", "claude-opus-4-1", "claude-sonnet-4-6"):
        assert m.native_choice(value) == value
        assert leg_model_id(None, value) == value
        assert _native_task_model(value) == value
        assert model_override.app_id(None, value) == value
        assert m.normalize_picked(value) == value
    payload = m.registry_payload()
    assert not [k for k in payload["legacy_map"] if not k.endswith("[1m]")]
    # Retired [1m] forms still migrate to today's alias.
    assert m.normalize_picked("claude-opus-4-6[1m]") == "opus"
    assert payload["legacy_map"]["claude-opus-4-6[1m]"] == "opus"
    # Cloud folds exactly as on main.
    keys["mode"] = "cloud"
    assert m.normalize_picked("claude-opus-4-6") == "opus"
    assert leg_model_id(None, "claude-opus-4-6") == "opus"


# ─── Choosing ───────────────────────────────────────────────────────────────


def test_checking_one_id_keeps_the_others(monkeypatch):
    from app.core import claude_cli_status
    from app.db import settings as db_settings

    saved = {am.ENABLED_SETTING: json.dumps(["claude-only-elsewhere"])}
    monkeypatch.setattr(db_settings, "get_system_setting", lambda k: saved.get(k))
    monkeypatch.setattr(db_settings, "set_system_setting", lambda k, v: saved.update({k: v}))
    monkeypatch.setattr(claude_cli_status, "announce_account_change", lambda: None)
    assert am.set_model_enabled("claude-opus-4-6", True) == ["claude-only-elsewhere", "claude-opus-4-6"]
    assert am.set_model_enabled("claude-opus-4-6", False) == ["claude-only-elsewhere"]
    for bad in ("opus", "gpt-5", "claude-opus-4-6[1m]", ""):
        with pytest.raises(ValueError):
            am.set_model_enabled(bad, True)


# ─── Endpoint and dev seam ──────────────────────────────────────────────────


@pytest.mark.parametrize("url,disposable,expected", [
    ("http://127.0.0.1:9", True, "http://127.0.0.1:9/anthropic/v1/models"),
    ("http://127.0.0.1:9", False, None),
    ("http://10.0.0.5:9", True, None),
    ("https://127.0.0.1:9", True, None),
    ("http://127.0.0.1:9/v1", True, None),
    ("http://user:pw@127.0.0.1:9", True, None),
    ("", True, None),
])
def test_only_a_throwaway_instance_may_use_the_fake_catalog(monkeypatch, url, disposable, expected):
    from unittest.mock import patch

    monkeypatch.setenv("SHUFFLE_DEV_FAKE_AI_URL", url)
    with patch("app.preview_run.disposable_preview_data", return_value=disposable):
        assert am.fake_models_url() == expected
        assert am.models_url() == (expected or am.MODELS_URL)


def test_a_throwaway_preview_may_list_only_through_the_fake_and_check_versions():
    from unittest.mock import MagicMock, patch

    from app.api.auth import _preview_may_change_dev_data

    def request(method, path):
        req = MagicMock()
        req.method, req.url.path = method, path
        return req

    refresh = request("POST", "/api/v1/models/claude/api-keys/acc-a/refresh")
    choose = request("PUT", "/api/v1/models/claude/api-enabled")
    activate = request("POST", "/api/v1/settings/claude-auth/accounts/api_key/acc-a/activate")
    mode = request("PUT", "/api/v1/settings/claude-auth")
    for disposable in (True, False):
        with patch("app.preview_run.disposable_preview_data", return_value=disposable):
            with patch.object(am, "fake_models_url", return_value="http://127.0.0.1:9" + am.FAKE_PATH):
                assert _preview_may_change_dev_data(refresh) == disposable
                assert _preview_may_change_dev_data(choose) == disposable
                assert _preview_may_change_dev_data(activate) is False
                assert _preview_may_change_dev_data(mode) is False
            with patch.object(am, "fake_models_url", return_value=None):
                # Never sends a saved key to Anthropic on a preview's say-so.
                assert _preview_may_change_dev_data(refresh) is False
                assert _preview_may_change_dev_data(choose) == disposable
