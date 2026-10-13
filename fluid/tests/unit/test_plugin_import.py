"""Plugins from Claude Code and Codex behave natively here (fluid.context.plugin_import).

Small fixtures in the shape each tool publishes; nothing is downloaded.
"""

import json
from pathlib import Path

import pytest

from fluid.context.agents import discover_agents
from fluid.context.plugin_formats import read_plugin
from fluid.context.plugin_import import map_tool, map_tools, normalize_agent_fields, summarize


def _write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def _claude_plugin(root: Path) -> Path:
    """A Claude Code plugin with the fields that only mean something there."""
    _write(root / ".claude-plugin" / "plugin.json", json.dumps({
        "name": "cc-demo",
        "version": "1.0.0",
        "description": "Claude Code demo",
        "lspServers": {"go": {"command": "gopls", "extensionToLanguage": {".go": "go"}}},
        "outputStyles": "./styles/",
        "userConfig": {"api_token": {"type": "string", "title": "t", "description": "d", "sensitive": True}},
        "experimental": {"monitors": [{"name": "m", "command": "x", "description": "d"}]},
    }))
    _write(root / "agents" / "reviewer.md", (
        "---\n"
        "name: reviewer\n"
        "description: reviews code\n"
        "model: opus\n"
        "effort: high\n"
        "permissionMode: bypassPermissions\n"
        "color: blue\n"
        "maxTurns: 5\n"
        "tools: Read, MultiEdit, TodoWrite, mcp__plugin_cc-demo_docs__search, Bash(git status:*)\n"
        "---\n"
        "Review using ${CLAUDE_PLUGIN_ROOT}/rules.md\n"
    ))
    _write(root / "agents" / "planner.md", (
        "---\nname: planner\ndescription: plans\ntools: TodoWrite, ExitPlanMode\n---\nPlan.\n"
    ))
    _write(root / "skills" / "lint" / "SKILL.md", (
        "---\nname: lint\ndescription: lint it\nmodel: haiku\n---\n"
        "Run ${CLAUDE_PLUGIN_ROOT}/scripts/lint.sh with ${user_config.api_token}\n"
    ))
    _write(root / "commands" / "ship.md", (
        "---\ndescription: ship\nmodel: sonnet\nallowed-tools: Bash(git:*)\n---\n"
        "Status: !`git status`\nShip $ARGUMENTS\n"
    ))
    _write(root / "agents" / "guarded.md", (
        "---\nname: guarded\ndescription: no writes\n"
        "disallowedTools: Write, MultiEdit, TodoWrite\n---\nLook only.\n"
    ))
    _write(root / ".mcp.json", json.dumps({"mcpServers": {
        "docs": {"command": "node", "args": ["${CLAUDE_PLUGIN_ROOT}/docs.js"]},
        "billing": {"command": "node", "args": ["b.js"], "env": {"TOKEN": "${user_config.api_token}"}},
    }}))
    (root / "bin").mkdir(parents=True, exist_ok=True)
    return root


def _codex_plugin(root: Path) -> Path:
    """A Codex plugin: .codex-plugin manifest, interface, skills, .mcp.json, apps, hooks."""
    _write(root / ".codex-plugin" / "plugin.json", json.dumps({
        "name": "cx-demo",
        "version": "0.2.0",
        "description": "Codex demo",
        "skills": "./skills/",
        "mcpServers": "./.mcp.json",
        "apps": "./.app.json",
        "hooks": "./hooks.json",
        "interface": {"displayName": "Codex Demo", "shortDescription": "short", "developerName": "Acme"},
    }))
    _write(root / "skills" / "search" / "SKILL.md", "---\nname: search\ndescription: search docs\n---\nSearch.\n")
    _write(root / "skills" / "search" / "agents" / "openai.yaml", "interface:\n  display_name: Search\n")
    _write(root / ".mcp.json", json.dumps({"mcpServers": {"kb": {"url": "https://kb.example/mcp"}}}))
    _write(root / ".app.json", json.dumps({"apps": {"drive": {"id": "x"}}}))
    _write(root / "hooks.json", json.dumps({"hooks": {"SessionStart": []}}))
    _write(root / "agents" / "helper.toml", 'name = "helper"\nmodel = "gpt-5"\n')
    return root


def _by(record, kind: str, name: str):
    return next(c for c in record.components if c.kind == kind and c.name == name)


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


def test_denied_tools_are_mapped_kept_and_taken_out_of_the_tool_list() -> None:
    out = normalize_agent_fields({
        "name": "p", "tools": "Read, Edit, Bash", "disallowedTools": ["MultiEdit", "Bash"],
    })
    assert out.fields["disallowedTools"] == ["Edit", "Bash"]
    assert out.fields["tools"] == ["Read"]
    only_denied = normalize_agent_fields({"name": "p", "tools": "Bash", "disallowedTools": "Bash"})
    assert only_denied.skip_reason


def test_plugin_commands_never_run_inline_shell(tmp_path) -> None:
    from fluid.context.instructions import render_command_blocks

    marker = tmp_path / "ran.txt"
    text = f"---\nallowed-tools: Bash\n---\nOut: !`echo hi > {marker.as_posix()}`\n"
    blocks = render_command_blocks(text, "", cwd=tmp_path, inline_bash=False)
    assert "!`echo hi" in blocks[0]
    assert not marker.exists()


def test_agent_without_tools_keeps_inheriting() -> None:
    out = normalize_agent_fields({"name": "p", "description": "d", "model": "opus"})
    assert out.skip_reason == ""
    assert out.fields == {"name": "p", "description": "d"}
    assert any("model pin removed" in n for n in out.notes)


# ─── Claude Code plugin ──────────────────────────────────────────────────────


