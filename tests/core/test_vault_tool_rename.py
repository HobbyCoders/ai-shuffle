"""The Vault tools were the "chat access" tools. Renaming them must not lift a
block, drop a grant or break a worker on an older version:

* saved tool lists (agents, agent files) naming the old tools select the new ones;
* a saved permission rule or a hook matcher on an old name still applies;
* a pattern saved before the rename does not gain a tool through its new name;
* a block saved under a new name reaches other workers under the old one, so
  it holds on a worker that has not updated yet;
* the names other workers and the settings store see did not change.
"""

from unittest.mock import patch

import pytest

from app.core import tool_access
from app.core.hook_executor import tool_matcher_matches
from app.core.permission_handler import PermissionDecision, PermissionHandler, PermissionRule
from app.core.session_modes import turn_tool_surface
from app.mcp.vault_sdk import TOOL_NAMES

OLD_RUN = "mcp__chat-access__ChatRunCommand"
NEW_RUN = "mcp__app-vault__VaultRunCommand"
BRIDGE = "mcp__fluid_bundled__"
OLD_HTTP = "mcp__chat-access__ChatHttpRequest"
NEW_HTTP = "mcp__app-vault__VaultHttpRequest"


def test_every_vault_tool_has_its_earlier_name():
    assert sorted(tool_access.RENAMED_TOOLS.values()) == sorted(TOOL_NAMES)
    for name in TOOL_NAMES:
        (old,) = tool_access.earlier_tool_names(name)
        assert old.startswith("mcp__chat-access__")
        assert tool_access.earlier_tool_names(BRIDGE + name) == [BRIDGE + old]
    assert tool_access.earlier_tool_names("Bash") == []
    assert tool_access.earlier_tool_names(OLD_RUN) == []


def test_saved_lists_resolve_old_names_and_patterns():
    aliases = tool_access.apply_legacy_tool_aliases
    assert aliases([OLD_RUN, "Bash"]) == [NEW_RUN, "Bash"]
    assert aliases([BRIDGE + OLD_RUN]) == [BRIDGE + NEW_RUN]
    assert aliases([NEW_RUN, OLD_RUN]) == [NEW_RUN]
    # The whole earlier server stays and gains the whole new server, so it
    # also covers a tool the Vault gains later.
    assert aliases(["mcp__chat-access__*"]) == ["mcp__chat-access__*", "mcp__app-vault__*"]
    assert aliases([BRIDGE + "mcp__chat-access__*"]) == [
        BRIDGE + "mcp__chat-access__*", BRIDGE + "mcp__app-vault__*"]
    # Any other pattern stays and gains the tools it used to select.
    assert aliases(["mcp__chat-access__Chat*File"]) == [
        "mcp__chat-access__Chat*File",
        "mcp__app-vault__VaultReadFile",
        "mcp__app-vault__VaultWriteFile",
    ]
    # A pattern that still selects the tools gains nothing.
    assert aliases(["mcp__*"]) == ["mcp__*"]
    assert aliases(["*"]) == ["*"]


@pytest.mark.parametrize("entry", [OLD_RUN, "mcp__chat-access__*", "mcp__chat-access__ChatRun*"])
def test_a_blocklist_written_for_the_old_tool_still_blocks(entry):
    access = tool_access.resolve_tool_access(
        {"tool_selection_mode": "disallow", "disallowed_tools": [entry]})
    assert not tool_access.tool_permitted(NEW_RUN, access.allowed, access.disallowed)
    assert tool_access.tool_permitted("Bash", access.allowed, access.disallowed)


def test_an_allow_list_written_for_the_old_tool_still_allows_only_it():
    access = tool_access.resolve_tool_access(
        {"tool_selection_mode": "allow", "allowed_tools": [OLD_RUN]})
    assert tool_access.tool_permitted(NEW_RUN, access.allowed, access.disallowed)
    assert not tool_access.tool_permitted("mcp__app-vault__VaultWriteFile", access.allowed, access.disallowed)


def test_agent_file_lists_keep_their_entries_and_gain_the_new_names():
    assert tool_access.with_renamed_tools([" Read ", OLD_RUN, "", "PushNotification"]) == [
        "Read", OLD_RUN, NEW_RUN, "PushNotification"]
    surface, denied = turn_tool_surface(None, ["Bash"], {
        "tool_allowlist": ["Read", OLD_RUN], "tool_denylist": ["mcp__chat-access__*"]})
    assert NEW_RUN in surface and "Read" in surface
    assert denied == ["Bash", "mcp__chat-access__*", "mcp__app-vault__*"]
    assert not any(tool_access.tool_permitted(name, None, denied) for name in TOOL_NAMES)


