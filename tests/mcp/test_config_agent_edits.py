"""Fine-grained agent edits in the app-config MCP tools.

Covers prompt_edits, add/remove and the agent settings exposed on
createProfile/updateProfile, and provider_models on the subagent tools. The
tool round-trips run against the isolated DB shim (``mock_db``) so the stored
config is the real read-modify-write result, not a mocked call.
"""

import pytest

from app.mcp.config_agent_edits import apply_list_ops, apply_map_ops, apply_prompt_edits
from app.mcp.config_profiles import _create_profile_tools
from app.mcp.config_subagents import _create_subagent_tools
from tests.mcp.tool_helpers import get_tool, tool_result_json


async def _call(tools, name, args):
    return tool_result_json(await get_tool(tools, name).handler(args))


@pytest.fixture
def profile_tools():
    return _create_profile_tools(is_admin=True)


@pytest.fixture
def subagent_tools():
    return _create_subagent_tools(is_admin=True)


def _stored(profile_id):
    from app.db import database

    return database.get_profile(profile_id)["config"]


# =============================================================================
# Pure helpers
# =============================================================================

class TestPromptEdits:
    def test_replace_once(self):
        text, error, excerpts = apply_prompt_edits("Be brief. Use tests.", [{"old": "brief", "new": "thorough"}])
        assert error is None
        assert text == "Be thorough. Use tests."
        assert "thorough" in excerpts[0]

    def test_not_found_and_multiple_matches_refused(self):
        text, error, _ = apply_prompt_edits("abc", [{"old": "zzz", "new": "y"}])
        assert text is None and "not found" in error
        text, error, _ = apply_prompt_edits("a a a", [{"old": "a", "new": "b"}])
        assert text is None and "matches 3 times" in error

    def test_append_and_order(self):
        text, error, _ = apply_prompt_edits(
            "One.\n", [{"append": "## Two"}, {"old": "Two", "new": "2"}])
        assert error is None
        assert text == "One.\n\n## 2"
        assert apply_prompt_edits("", [{"append": "First"}])[0] == "First"

    def test_malformed_items_refused(self):
        for edits in ([], [{"old": "a", "new": "b", "append": "c"}], [{"old": "", "new": "x"}],
                      [{"append": "  "}], ["text"], [{"old": "a"}]):
            assert apply_prompt_edits("a", edits)[0] is None, edits

    def test_one_bad_item_applies_nothing(self):
        text, error, _ = apply_prompt_edits("a b", [{"old": "a", "new": "x"}, {"old": "q", "new": "y"}])
        assert text is None and "prompt_edits[2]" in error


class TestListAndMapOps:
    def test_add_dedupes_and_keeps_order(self):
        value, error, missing = apply_list_ops(["Read", "Grep"], ["Grep", "Bash", "Bash"], ["Read", "Nope"], "tools")
        assert error is None
        assert value == ["Grep", "Bash"]
        assert missing == ["Nope"]

    def test_bad_types_refused(self):
        assert apply_list_ops([], "Read", None, "tools")[1]
        assert apply_map_ops({}, {"k": 1}, None, "provider_models")[1]
        assert apply_map_ops({}, {"": "x"}, None, "provider_models")[1]

    def test_map_set_and_remove_single_keys(self):
        value, error, missing = apply_map_ops(
            {"__native__": "haiku", "p1": "gpt"}, {"__native__": "sonnet"}, ["p1", "p9"], "provider_models")
        assert error is None
        assert value == {"__native__": "sonnet"}
        assert missing == ["p9"]


# =============================================================================
# updateProfile / createProfile (real persistence via the DB shim)
# =============================================================================

