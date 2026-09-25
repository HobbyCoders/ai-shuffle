"""
Unit tests for MCP profile config tools (_create_profile_tools).

Covers listProfiles, getProfile, createProfile, updateProfile, and deleteProfile.
The database layer is mocked at the function level (app.db.database.*) so no
real DB connection is needed. Mirrors the style of test_config_subagents.py.
"""

import json
import pytest
from unittest.mock import patch

from app.mcp.config_profiles import _create_profile_tools


# =============================================================================
# Helpers
# =============================================================================

def _get_tool(tools: list, name: str):
    """Return the SdkMcpTool with the given name."""
    for t in tools:
        if t.name == name:
            return t
    raise KeyError(f"Tool '{name}' not found in tool list")


def _decode(result: dict) -> dict:
    """Decode the MCP tool result envelope."""
    return json.loads(result["content"][0]["text"])


def _make_profile(**kwargs) -> dict:
    """Return a minimal profile row dict, overridable via kwargs."""
    defaults = {
        "id": "test-profile",
        "name": "Test Profile",
        "description": None,
        "is_builtin": False,
        "config": {},
        "mcp_tools": [],
        "created_at": "2026-01-01T00:00:00+00:00",
        "updated_at": "2026-01-01T00:00:00+00:00",
    }
    defaults.update(kwargs)
    return defaults


# =============================================================================
# Fixtures
# =============================================================================

@pytest.fixture
def admin_tools():
    """Tool list created with is_admin=True."""
    return _create_profile_tools(is_admin=True)


@pytest.fixture
def non_admin_tools():
    """Tool list created with is_admin=False."""
    return _create_profile_tools(is_admin=False)


# =============================================================================
# listProfiles
# =============================================================================

class TestListProfiles:
    @pytest.mark.asyncio
    async def test_list_profiles_returns_seeded_profiles(self, admin_tools):
        profile = _make_profile(
            id="my-list-profile",
            name="My List Profile",
            config={"model": "claude-opus-4-20250514"},
        )
        tool = _get_tool(admin_tools, "listProfiles")
        with patch("app.db.database.get_all_profiles", return_value=[profile]):
            result = await tool.handler({})
        body = _decode(result)
        ids = [p["id"] for p in body["profiles"]]
        assert "my-list-profile" in ids
        assert body["count"] >= 1


# =============================================================================
# getProfile
# =============================================================================

class TestGetProfile:
    @pytest.mark.asyncio
    async def test_get_profile_not_found_returns_error(self, admin_tools):
        tool = _get_tool(admin_tools, "getProfile")
        with patch("app.db.database.get_profile", return_value=None):
            result = await tool.handler({"profile_id": "does-not-exist-xyz"})
        body = _decode(result)
        assert body["success"] is False
        assert "not found" in body["error"].lower()

    @pytest.mark.asyncio
    async def test_get_profile_returns_full_record(self, admin_tools):
        profile = _make_profile(
            id="full-record-profile",
            name="Full Record",
            description="desc",
            config={"model": "claude-haiku-4-20250514", "tools_preset": "claude_code"},
        )
        tool = _get_tool(admin_tools, "getProfile")
        with patch("app.db.database.get_profile", return_value=profile):
            result = await tool.handler({"profile_id": "full-record-profile"})
        body = _decode(result)
        assert body["id"] == "full-record-profile"
        assert "config" in body
        assert body["config"]["model"] == "claude-haiku-4-20250514"

    @pytest.mark.asyncio
    async def test_get_profile_serializes_datetime_fields(self, admin_tools):
        """Regression: the DB layer returns real datetime objects (psycopg),
        so the result serializer must ISO-normalize them rather than crash with
        'Object of type datetime is not JSON serializable'."""
        from datetime import datetime, timezone

        profile = _make_profile(
            id="dt-profile",
            created_at=datetime(2026, 1, 2, 3, 4, 5, tzinfo=timezone.utc),
            updated_at=datetime(2026, 1, 2, 3, 4, 5, tzinfo=timezone.utc),
        )
        tool = _get_tool(admin_tools, "getProfile")
        with patch("app.db.database.get_profile", return_value=profile):
            result = await tool.handler({"profile_id": "dt-profile"})
        body = _decode(result)
        assert body["id"] == "dt-profile"
        assert body["created_at"].startswith("2026-01-02T03:04:05")


