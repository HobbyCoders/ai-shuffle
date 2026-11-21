"""The Vault tools were the "chat access" tools. Renaming them must not lift a
block, drop a grant or break a worker on an older version:

* saved tool lists (agents, agent files) naming the old tools select the new ones;
* a saved permission rule or a hook matcher on an old name still applies;
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
    # A pattern stays and gains the tools it used to select.
    whole_server = aliases(["mcp__chat-access__*"])
    assert whole_server[0] == "mcp__chat-access__*"
    assert set(TOOL_NAMES) <= set(whole_server)
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
    assert denied[0] == "Bash" and set(TOOL_NAMES) <= set(denied)


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


def test_names_shared_with_other_workers_and_the_settings_store_are_unchanged():
    from app.core import vault_store
    from app.network import control, policy, vault_routing

    assert (vault_routing.CONTROL_ACTION, vault_routing.CHANGED_ACTION) == ("chat-access", "chat-access-changed")
    assert control.known_action("chat-access") and control.known_action("chat-access-changed")
    saved_key = vault_store.key(("user", "u1", None), "0" * 32)
    assert saved_key.startswith("chat_connection:")
    assert not policy.is_shared_setting_key(saved_key)