class TestProfilePromptEdits:
    @pytest.mark.asyncio
    async def test_patch_custom_prompt(self, mock_db, profile_tools):
        await _call(profile_tools, "createProfile",
                    {"name": "Writer", "system_prompt_content": "Be brief.\n\n## Rules\nNo jargon."})
        body = await _call(profile_tools, "updateProfile", {
            "profile_id": "writer",
            "prompt_edits": [{"old": "No jargon.", "new": "Plain words."}, {"append": "## Tone\nWarm."}],
        })
        assert body["success"] is True, body
        assert body["updated_fields"] == ["system_prompt"]
        expected = "Be brief.\n\n## Rules\nPlain words.\n\n## Tone\nWarm."
        assert body["prompt"]["length"] == len(expected)
        assert _stored("writer")["system_prompt"] == {"type": "custom", "content": expected}

    @pytest.mark.asyncio
    async def test_failed_patch_writes_nothing(self, mock_db, profile_tools):
        await _call(profile_tools, "createProfile", {"name": "Dup", "system_prompt_content": "x and x"})
        before = _stored("dup")
        body = await _call(profile_tools, "updateProfile",
                           {"profile_id": "dup", "name": "Renamed", "prompt_edits": [{"old": "x", "new": "y"}]})
        assert body["success"] is False and "matches 2 times" in body["error"]
        from app.db import database
        assert database.get_profile("dup")["name"] == "Dup"
        assert _stored("dup") == before

    @pytest.mark.asyncio
    async def test_patch_preset_prompt_edits_the_append(self, mock_db, profile_tools):
        await _call(profile_tools, "createProfile", {"name": "Preset", "system_prompt_append": "Use tabs."})
        body = await _call(profile_tools, "updateProfile",
                           {"profile_id": "preset", "prompt_edits": [{"old": "tabs", "new": "spaces"}]})
        assert body["success"] is True, body
        assert _stored("preset")["system_prompt"] == {
            "type": "preset", "preset": "claude_code", "append": "Use spaces."}

    @pytest.mark.asyncio
    async def test_reset_to_preset_and_full_replace(self, mock_db, profile_tools):
        await _call(profile_tools, "createProfile", {"name": "Reset", "system_prompt_content": "Custom."})
        assert (await _call(profile_tools, "updateProfile",
                            {"profile_id": "reset", "reset_system_prompt": True}))["success"]
        assert _stored("reset")["system_prompt"] == {"type": "preset", "preset": "claude_code"}
        assert (await _call(profile_tools, "updateProfile",
                            {"profile_id": "reset", "system_prompt_content": "New."}))["success"]
        assert _stored("reset")["system_prompt"] == {"type": "custom", "content": "New."}

    @pytest.mark.asyncio
    async def test_append_to_missing_prompt_stays_custom(self, mock_db, profile_tools):
        from app.db import database
        database.create_profile("bare", "Bare", None, {})
        body = await _call(profile_tools, "updateProfile",
                           {"profile_id": "bare", "prompt_edits": [{"append": "Hello."}]})
        assert body["success"] is True, body
        assert _stored("bare")["system_prompt"] == {"type": "custom", "content": "Hello."}

    @pytest.mark.asyncio
    async def test_two_prompt_changes_at_once_refused(self, mock_db, profile_tools):
        body = await _call(profile_tools, "updateProfile", {
            "profile_id": "anything", "reset_system_prompt": True,
            "prompt_edits": [{"append": "x"}]})
        assert body["success"] is False and "only one way" in body["error"]