def _rows(*rules):
    return [{"id": f"r{i}", "profile_id": "p", "tool_name": name, "tool_pattern": pattern, "decision": decision}
            for i, (name, pattern, decision) in enumerate(rules)]


def _decide(rows, tool, tool_input=None, session_rules=()):
    handler = PermissionHandler()
    handler._session_rules["s"] = list(session_rules)
    with patch("app.db.database.get_permission_rules", return_value=rows):
        rule = handler._check_rules("s", "p", tool, tool_input or {})
    return rule.decision.value if rule else None


@pytest.mark.parametrize("saved", [OLD_RUN, BRIDGE + OLD_RUN])
@pytest.mark.parametrize("called", [NEW_RUN, BRIDGE + NEW_RUN])
def test_a_rule_saved_for_the_old_tool_decides_the_new_one(saved, called):
    if saved.startswith(BRIDGE) or not called.startswith(BRIDGE):
        assert _decide(_rows((saved, None, "deny")), called) == "deny"
        assert _decide(_rows((saved, None, "allow")), called) == "allow"
    assert _decide(_rows((saved, None, "deny")), "mcp__app-vault__VaultReadFile") is None


def test_a_deny_on_the_old_name_beats_an_allow_on_the_new_one():
    rows = _rows((NEW_RUN, None, "allow"), (OLD_RUN, "rm *", "deny"))
    assert _decide(rows, NEW_RUN, {"command": "rm -rf /srv"}) == "deny"
    assert _decide(rows, NEW_RUN, {"command": "ls"}) == "allow"
    session_allow = PermissionRule(id="s1", session_id="s", profile_id=None, tool_name=NEW_RUN,
                                   tool_pattern=None, decision=PermissionDecision.ALLOW)
    assert _decide(_rows((OLD_RUN, None, "deny")), NEW_RUN, session_rules=[session_allow]) == "deny"
    # The current name still decides first when nothing denies.
    assert _decide(_rows((OLD_RUN, None, "allow"), (NEW_RUN, None, "defer")), NEW_RUN) == "defer"


def test_a_batched_request_is_checked_item_by_item_under_either_name():
    batch = {"requests": [{"grant_id": "g", "method": "GET", "path": "/zones"},
                          {"grant_id": "g", "method": "DELETE", "path": "/zones/1"}]}
    for saved in (OLD_HTTP, NEW_HTTP):
        assert _decide(_rows((saved, "/zones", "allow")), NEW_HTTP, batch) is None
        assert _decide(_rows((saved, "/zones/1", "deny")), NEW_HTTP, batch) == "deny"


def test_a_hook_matcher_written_for_the_old_tool_still_selects_it():
    assert tool_matcher_matches(OLD_RUN, NEW_RUN)
    assert tool_matcher_matches("mcp__chat-access__.*", BRIDGE + NEW_RUN) is False
    assert tool_matcher_matches(r"mcp__fluid_bundled__mcp__chat-access__.*", BRIDGE + NEW_RUN)
    assert tool_matcher_matches("mcp__chat-access__", NEW_RUN, plugin=True)
    assert tool_matcher_matches(NEW_RUN, NEW_RUN)
    assert not tool_matcher_matches(OLD_RUN, "mcp__app-vault__VaultReadFile")
    assert not tool_matcher_matches(OLD_RUN, "Bash")


def test_a_pattern_does_not_pick_up_a_tool_through_its_new_name():
    def allows(entries, tool):
        access = tool_access.resolve_tool_access({"tool_selection_mode": "allow", "allowed_tools": entries})
        return tool_access.tool_permitted(tool, access.allowed, access.disallowed)

    # ``mcp__app-*__*`` selected app-config and none of the chat access tools.
    assert allows(["mcp__app-*__*"], "mcp__app-config__createHook")
    assert not allows(["mcp__app-*__*"], NEW_RUN)
    assert not allows(["mcp__fluid_bundled__mcp__app-*"], BRIDGE + NEW_RUN)
    assert not allows(["mcp__app-*__*"], BRIDGE + NEW_RUN)
    # Named exactly, spelled-out server, or a pattern that always selected it.
    assert allows(["mcp__app-*__*", NEW_RUN], NEW_RUN)
    assert not allows(["mcp__app-*__*", NEW_RUN], NEW_HTTP)
    for entries in (["mcp__app-vault__*"], ["mcp__*"], ["*"], ["mcp__chat-access__*"], [OLD_RUN]):
        assert allows(entries, NEW_RUN), entries
    # An agent file's ``tools:`` line follows the same rule.
    surface, denied = turn_tool_surface(None, None, {"tool_allowlist": ["Read", "mcp__app-*__*"]})
    assert surface == ["Read", "mcp__app-*__*"] and set(TOOL_NAMES) <= set(denied)
    assert turn_tool_surface(None, None, {"tool_allowlist": ["Read", "mcp__app-vault__*"]}) == (
        ["Read", "mcp__app-vault__*"], None)