# =============================================================================
# createProfile
# =============================================================================

class TestCreateProfile:
    @pytest.mark.asyncio
    async def test_create_profile_minimal_only_name(self, admin_tools):
        created = _make_profile(id="simple-test-profile", name="Simple Test Profile")
        tool = _get_tool(admin_tools, "createProfile")
        with patch("app.db.database.get_profile", return_value=None), \
             patch("app.db.database.create_profile", return_value=created):
            result = await tool.handler({"name": "Simple Test Profile"})
        body = _decode(result)
        assert body["success"] is True
        # ID should be kebab-case derived from name
        assert body["profile_id"] == "simple-test-profile"

    @pytest.mark.asyncio
    async def test_create_profile_with_system_prompt_content_stores_custom(self, admin_tools):
        tool = _get_tool(admin_tools, "createProfile")
        with patch("app.db.database.get_profile", return_value=None), \
             patch("app.db.database.create_profile") as mock_create:
            mock_create.return_value = _make_profile(
                id="custom-prompt-profile",
                name="Custom Prompt Profile",
            )
            result = await tool.handler({
                "name": "Custom Prompt Profile",
                "system_prompt_content": "You are a code reviewer.",
            })
        body = _decode(result)
        assert body["success"] is True
        # Verify create_profile was called with the correct config
        _, kwargs = mock_create.call_args
        sp = kwargs["config"]["system_prompt"]
        assert sp["type"] == "custom"
        assert sp["content"] == "You are a code reviewer."

    @pytest.mark.asyncio
    async def test_create_profile_with_system_prompt_append_stores_preset(self, admin_tools):
        tool = _get_tool(admin_tools, "createProfile")
        with patch("app.db.database.get_profile", return_value=None), \
             patch("app.db.database.create_profile") as mock_create:
            mock_create.return_value = _make_profile(
                id="append-prompt-profile",
                name="Append Prompt Profile",
            )
            result = await tool.handler({
                "name": "Append Prompt Profile",
                "system_prompt_append": "Always use Python 3.",
            })
        body = _decode(result)
        assert body["success"] is True
        _, kwargs = mock_create.call_args
        sp = kwargs["config"]["system_prompt"]
        assert sp["type"] == "preset"
        assert sp["append"] == "Always use Python 3."

    @pytest.mark.asyncio
    async def test_create_profile_duplicate_id_rejected(self, admin_tools):
        existing = _make_profile(id="dup-profile", name="Dup Profile")
        tool = _get_tool(admin_tools, "createProfile")
        # First call finds no conflict, second call finds existing profile
        with patch("app.db.database.get_profile", return_value=existing):
            result = await tool.handler({"name": "Dup Profile"})
        body = _decode(result)
        assert body["success"] is False
        assert "already exists" in body["error"]

    @pytest.mark.asyncio
    async def test_create_profile_requires_admin(self, non_admin_tools):
        tool = _get_tool(non_admin_tools, "createProfile")
        result = await tool.handler({"name": "Admin Only"})
        body = _decode(result)
        assert body["success"] is False
        assert "admin" in body["error"].lower()


# =============================================================================
# updateProfile
# =============================================================================