class TestProfileListOps:
    @pytest.mark.asyncio
    async def test_add_remove_allowed_tools(self, mock_db, profile_tools):
        await _call(profile_tools, "createProfile", {"name": "Lists", "allowed_tools": ["Read", "Grep"]})
        body = await _call(profile_tools, "updateProfile", {
            "profile_id": "lists",
            "add": {"allowed_tools": ["Bash", "Read"], "enabled_agents": ["helper"]},
            "remove": {"allowed_tools": ["Grep", "Missing"]},
        })
        assert body["success"] is True, body
        assert body["lists"]["allowed_tools"] == ["Read", "Bash"]
        assert body["not_found_for_remove"] == {"allowed_tools": ["Missing"]}
        config = _stored("lists")
        assert config["allowed_tools"] == ["Read", "Bash"]
        assert config["enabled_agents"] == ["helper"]

    @pytest.mark.asyncio
    async def test_removing_last_allowed_tool_refused(self, mock_db, profile_tools):
        await _call(profile_tools, "createProfile",
                    {"name": "Last", "tool_selection_mode": "allow", "allowed_tools": ["Read"]})
        body = await _call(profile_tools, "updateProfile",
                           {"profile_id": "last", "remove": {"allowed_tools": ["Read"]}})
        assert body["success"] is False
        assert _stored("last")["allowed_tools"] == ["Read"]

    @pytest.mark.asyncio
    async def test_removing_last_tool_without_mode_refused(self, mock_db, profile_tools):
        from app.db import database
        database.create_profile("legacy", "Legacy", None, {"allowed_tools": ["Read"]})
        body = await _call(profile_tools, "updateProfile",
                           {"profile_id": "legacy", "remove": {"allowed_tools": ["Read"]}})
        assert body["success"] is False and "no tools" in body["error"]

    @pytest.mark.asyncio
    async def test_unknown_op_field_refused(self, mock_db, profile_tools):
        body = await _call(profile_tools, "updateProfile",
                           {"profile_id": "x", "add": {"env": ["SECRET"]}})
        assert body["success"] is False and "does not support env" in body["error"]

    @pytest.mark.asyncio
    async def test_provider_models_single_key(self, mock_db, profile_tools):
        await _call(profile_tools, "createProfile",
                    {"name": "Models", "provider_models": {"__native__": "haiku", "p1": "gpt", "p2": " "}})
        assert _stored("models")["provider_models"] == {"__native__": "haiku", "p1": "gpt"}
        body = await _call(profile_tools, "updateProfile", {
            "profile_id": "models",
            "add": {"provider_models": {"__native__": "sonnet"}},
            "remove": {"provider_models": ["p1"]},
        })
        assert body["success"] is True, body
        assert _stored("models")["provider_models"] == {"__native__": "sonnet"}