def test_tool_lists_reach_other_workers_under_the_earlier_names():
    shared = tool_access.shared_tool_names
    assert shared([NEW_RUN, "Bash", BRIDGE + NEW_HTTP]) == [OLD_RUN, "Bash", BRIDGE + OLD_HTTP]
    assert shared([NEW_RUN, OLD_RUN]) == [OLD_RUN] == shared([OLD_RUN, NEW_RUN])
    # A list that names no renamed tool is returned exactly as saved.
    untouched = [" Read ", "", 5, "mcp__*", "mcp__app-*__*", "mcp__chat-access__*", OLD_RUN, "*"]
    assert shared(untouched) == untouched
    # The whole new server travels as the whole earlier one, either spelling.
    assert shared(["mcp__app-vault__*", "Bash"]) == ["mcp__chat-access__*", "Bash"]
    assert shared([BRIDGE + "mcp__app-vault__*"]) == [BRIDGE + "mcp__chat-access__*"]
    assert shared(["mcp__app-vault__*", "mcp__chat-access__*"]) == ["mcp__chat-access__*"]
    # Any other pattern on the new server is kept and sends the tools it selects.
    assert shared(["mcp__app-vault__Vault*File"]) == [
        "mcp__app-vault__Vault*File", "mcp__chat-access__ChatReadFile", "mcp__chat-access__ChatWriteFile"]
    for names in ([NEW_RUN, "Bash"], ["mcp__app-vault__Vault*File", NEW_RUN], ["mcp__app-vault__*"], untouched):
        assert shared(shared(names)) == shared(names)
        # What an updated worker reads back selects the same tools.
        assert tool_access.current_tool_names(shared(names)) == tool_access.current_tool_names(names) or (
            set(tool_access.current_tool_names(names)) <= set(tool_access.current_tool_names(shared(names))))


def test_a_block_saved_here_still_blocks_on_a_worker_that_has_not_updated():
    from app.network import engine, policy

    def agent(tools, mode="disallow"):
        key = "disallowed_tools" if mode == "disallow" else "allowed_tools"
        return {"id": "a1", "name": "A", "description": None, "is_builtin": False, "avatar_color": None,
                "config": {"tool_selection_mode": mode, key: tools}, "mcp_tools": [],
                "created_at": "t", "updated_at": "t"}

    agents = policy.collection("agents")
    sent = engine.serialize_row(agents, agent([NEW_RUN, "Bash"]))
    # The older worker matches the list against the only names it knows.
    assert sent["config"]["disallowed_tools"] == [OLD_RUN, "Bash"]
    assert not tool_access.tool_permitted(OLD_RUN, None, sent["config"]["disallowed_tools"])
    # The record is the one a list saved before the rename produced.
    assert engine.content_hash(agents, sent) == engine.content_hash(
        agents, engine.serialize_row(agents, agent([OLD_RUN, "Bash"])))
    allowed = engine.serialize_row(agents, agent([NEW_RUN], mode="allow"))["config"]["allowed_tools"]
    assert allowed == [OLD_RUN]
    plain = agent(["Bash", "mcp__browser__*"])
    assert engine.serialize_row(agents, plain)["config"] == plain["config"]

    rules = policy.collection("permission_rules")
    rule = {"id": "r1", "profile_id": "a1", "tool_name": NEW_RUN, "tool_pattern": "rm *",
            "decision": "deny", "created_at": "t"}
    sent_rule = engine.serialize_row(rules, rule)
    assert sent_rule["tool_name"] == OLD_RUN and sent_rule["tool_pattern"] == "rm *"
    assert engine.content_hash(rules, sent_rule) == engine.content_hash(
        rules, engine.serialize_row(rules, {**rule, "tool_name": OLD_RUN}))
    # Stored under the earlier name on the worker that receives it, the rule
    # decides the renamed tool there once that worker updates.
    assert _decide(_rows((sent_rule["tool_name"], "rm *", "deny")), NEW_RUN, {"command": "rm -rf /srv"}) == "deny"
    assert engine.serialize_row(rules, {**rule, "tool_name": "Bash"})["tool_name"] == "Bash"


