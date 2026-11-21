"""The agent editor's "Vault" group (mcp__chat-access__*).

``query_options`` attaches the vault server only when one of its tool
names is permitted by the agent's lists. An agent in "Allow only selected"
mode therefore needs those names in ``allowed_tools`` to get Vault —
which it could not get while the picker had no group to tick them in.
"""

from unittest.mock import patch

import pytest

from app.core import tool_catalog as tc
from app.core.session_modes import CHAT_MODE_RESOURCE_TOOLS
from app.mcp.vault_sdk import TOOL_NAMES


def test_vault_rows_cover_every_vault_access_tool_with_real_descriptions():
    rows = tc.vault_tools()
    names = [r["name"] for r in rows]
    assert names == list(TOOL_NAMES)
    assert set(names) == set(CHAT_MODE_RESOURCE_TOOLS)
    assert names[0] == "mcp__chat-access__RequestChatAccess"
    # Taken from the server's own @tool definitions, not hand-copied.
    assert all(r["description"] for r in rows)
    request = rows[0]["description"]
    assert request == tc.first_sentence(request)
    assert "Vault card" in request


def test_vault_rows_survive_a_server_build_failure(monkeypatch):
    import app.mcp.vault_sdk as sdk

    def boom(_session_id):
        raise RuntimeError("cannot build")

    monkeypatch.setattr(sdk, "create_vault_server", boom)
    rows = tc.vault_tools()
    assert [r["name"] for r in rows] == list(TOOL_NAMES)
    assert all(r["description"] is None for r in rows)


@pytest.mark.asyncio
async def test_surface_places_vault_after_app_tools(monkeypatch):
    monkeypatch.setattr(tc, "harness_tools", lambda: [])
    monkeypatch.setattr(tc, "app_tools", lambda: [])
    monkeypatch.setattr(tc, "ai_tools", lambda: ([], False))
    monkeypatch.setattr(tc, "computer_use_tools", lambda: ([], False, None))
    monkeypatch.setattr(tc, "app_config_tools", lambda: [])
    monkeypatch.setattr(tc, "_configured_mcp_servers", lambda errors: (None, {}))

    surface = await tc.build_tool_surface()
    ids = [g["id"] for g in surface["groups"]]
    assert ids.index("vault") == ids.index("app") + 1
    group = next(g for g in surface["groups"] if g["id"] == "vault")
    assert group["kind"] == "vault"
    assert group["label"] == "Vault"
    assert group["server"] == "chat-access"
    assert group["available"] is True and group["admin_only"] is False
    assert group["note"] == tc.VAULT_NOTE
    assert {t["name"] for t in group["tools"]} == set(CHAT_MODE_RESOURCE_TOOLS)
    assert not any(e["group"] == "vault" for e in surface["errors"])


def _build(tmp_path, config):
    import app.core.query_options as qo

    profile = {"id": "p", "config": {
        "model": "sonnet", "tools_preset": "claude_code", "cwd": str(tmp_path), **config,
    }}
    session = {"id": "s", "mode": "code", "project_id": "project"}
    with patch.object(qo.database, "get_session", return_value=session), \
            patch.object(qo, "get_security_instructions", return_value=""):
        options, _ = qo.build_options_from_profile(
            profile=profile, session_id="s", overrides={"cwd": str(tmp_path)},
        )
    return options


def test_allow_mode_without_vault_names_gets_no_vault_access(tmp_path):
    """The reported defect: an allow-list agent silently has no RequestChatAccess."""
    options = _build(tmp_path, {"tool_selection_mode": "allow", "allowed_tools": ["Read", "Grep"]})
    assert "chat-access" not in options.mcp_servers
    assert not set(CHAT_MODE_RESOURCE_TOOLS).intersection(options.allowed_tools)


def test_allow_mode_with_the_picked_vault_names_attaches_vault_access(tmp_path):
    """What ticking the new group saves: the exact mcp__chat-access__* names."""
    options = _build(tmp_path, {
        "tool_selection_mode": "allow",
        "allowed_tools": ["Read", *CHAT_MODE_RESOURCE_TOOLS],
    })
    assert "chat-access" in options.mcp_servers
    assert set(CHAT_MODE_RESOURCE_TOOLS) <= set(options.allowed_tools)


def test_allow_mode_with_only_some_vault_names_attaches_just_those(tmp_path):
    picked = ["mcp__chat-access__RequestChatAccess", "mcp__chat-access__ChatResources"]
    options = _build(tmp_path, {"tool_selection_mode": "allow", "allowed_tools": ["Read", *picked]})
    assert "chat-access" in options.mcp_servers
    granted = set(CHAT_MODE_RESOURCE_TOOLS).intersection(options.allowed_tools)
    assert granted == set(picked)


def test_disallowing_the_group_removes_vault(tmp_path):
    options = _build(tmp_path, {
        "tool_selection_mode": "disallow",
        "disallowed_tools": list(CHAT_MODE_RESOURCE_TOOLS),
    })
    assert "chat-access" not in options.mcp_servers