class TestUpdateProfile:
    @pytest.mark.asyncio
    async def test_update_profile_basic_fields(self, admin_tools):
        original = _make_profile(id="update-basic", name="Update Basic", config={})
        updated = _make_profile(
            id="update-basic",
            name="Update Basic",
            config={
                "thinking_mode": "think",
                "tools_preset": "claude_code",
                "max_turns": 5,
                "app_config_tools": True,
            },
        )
        tool = _get_tool(admin_tools, "updateProfile")
        with patch("app.db.database.get_profile", return_value=original),              patch("app.db.database.update_profile", return_value=updated) as mock_update:
            result = await tool.handler({
                "profile_id": "update-basic",
                "thinking_mode": "think",
                "tools_preset": "claude_code",
                "max_turns": 5,
                "app_config_tools": True,
            })
        body = _decode(result)
        assert body["success"] is True
        _, kwargs = mock_update.call_args
        cfg = kwargs["config"]
        assert cfg["thinking_mode"] == "think"
        assert cfg["tools_preset"] == "claude_code"
        assert cfg["max_turns"] == 5
        assert cfg["app_config_tools"] is True

    @pytest.mark.asyncio
    async def test_update_profile_allowed_tools(self, admin_tools):
        original = _make_profile(id="update-allowed", name="Update Allowed", config={})
        tool = _get_tool(admin_tools, "updateProfile")

        # Set a whitelist
        with patch("app.db.database.get_profile", return_value=original), \
             patch("app.db.database.update_profile") as mock_update:
            mock_update.return_value = _make_profile(
                id="update-allowed",
                name="Update Allowed",
                config={"allowed_tools": ["Read", "Grep"]},
            )
            result = await tool.handler({
                "profile_id": "update-allowed",
                "allowed_tools": ["Read", "Grep"],
            })
        body = _decode(result)
        assert body["success"] is True
        _, kwargs = mock_update.call_args
        assert kwargs["config"]["allowed_tools"] == ["Read", "Grep"]

        # Clear the whitelist with empty list
        with patch("app.db.database.get_profile", return_value=original), \
             patch("app.db.database.update_profile") as mock_update2:
            mock_update2.return_value = _make_profile(
                id="update-allowed",
                name="Update Allowed",
                config={"allowed_tools": []},
            )
            result2 = await tool.handler({
                "profile_id": "update-allowed",
                "allowed_tools": [],
            })
        body2 = _decode(result2)
        assert body2["success"] is True
        _, kwargs2 = mock_update2.call_args
        assert kwargs2["config"]["allowed_tools"] == []

    @pytest.mark.asyncio
    async def test_update_profile_disallowed_tools(self, admin_tools):
        original = _make_profile(id="update-disallowed", name="Update Disallowed", config={})
        tool = _get_tool(admin_tools, "updateProfile")
        with patch("app.db.database.get_profile", return_value=original), \
             patch("app.db.database.update_profile") as mock_update:
            mock_update.return_value = _make_profile(
                id="update-disallowed",
                name="Update Disallowed",
                config={"disallowed_tools": ["Bash"]},
            )
            result = await tool.handler({
                "profile_id": "update-disallowed",
                "disallowed_tools": ["Bash"],
            })
        body = _decode(result)
        assert body["success"] is True
        _, kwargs = mock_update.call_args
        assert kwargs["config"]["disallowed_tools"] == ["Bash"]

    @pytest.mark.asyncio
    async def test_update_profile_ignores_run_binding_fields(self, admin_tools):
        """Model, effort and permission mode belong to a session, not an agent
        (v79) — the tool must not write them into an agent's config even if a
        model names them."""
        original = _make_profile(id="update-binding", name="Update Binding", config={})
        tool = _get_tool(admin_tools, "updateProfile")
        with patch("app.db.database.get_profile", return_value=original),              patch("app.db.database.update_profile") as mock_update:
            mock_update.return_value = _make_profile(
                id="update-binding",
                name="Update Binding",
                config={"max_turns": 3},
            )
            result = await tool.handler({
                "profile_id": "update-binding",
                "max_turns": 3,
                "model": "claude-haiku-4-20250514",
                "permission_mode": "auto",
                "effort": "high",
            })
        body = _decode(result)
        assert body["success"] is True
        cfg = mock_update.call_args[1]["config"]
        assert cfg["max_turns"] == 3
        assert "model" not in cfg
        assert "permission_mode" not in cfg
        assert "effort" not in cfg

    @pytest.mark.asyncio
    async def test_update_profile_system_prompt_content_switches_to_custom(self, admin_tools):
        original = _make_profile(
            id="sp-content-switch",
            name="SP Content Switch",
            config={"system_prompt": {"type": "preset", "preset": "claude_code"}},
        )
        tool = _get_tool(admin_tools, "updateProfile")
        with patch("app.db.database.get_profile", return_value=original), \
             patch("app.db.database.update_profile") as mock_update:
            mock_update.return_value = _make_profile(
                id="sp-content-switch",
                name="SP Content Switch",
                config={"system_prompt": {"type": "custom", "content": "x", "inject_env_details": True}},
            )
            result = await tool.handler({
                "profile_id": "sp-content-switch",
                "system_prompt_content": "x",
            })
        body = _decode(result)
        assert body["success"] is True
        _, kwargs = mock_update.call_args
        sp = kwargs["config"]["system_prompt"]
        assert sp["type"] == "custom"
        assert sp["content"] == "x"

    @pytest.mark.asyncio
    async def test_update_profile_system_prompt_append_sets_preset_append(self, admin_tools):
        original = _make_profile(
            id="sp-append-set",
            name="SP Append Set",
            config={"system_prompt": {"type": "preset", "preset": "claude_code"}},
        )
        tool = _get_tool(admin_tools, "updateProfile")
        with patch("app.db.database.get_profile", return_value=original), \
             patch("app.db.database.update_profile") as mock_update:
            mock_update.return_value = _make_profile(
                id="sp-append-set",
                name="SP Append Set",
                config={"system_prompt": {"type": "preset", "preset": "claude_code", "append": "hello"}},
            )
            result = await tool.handler({
                "profile_id": "sp-append-set",
                "system_prompt_append": "hello",
            })
        body = _decode(result)
        assert body["success"] is True
        _, kwargs = mock_update.call_args
        sp = kwargs["config"]["system_prompt"]
        assert sp["type"] == "preset"
        assert sp["preset"] == "claude_code"
        assert sp["append"] == "hello"

    @pytest.mark.asyncio
    async def test_update_profile_no_system_prompt_args_preserves_existing(self, admin_tools):
        original = _make_profile(
            id="sp-preserve",
            name="SP Preserve",
            config={"system_prompt": {"type": "custom", "content": "abc"}},
        )
        tool = _get_tool(admin_tools, "updateProfile")
        with patch("app.db.database.get_profile", return_value=original), \
             patch("app.db.database.update_profile") as mock_update:
            mock_update.return_value = _make_profile(
                id="sp-preserve",
                name="SP Preserve",
                config={
                    "model": "claude-haiku-4-20250514",
                    "system_prompt": {"type": "custom", "content": "abc"},
                },
            )
            result = await tool.handler({
                "profile_id": "sp-preserve",
                "max_turns": 4,
            })
        body = _decode(result)
        assert body["success"] is True
        # Inspect the config kwarg passed to update_profile — system_prompt must be unchanged
        _, kwargs = mock_update.call_args
        sp = kwargs["config"]["system_prompt"]
        assert sp["type"] == "custom"
        assert sp["content"] == "abc"

    @pytest.mark.asyncio
    async def test_update_profile_not_found_returns_error(self, admin_tools):
        tool = _get_tool(admin_tools, "updateProfile")
        with patch("app.db.database.get_profile", return_value=None):
            result = await tool.handler({
                "profile_id": "no-such-profile-xyz",
                "model": "claude-haiku-4-20250514",
            })
        body = _decode(result)
        assert body["success"] is False
        assert "not found" in body["error"].lower()

    @pytest.mark.asyncio
    async def test_update_profile_no_fields_returns_error(self, admin_tools):
        original = _make_profile(id="no-fields-update", name="No Fields Update", config={})
        tool = _get_tool(admin_tools, "updateProfile")
        with patch("app.db.database.get_profile", return_value=original):
            result = await tool.handler({"profile_id": "no-fields-update"})
        body = _decode(result)
        assert body["success"] is False
        assert "no fields" in body["error"].lower()

    @pytest.mark.asyncio
    async def test_update_profile_requires_admin(self, non_admin_tools):
        tool = _get_tool(non_admin_tools, "updateProfile")
        result = await tool.handler({
            "profile_id": "admin-update-check",
            "model": "claude-haiku-4-20250514",
        })
        body = _decode(result)
        assert body["success"] is False
        assert "admin" in body["error"].lower()

    @pytest.mark.asyncio
    async def test_update_profile_allows_builtin(self, admin_tools):
        """Built-in profiles remain editable via MCP (mirrors REST behavior)."""
        builtin = _make_profile(
            id="claude-code",
            name="Claude Code",
            is_builtin=True,
            config={"max_turns": 2},
        )
        updated_row = _make_profile(
            id="claude-code",
            name="Claude Code",
            is_builtin=True,
            config={"max_turns": 9},
        )
        tool = _get_tool(admin_tools, "updateProfile")
        with patch("app.db.database.get_profile", return_value=builtin), \
             patch("app.db.database.update_profile", return_value=updated_row) as mock_update:
            result = await tool.handler({
                "profile_id": "claude-code",
                "max_turns": 9,
            })
        body = _decode(result)
        assert body["success"] is True
        assert "max_turns" in body["updated_fields"]
        # Confirm allow_builtin=True was passed through to the DB layer
        _, kwargs = mock_update.call_args
        assert kwargs.get("allow_builtin") is True