class TestProfileSettings:
    @pytest.mark.asyncio
    async def test_settings_round_trip_through_get_profile(self, mock_db, profile_tools):
        body = await _call(profile_tools, "createProfile", {
            "name": "Settings",
            "agent_invoke_mode": "custom",
            "enabled_agents": ["helper"],
            "enabled_plugins": ["plug@market"],
            "add_dirs": ["/srv/data"],
            "avatar_color": "rose",
        })
        assert body["success"] is True, body
        await _call(profile_tools, "updateProfile", {
            "profile_id": "settings",
            "add": {"enabled_plugins": ["other@market"]},
            "add_dirs": None,
        })
        got = await _call(profile_tools, "getProfile", {"profile_id": "settings"})
        config = got["config"]
        assert config["agent_invoke_mode"] == "custom"
        assert config["enabled_agents"] == ["helper"]
        assert config["enabled_plugins"] == ["plug@market", "other@market"]
        assert "add_dirs" not in config
        assert got["avatar_color"] == "rose"
        await _call(profile_tools, "updateProfile", {"profile_id": "settings", "avatar_color": "blue"})
        assert (await _call(profile_tools, "getProfile", {"profile_id": "settings"}))["avatar_color"] == "blue"
        await _call(profile_tools, "updateProfile", {"profile_id": "settings", "avatar_color": None})
        assert (await _call(profile_tools, "getProfile", {"profile_id": "settings"}))["avatar_color"] is None

    @pytest.mark.asyncio
    @pytest.mark.parametrize("args, phrase", [
        ({"agent_invoke_mode": "some"}, "agent_invoke_mode"),
        ({"avatar_color": "purple"}, "avatar_color"),
        ({"enabled_agents": "helper"}, "enabled_agents"),
        ({"provider_models": {"__native__": 3}}, "provider_models"),
    ])
    async def test_invalid_settings_write_nothing(self, mock_db, profile_tools, args, phrase):
        await _call(profile_tools, "createProfile", {"name": "Strict"})
        before = _stored("strict")
        body = await _call(profile_tools, "updateProfile", {"profile_id": "strict", **args})
        assert body["success"] is False and phrase in body["error"], body
        assert _stored("strict") == before
        created = await _call(profile_tools, "createProfile", {"name": "Strict Two", **args})
        assert created["success"] is False
        from app.db import database
        assert database.get_profile("strict-two") is None

    @pytest.mark.parametrize("name", ["createProfile", "updateProfile"])
    def test_retired_and_pinned_settings_not_exposed(self, profile_tools, name):
        props = get_tool(profile_tools, name).input_schema["properties"]
        for key in ("ai_tools", "computer_use", "effort", "model", "provider",
                    "permission_mode", "env", "extra_args", "cwd", "setting_sources"):
            assert key not in props

    @pytest.mark.asyncio
    async def test_env_values_masked_on_read(self, mock_db, profile_tools):
        from app.db import database
        database.create_profile("envy", "Envy", None, {"env": {"API_KEY": "sk-secret-value"}})
        got = await _call(profile_tools, "getProfile", {"profile_id": "envy"})
        assert got["config"]["env"] == {"API_KEY": "••••••••"}
        assert "sk-secret-value" not in str(got)

    @pytest.mark.asyncio
    async def test_assistant_agent_still_refused(self, mock_db, profile_tools):
        from app.db.owner_state import AGENT_ID
        body = await _call(profile_tools, "updateProfile",
                           {"profile_id": AGENT_ID, "prompt_edits": [{"append": "x"}]})
        assert body["success"] is False and "assistant" in body["error"].lower()

    @pytest.mark.asyncio
    async def test_non_admin_refused(self, mock_db):
        tools = _create_profile_tools(is_admin=False)
        body = await _call(tools, "updateProfile", {"profile_id": "x", "add": {"allowed_tools": ["Read"]}})
        assert body["success"] is False and "admin" in body["error"]


# =============================================================================
# updateSubagent / createSubagent
# =============================================================================