def test_claude_code_plugin_components_record_what_was_adapted_and_skipped(tmp_path) -> None:
    record = read_plugin(_claude_plugin(tmp_path / "cc"), source="mp")
    reviewer = _by(record, "agent", "reviewer")
    assert reviewer.support == "loaded"
    for text in ("model pin removed", "effort pin removed", "permissionMode removed",
                 "MultiEdit became Edit", "TodoWrite was dropped",
                 "mcp__plugin_cc-demo_docs__search became mcp__docs__search",
                 "Bash(git status:*) became Bash", "color", "maxTurns"):
        assert text in reviewer.reason, text
    planner = _by(record, "agent", "planner")
    assert planner.support == "unsupported" and "every tool" in planner.reason
    assert sorted(a.name for a in record.agents) == ["cc-demo:guarded", "cc-demo:reviewer"]

    lint = _by(record, "skill", "lint")
    assert lint.support == "loaded"
    assert "model pin ignored" in lint.reason and "api_token" in lint.reason
    ship = _by(record, "command", "ship").reason
    assert "model pin ignored" in ship and "inline shell commands" in ship
    guarded = _by(record, "agent", "guarded")
    assert guarded.support == "loaded"
    assert "denied MultiEdit became Edit" in guarded.reason
    assert "denied TodoWrite was dropped" in guarded.reason

    assert _by(record, "mcp_server", "docs").support == "loaded"
    billing = _by(record, "mcp_server", "billing")
    assert billing.support == "needs_config" and "api_token" in billing.reason
    for kind in ("lsp_server", "output_style", "monitor", "executables", "user_config"):
        assert record.has(kind), kind
        assert all(c.support != "loaded" for c in record.components if c.kind == kind)

    report = summarize(record.components)
    assert any(line.startswith("agent reviewer: ") for line in report["adapted"])
    assert any(line.startswith("agent planner: ") for line in report["skipped"])
    assert any(line.startswith("lsp server go: ") for line in report["skipped"])


def test_claude_code_subagent_loads_without_its_pins(tmp_path) -> None:
    home = Path.home()
    root = _claude_plugin(home / ".fluid" / "plugins" / "cache" / "mp" / "cc-demo" / "1.0.0")
    _write(home / ".fluid" / "settings.json", json.dumps({"enabledPlugins": {"cc-demo@mp": True}}))
    proj = tmp_path / "proj"
    (proj / ".git").mkdir(parents=True)

    agents = {a.name: a for a in discover_agents(proj, include_global=False, include_plugins=True)}
    assert set(agents) == {"cc-demo:reviewer", "cc-demo:guarded"}
    guarded = agents["cc-demo:guarded"]
    assert guarded.tools == ()
    assert guarded.disallowed_tools == ("Write", "Edit")
    reviewer = agents["cc-demo:reviewer"]
    assert reviewer.model is None
    assert reviewer.tools == ("Read", "Edit", "mcp__docs__search", "Bash")
    assert set(reviewer.raw) == {"name", "description", "tools"}
    assert "${CLAUDE_PLUGIN_ROOT}" not in reviewer.body
    assert str(root).replace("\\", "/") + "/rules.md" in reviewer.body


async def test_skill_tool_expands_the_plugin_root_in_a_plugin_skill(tmp_path) -> None:
    from fluid.tools.builtins.skill import handler
    from fluid.tools.registry import ToolCtx

    home = Path.home()
    root = _claude_plugin(home / ".fluid" / "plugins" / "cache" / "mp" / "cc-demo" / "1.0.0")
    _write(home / ".fluid" / "settings.json", json.dumps({"enabledPlugins": {"cc-demo@mp": True}}))
    proj = tmp_path / "proj"
    (proj / ".git").mkdir(parents=True)

    out = await handler({"name": "cc-demo:lint"}, ToolCtx(cwd=str(proj)))
    assert not out.is_error, out.text
    assert str(root).replace("\\", "/") + "/scripts/lint.sh" in out.text
    # Only the plugin path variables are expanded; nothing else is looked up.
    assert "${user_config.api_token}" not in out.text
    assert "[plugin setting api_token is not configured in AI Shuffle]" in out.text


# ─── Codex plugin ────────────────────────────────────────────────────────────


def test_codex_plugin_components_record_what_was_adapted_and_skipped(tmp_path) -> None:
    record = read_plugin(_codex_plugin(tmp_path / "cx"), source="mp")
    assert record.format == "agent-plugins"
    assert record.display_name == "Codex Demo"
    assert record.author == "Acme"
    search = _by(record, "skill", "search")
    assert search.support == "loaded" and "openai.yaml" in search.reason
    assert _by(record, "mcp_server", "kb").support == "loaded"
    assert _by(record, "app", "drive").support == "unsupported"
    assert _by(record, "hook", "./hooks.json").support == "unsupported"
    toml = _by(record, "agent", "agents/*.toml")
    assert toml.support == "unsupported" and "TOML" in toml.reason
    report = summarize(record.components)
    assert any("openai.yaml" in line for line in report["adapted"])
    assert {line.split(":")[0] for line in report["skipped"]} >= {"app drive", "agent agents/*.toml"}


def test_mcp_servers_declared_as_a_list_are_read(tmp_path) -> None:
    root = tmp_path / "p"
    _write(root / ".claude-plugin" / "plugin.json", json.dumps({
        "name": "p", "mcpServers": ["./extra.json", {"inline": {"command": "x"}}, "./bundle.mcpb"],
    }))
    _write(root / "extra.json", json.dumps({"mcpServers": {"fromfile": {"command": "y"}}}))
    record = read_plugin(root)
    assert set(record.mcp_servers) == {"fromfile", "inline"}
    bundle = _by(record, "mcp_server", "./bundle.mcpb")
    assert bundle.support == "unsupported"