def test_a_hook_for_the_new_name_still_runs_on_a_worker_that_has_not_updated():
    from app.network import engine, policy

    hooks = policy.collection("project_hooks")

    def sent(matcher):
        row = {"id": "h1", "project_id": "p", "name": "guard", "event": "PreToolUse", "hook_type": "command",
               "command": "exit 2", "timeout": 5, "url": None, "secret": None, "enabled": True,
               "tool_matcher": matcher, "created_at": "t", "updated_at": "t"}
        return engine.serialize_row(hooks, row)["tool_matcher"]

    # The older worker matches the whole name against the only name it knows.
    for matcher in (NEW_RUN, "Bash|" + NEW_RUN, "mcp__app-vault__.*", "(?i)mcp__APP-vault__vaultruncommand"):
        shared = sent(matcher)
        assert shared.startswith(matcher + "|")
        assert tool_matcher_matches(shared, OLD_RUN) and tool_matcher_matches(shared, NEW_RUN), matcher
        assert sent(shared) == shared
    assert not tool_matcher_matches(sent(NEW_RUN), OLD_HTTP)
    assert tool_matcher_matches(sent(BRIDGE + NEW_RUN), BRIDGE + OLD_RUN)
    # A matcher that selects no renamed tool, or both names already, is not touched.
    for matcher in (None, "", "Bash", ".*", "mcp__.*", OLD_RUN, "mcp__chat-access__.*", "mcp__app-vault__(", "[unclosed"):
        assert sent(matcher) == matcher, matcher


def test_a_saved_list_reads_under_the_current_names():
    import json

    from app.db import profiles

    def read(config):
        return profiles._decode_row({"config": json.dumps(config), "mcp_tools": "[]"})["config"]

    config = read({"tool_selection_mode": "disallow",
                   "disallowed_tools": [OLD_RUN, "Bash", "mcp__chat-access__*", "mcp__browser__*"]})
    assert config["disallowed_tools"] == [NEW_RUN, "Bash", "mcp__app-vault__*", "mcp__browser__*"]
    # A pattern for part of the earlier server reads as the tools it selected.
    listed = read({"disallowed_tools": [OLD_RUN, "mcp__chat-access__Chat*File", "Bash"]})["disallowed_tools"]
    assert listed == [NEW_RUN, "mcp__app-vault__VaultReadFile", "mcp__app-vault__VaultWriteFile", "Bash"]
    assert read({"allowed_tools": [OLD_RUN, NEW_RUN, BRIDGE + OLD_HTTP]})["allowed_tools"] == [
        NEW_RUN, BRIDGE + NEW_HTTP]
    # Unticking the tool in the editor removes the one entry that selects it,
    # and the list that then travels no longer blocks it anywhere.
    unticked = [name for name in listed if name != NEW_RUN]
    assert OLD_RUN not in tool_access.shared_tool_names(unticked)
    untouched = {"allowed_tools": ["Read", "mcp__*", "mcp__chat-access__Other*"], "model": "m"}
    assert read(untouched) == untouched


def test_a_saved_rule_is_listed_under_the_current_name():
    import asyncio

    from app.api import agents

    rows = _rows((OLD_RUN, None, "deny"), ("Bash", "ls *", "allow"))
    for row in rows:
        row["created_at"] = "2026-01-01T00:00:00+00:00"
    with patch("app.db.database.get_profile", return_value={"id": "p"}), \
            patch("app.db.database.get_permission_rules", return_value=rows):
        listed = asyncio.run(agents.list_permission_rules("p", token="t"))
    assert [rule["tool_name"] for rule in listed] == [NEW_RUN, "Bash"]
    assert [rule["id"] for rule in listed] == ["r0", "r1"]


def test_names_shared_with_other_workers_and_the_settings_store_are_unchanged():
    from app.core import vault_store
    from app.network import control, policy, vault_routing

    assert (vault_routing.CONTROL_ACTION, vault_routing.CHANGED_ACTION) == ("chat-access", "chat-access-changed")
    assert control.known_action("chat-access") and control.known_action("chat-access-changed")
    saved_key = vault_store.key(("user", "u1", None), "0" * 32)
    assert saved_key.startswith("chat_connection:")
    assert not policy.is_shared_setting_key(saved_key)