class TestSubagentEdits:
    @pytest.mark.asyncio
    async def test_prompt_patch_and_list_ops(self, mock_db, subagent_tools):
        created = await _call(subagent_tools, "createSubagent", {
            "name": "Reviewer", "description": "Reviews", "prompt": "Check tests.",
            "tools": ["Read"], "skills": ["lint"],
            "provider_models": {"__native__": "haiku"},
        })
        assert created["success"] is True, created
        sub_id = created["subagent_id"]
        body = await _call(subagent_tools, "updateSubagent", {
            "subagent_id": sub_id,
            "prompt_edits": [{"old": "tests", "new": "tests and docs"}, {"append": "Be kind."}],
            "add": {"tools": ["Grep", "Read"], "skills": ["format"], "provider_models": {"p1": "gpt"}},
            "remove": {"skills": ["lint"], "provider_models": ["__native__"]},
        })
        assert body["success"] is True, body
        assert set(body["updated_fields"]) == {"prompt", "tools", "skills", "provider_models"}
        got = await _call(subagent_tools, "getSubagent", {"subagent_id": sub_id})
        assert got["prompt"] == "Check tests and docs.\n\nBe kind."
        assert got["tools"] == ["Read", "Grep"]
        assert got["skills"] == ["format"]
        assert got["provider_models"] == {"p1": "gpt"}

    @pytest.mark.asyncio
    async def test_failed_patch_writes_nothing(self, mock_db, subagent_tools):
        created = await _call(subagent_tools, "createSubagent",
                              {"name": "Same", "description": "d", "prompt": "Old."})
        sub_id = created["subagent_id"]
        body = await _call(subagent_tools, "updateSubagent", {
            "subagent_id": sub_id, "name": "Changed", "prompt_edits": [{"old": "Gone", "new": "x"}]})
        assert body["success"] is False and "not found" in body["error"]
        got = await _call(subagent_tools, "getSubagent", {"subagent_id": sub_id})
        assert got["name"] == "Same" and got["prompt"] == "Old."

    @pytest.mark.asyncio
    async def test_removing_every_tool_refused(self, mock_db, subagent_tools):
        created = await _call(subagent_tools, "createSubagent",
                              {"name": "Solo", "description": "d", "prompt": "p", "tools": ["Read"]})
        body = await _call(subagent_tools, "updateSubagent",
                           {"subagent_id": created["subagent_id"], "remove": {"tools": ["Read"]}})
        assert body["success"] is False and "tools: null" in body["error"]

    @pytest.mark.asyncio
    async def test_provider_models_replace_clear_and_validate(self, mock_db, subagent_tools):
        created = await _call(subagent_tools, "createSubagent",
                              {"name": "Mapped", "description": "d", "prompt": "p"})
        sub_id = created["subagent_id"]
        await _call(subagent_tools, "updateSubagent",
                    {"subagent_id": sub_id, "provider_models": {"__native__": "opus"}})
        got = await _call(subagent_tools, "getSubagent", {"subagent_id": sub_id})
        assert got["provider_models"] == {"__native__": "opus"}
        await _call(subagent_tools, "updateSubagent", {"subagent_id": sub_id, "provider_models": None})
        got = await _call(subagent_tools, "getSubagent", {"subagent_id": sub_id})
        assert not got["provider_models"]
        bad = await _call(subagent_tools, "updateSubagent",
                          {"subagent_id": sub_id, "provider_models": {"__native__": 5}})
        assert bad["success"] is False and "provider_models" in bad["error"]

    @pytest.mark.asyncio
    async def test_prompt_and_prompt_edits_together_refused(self, mock_db, subagent_tools):
        body = await _call(subagent_tools, "updateSubagent", {
            "subagent_id": "x", "prompt": "new", "prompt_edits": [{"append": "y"}]})
        assert body["success"] is False and "not both" in body["error"]

    @pytest.mark.asyncio
    async def test_assistant_agent_still_refused(self, mock_db, subagent_tools):
        from app.db.owner_state import AGENT_ID
        body = await _call(subagent_tools, "updateSubagent",
                           {"subagent_id": AGENT_ID, "add": {"tools": ["Read"]}})
        assert body["success"] is False and "assistant" in body["error"].lower()


# =============================================================================
# The subagent view of any agent row: stored prompt and tool mode
# =============================================================================

_PRESET = {"type": "preset", "preset": "claude_code", "append": "Keep these rules."}


def _agent(agent_id, config):
    """An agent row saved in the profile (Agents page / REST) shape."""
    from app.db import database
    database.create_profile(agent_id, agent_id.title(), None, config)


async def _rest_put(agent_id, **fields):
    """PUT /agents/{profile}/agents/{name} against the real DB shim."""
    from app.api.agents import ProfileSubagentRequest, update_profile_agent
    from app.db import database
    if not database.get_profile("caller"):
        _agent("caller", {})
    return await update_profile_agent("caller", agent_id, ProfileSubagentRequest(**fields), token="t")


