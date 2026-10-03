"""Foreign tool names and subagent fields (fluid.context.plugin_import).

The validator uses the tool-name table; reading foreign plugins through these
rules is a separate change.
"""

import pytest

from fluid.context.plugin_import import map_tool, map_tools, normalize_agent_fields


# ─── tool names ──────────────────────────────────────────────────────────────


@pytest.mark.parametrize(("foreign", "native"), [
    ("Read", "Read"),
    ("MultiEdit", "Edit"),
    ("LS", "Glob"),
    ("Agent", "Task"),
    ("AskUserQuestion", "AskUser"),
    ("shell", "Bash"),
    ("apply_patch", "Edit"),
    ("read_file", "Read"),
    ("web_search", "WebSearch"),
    ("Bash(git status:*)", "Bash"),
    ("mcp__github__get_issue", "mcp__github__get_issue"),
    ("TodoWrite", None),
    ("update_plan", None),
    ("NoSuchTool", None),
])
def test_foreign_tool_names_map_or_drop(foreign, native) -> None:
    assert map_tool(foreign)[0] == native


def test_claude_plugin_mcp_tool_names_map_to_this_apps_names() -> None:
    kept, notes = map_tools(
        ["mcp__plugin_cc-demo_docs__search"], plugin_name="cc-demo", server_names=["docs"]
    )
    assert kept == ["mcp__docs__search"]
    assert notes == ["mcp__plugin_cc-demo_docs__search became mcp__docs__search"]


def test_agent_with_no_usable_tools_is_skipped_not_widened() -> None:
    out = normalize_agent_fields({"name": "p", "tools": "TodoWrite"})
    assert out.skip_reason
    assert "tools" not in out.fields


def test_agent_without_tools_keeps_inheriting() -> None:
    out = normalize_agent_fields({"name": "p", "description": "d", "model": "opus"})
    assert out.skip_reason == ""
    assert out.fields == {"name": "p", "description": "d"}
    assert any("model pin removed" in n for n in out.notes)