# =============================================================================
# deleteProfile
# =============================================================================

class TestDeleteProfile:
    @pytest.mark.asyncio
    async def test_delete_profile_requires_confirm(self, admin_tools):
        tool = _get_tool(admin_tools, "deleteProfile")
        # Without confirm key at all
        result = await tool.handler({"profile_id": "del-no-confirm"})
        body = _decode(result)
        assert body["success"] is False
        assert "confirm" in body["error"].lower()

        # With confirm=False explicitly
        result2 = await tool.handler({
            "profile_id": "del-no-confirm",
            "confirm": False,
        })
        body2 = _decode(result2)
        assert body2["success"] is False
        assert "confirm" in body2["error"].lower()

    @pytest.mark.asyncio
    async def test_delete_profile_not_found(self, admin_tools):
        tool = _get_tool(admin_tools, "deleteProfile")
        with patch("app.db.database.get_profile", return_value=None):
            result = await tool.handler({
                "profile_id": "ghost-profile-xyz",
                "confirm": True,
            })
        body = _decode(result)
        assert body["success"] is False
        assert "not found" in body["error"].lower()

    @pytest.mark.asyncio
    async def test_delete_profile_rejects_builtin(self, admin_tools):
        builtin = _make_profile(id="builtin-del", name="Builtin Del", is_builtin=True)
        tool = _get_tool(admin_tools, "deleteProfile")
        with patch("app.db.database.get_profile", return_value=builtin):
            result = await tool.handler({
                "profile_id": "builtin-del",
                "confirm": True,
            })
        body = _decode(result)
        assert body["success"] is False
        assert "built-in" in body["error"].lower()

    @pytest.mark.asyncio
    async def test_delete_profile_success(self, admin_tools):
        profile = _make_profile(
            id="del-success",
            name="Del Success",
            config={"model": "claude-haiku-4-20250514"},
        )
        tool = _get_tool(admin_tools, "deleteProfile")
        with patch("app.db.database.get_profile", return_value=profile), \
             patch("app.db.database.get_automations_for_profile", return_value=[]), \
             patch("app.db.database.delete_profile", return_value=True):
            result = await tool.handler({
                "profile_id": "del-success",
                "confirm": True,
            })
        body = _decode(result)
        assert body["success"] is True
        assert body["profile_id"] == "del-success"

    @pytest.mark.asyncio
    async def test_delete_profile_blocked_by_automation(self, admin_tools):
        profile = _make_profile(id="del-blocked", name="Del Blocked")
        automations = [{"name": "Nightly Sync"}, {"name": "Weekly Report"}]
        tool = _get_tool(admin_tools, "deleteProfile")
        with patch("app.db.database.get_profile", return_value=profile), \
             patch("app.db.database.get_automations_for_profile", return_value=automations), \
             patch("app.db.database.delete_profile") as mock_delete:
            result = await tool.handler({"profile_id": "del-blocked", "confirm": True})
        body = _decode(result)
        assert body["success"] is False
        assert "2 automation(s)" in body["error"]
        assert "Nightly Sync" in body["error"]
        assert "Weekly Report" in body["error"]
        mock_delete.assert_not_called()

    @pytest.mark.asyncio
    async def test_delete_profile_requires_admin(self, non_admin_tools):
        tool = _get_tool(non_admin_tools, "deleteProfile")
        result = await tool.handler({
            "profile_id": "admin-del-check",
            "confirm": True,
        })
        body = _decode(result)
        assert body["success"] is False
        assert "admin" in body["error"].lower()