class TestSubagentKeepsStoredPrompt:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("args", [
        {"provider_models": {"__native__": "haiku"}},
        {"name": "Renamed"},
        {"add": {"tools": ["Read"], "skills": ["lint"]}},
        {"prompt": ""},  # the text this view shows for a preset: unchanged
    ])
    async def test_other_edits_keep_a_preset_prompt(self, mock_db, subagent_tools, args):
        _agent("styled", {"system_prompt": dict(_PRESET)})
        body = await _call(subagent_tools, "updateSubagent", {"subagent_id": "styled", **args})
        assert body["success"] is True, body
        assert _stored("styled")["system_prompt"] == _PRESET

    @pytest.mark.asyncio
    async def test_prompt_edits_patch_the_preset_append(self, mock_db, subagent_tools):
        _agent("styled", {"system_prompt": dict(_PRESET)})
        body = await _call(subagent_tools, "updateSubagent", {
            "subagent_id": "styled", "prompt_edits": [{"old": "these", "new": "the house"}]})
        assert body["success"] is True, body
        assert body["updated_fields"] == ["prompt"]
        assert body["prompt"]["edited"] == "text appended to the preset"
        assert _stored("styled")["system_prompt"] == {**_PRESET, "append": "Keep the house rules."}

    @pytest.mark.asyncio
    async def test_full_prompt_replaces_with_custom(self, mock_db, subagent_tools):
        _agent("styled", {"system_prompt": dict(_PRESET)})
        await _call(subagent_tools, "updateSubagent", {"subagent_id": "styled", "prompt": "Own words."})
        assert _stored("styled")["system_prompt"] == {"type": "custom", "content": "Own words."}

    @pytest.mark.asyncio
    @pytest.mark.parametrize("config", [{"system_prompt": "Legacy text."}, {}],
                             ids=["legacy-string", "no-prompt"])
    async def test_legacy_and_missing_prompts_left_alone(self, mock_db, subagent_tools, config):
        _agent("old", dict(config))
        await _call(subagent_tools, "updateSubagent", {"subagent_id": "old", "name": "Older"})
        assert _stored("old").get("system_prompt") == config.get("system_prompt")

    @pytest.mark.asyncio
    async def test_rest_put_round_trip_keeps_a_preset_prompt(self, mock_db):
        _agent("styled", {"system_prompt": dict(_PRESET)})
        updated = await _rest_put("styled", description="New", prompt="")
        assert updated.description == "New"
        assert _stored("styled")["system_prompt"] == _PRESET


def _access(agent_id):
    from app.core.tool_access import resolve_tool_access
    access = resolve_tool_access(_stored(agent_id))
    return access.allowed, access.disallowed


class TestSubagentToolsSetTheMode:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("start, edit, mode, access", [
        ({"tool_selection_mode": "none"}, {"add": {"tools": ["Read"]}}, "allow", (["Read"], None)),
        ({"tool_selection_mode": "all"}, {"add": {"tools": ["Read"]}}, "allow", (["Read"], None)),
        ({"tool_selection_mode": "allow", "allowed_tools": ["Read", "Grep"]}, {"tools": None},
         "all", (None, None)),
        ({"tool_selection_mode": "none"}, {"tools": None}, "all", (None, None)),
        ({"tool_selection_mode": "disallow", "disallowed_tools": ["Bash"]}, {"tools": None},
         "disallow", (None, ["Bash"])),
        ({"tool_selection_mode": "disallow", "disallowed_tools": ["Bash"]}, {"tools": ["Read"]},
         "allow", (["Read"], None)),
        ({"tool_selection_mode": "allow", "allowed_tools": ["Read", "Grep"]},
         {"remove": {"tools": ["Grep"]}}, "allow", (["Read"], None)),
        ({}, {"tools": []}, "none", (None, ["*"])),
    ], ids=["none+add", "all+add", "allow+null", "none+null", "disallow+null",
            "disallow+list", "allow+remove", "unstamped+empty"])
    async def test_explicit_tools_edit(self, mock_db, subagent_tools, start, edit, mode, access):
        _agent("worker", dict(start))
        body = await _call(subagent_tools, "updateSubagent", {"subagent_id": "worker", **edit})
        assert body["success"] is True, body
        assert _stored("worker")["tool_selection_mode"] == mode
        assert _access("worker") == access

    @pytest.mark.asyncio
    @pytest.mark.parametrize("start", [
        {}, {"allowed_tools": ["Read"]}, {"tool_selection_mode": "all", "allowed_tools": ["Read"]},
        {"tool_selection_mode": "none"}, {"tool_selection_mode": "allow", "allowed_tools": ["Read"]},
        {"tool_selection_mode": "disallow", "disallowed_tools": ["Bash"]},
    ])
    async def test_unrelated_edit_leaves_tools_untouched(self, mock_db, subagent_tools, start):
        _agent("worker", {**start, "system_prompt": dict(_PRESET)})
        before = _stored("worker")
        body = await _call(subagent_tools, "updateSubagent", {"subagent_id": "worker", "name": "Renamed"})
        assert body["success"] is True, body
        assert _stored("worker") == before

    @pytest.mark.asyncio
    async def test_removing_the_last_tool_still_refused(self, mock_db, subagent_tools):
        _agent("worker", {"tool_selection_mode": "allow", "allowed_tools": ["Read"]})
        before = _stored("worker")
        body = await _call(subagent_tools, "updateSubagent",
                           {"subagent_id": "worker", "remove": {"tools": ["Read"]}})
        assert body["success"] is False and "tools: null" in body["error"]
        assert _stored("worker") == before

    @pytest.mark.asyncio
    async def test_rest_put_sets_the_mode_too(self, mock_db):
        _agent("worker", {"tool_selection_mode": "none"})
        await _rest_put("worker", description="d", prompt="")  # tools omitted: unchanged
        assert _stored("worker")["tool_selection_mode"] == "none"
        assert _access("worker") == (None, ["*"])
        await _rest_put("worker", description="d", prompt="", tools=["Grep"])
        assert _access("worker") == (["Grep"], None)
        await _rest_put("worker", description="d", prompt="", tools=None)  # explicit null: all
        assert _stored("worker")["tool_selection_mode"] == "all"
        assert _access("worker") == (None, None)


class TestClearingWithNull:
    _PROFILE_CLEARS = {"avatar_color": None, "agent_invoke_mode": None, "enabled_agents": None,
                       "provider_models": None, "enabled_plugins": None, "add_dirs": None}
    _SUBAGENT_CLEARS = {"tools": None, "model": None, "provider_models": None}

    @pytest.mark.asyncio
    async def test_profile_clears_are_schema_valid_and_applied(self, mock_db, profile_tools):
        from jsonschema import validate
        args = {"profile_id": "full", **self._PROFILE_CLEARS}
        validate(args, get_tool(profile_tools, "updateProfile").input_schema)
        await _call(profile_tools, "createProfile", {
            "name": "Full", "avatar_color": "rose", "agent_invoke_mode": "custom",
            "enabled_agents": ["helper"], "provider_models": {"__native__": "haiku"},
            "enabled_plugins": ["plug@market"], "add_dirs": ["/srv"]})
        body = await _call(profile_tools, "updateProfile", args)
        assert body["success"] is True, body
        got = await _call(profile_tools, "getProfile", {"profile_id": "full"})
        assert got["avatar_color"] is None
        assert not set(self._PROFILE_CLEARS) & set(got["config"])

    @pytest.mark.asyncio
    async def test_subagent_clears_are_schema_valid_and_applied(self, mock_db, subagent_tools):
        from jsonschema import validate
        created = await _call(subagent_tools, "createSubagent", {
            "name": "Clearable", "description": "d", "prompt": "p", "tools": ["Read"],
            "model": "haiku", "provider_models": {"__native__": "haiku"}})
        args = {"subagent_id": created["subagent_id"], **self._SUBAGENT_CLEARS}
        validate(args, get_tool(subagent_tools, "updateSubagent").input_schema)
        body = await _call(subagent_tools, "updateSubagent", args)
        assert body["success"] is True, body
        got = await _call(subagent_tools, "getSubagent", {"subagent_id": created["subagent_id"]})
        assert got["tools"] is None and got["model"] is None and not got["provider_models"]
