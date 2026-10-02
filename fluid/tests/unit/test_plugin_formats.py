"""#1610 — one provider-neutral plugin reader (fluid.context.plugin_formats).

Fixture plugins in both package formats are laid out in tmp_path and read into
a PluginRecord; the runtime loader is checked to take the same components.
HOME is redirected like test_plugins.py so a real ~/.fluid never leaks in.
"""

import json
import os
from pathlib import Path

import pytest

from fluid.context.plugin_formats import (
    FORMAT_AGENT_PLUGINS,
    FORMAT_CLAUDE,
    SUPPORT_LOADED,
    SUPPORT_NEEDS_CONFIG,
    SUPPORT_UNSUPPORTED,
    content_hash,
    detect_format,
    read_marketplace,
    read_plugin,
    substitute_vars,
)
from fluid.context.plugins import discover_plugins


def _write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def _json(path: Path, data) -> Path:
    return _write(path, json.dumps(data))


def _support(record, kind: str, name: str) -> tuple[str, str]:
    for c in record.components:
        if c.kind == kind and c.name == name:
            return c.support, c.reason
    raise AssertionError(f"no {kind} {name!r} in {[(c.kind, c.name) for c in record.components]}")


def _claude_plugin(root: Path) -> Path:
    _json(root / ".claude-plugin" / "plugin.json", {
        "name": "reviewkit",
        "version": "2.0.0",
        "description": "Review helpers",
        "author": {"name": "Ada"},
        "lspServers": {"pyright": {"command": "pyright-langserver"}},
        "outputStyles": "./styles",
        "userConfig": {"api_token": {"type": "string", "sensitive": True}},
        "dependencies": ["other@mp"],
    })
    _write(root / "skills" / "triage" / "SKILL.md", "---\nname: triage\ndescription: sorts bugs\n---\nbody")
    _write(root / "commands" / "review.md", "Review $ARGUMENTS")
    _write(root / "agents" / "critic.md", "---\nname: critic\ndescription: critiques\n---\nprompt")
    _json(root / ".mcp.json", {"mcpServers": {
        "local": {"command": "node", "args": ["${CLAUDE_PLUGIN_ROOT}/s.js"]},
        "remote": {"type": "sse", "url": "https://example.test/sse"},
    }})
    _json(root / "hooks" / "hooks.json", {"hooks": {"PreToolUse": [], "Stop": []}})
    return root


def _agent_plugin(root: Path) -> Path:
    _json(root / "plugin.json", {
        "name": "notes-helper",
        "version": "0.3.0",
        "description": "Keeps notes",
        "author": "Grace",
        "skills": "./skills/",
        "mcpServers": "./mcp.json",
        "extensions": {
            "com.openai": {
                "interface": {
                    "displayName": "Notes Helper",
                    "shortDescription": "Short text",
                    "logo": "./assets/logo.png",
                },
                "apps": "./.app.json",
            },
            "ai.shuffle": {"commands": "./extra-commands/"},
        },
    })
    _write(root / "skills" / "jot" / "SKILL.md", "---\nname: jot\ndescription: writes a note\n---\nbody")
    _write(root / "extra-commands" / "summarize.md", "Summarize")
    _json(root / "mcp.json", {"mcpServers": {
        "notes": {"command": "python", "args": ["${PLUGIN_ROOT}/srv.py", "${PLUGIN_DATA}"]},
    }})
    _json(root / ".app.json", {"apps": {"drive": {"id": "connector_123"}}})
    return root


# ─── format detection ────────────────────────────────────────────────────────


def test_detects_format_from_the_manifest_file(tmp_path: Path) -> None:
    assert detect_format(_claude_plugin(tmp_path / "c")) == FORMAT_CLAUDE
    assert detect_format(_agent_plugin(tmp_path / "a")) == FORMAT_AGENT_PLUGINS
    codex = tmp_path / "x"
    _json(codex / ".codex-plugin" / "plugin.json", {"name": "x"})
    assert detect_format(codex) == FORMAT_AGENT_PLUGINS
    bare = tmp_path / "bare"
    (bare / "skills").mkdir(parents=True)
    assert detect_format(bare) == FORMAT_CLAUDE  # Claude's manifest is optional


def test_claude_manifest_wins_in_a_dual_format_plugin(tmp_path: Path) -> None:
    root = _agent_plugin(tmp_path / "dual")
    _json(root / ".claude-plugin" / "plugin.json", {"name": "notes-helper"})
    assert read_plugin(root).format == FORMAT_CLAUDE


# ─── Claude Code format ──────────────────────────────────────────────────────


def test_claude_plugin_reads_every_component_with_support(tmp_path: Path) -> None:
    record = read_plugin(_claude_plugin(tmp_path / "rk"), name="reviewkit", source="mp")

    assert record.id == "reviewkit@mp"
    assert record.format == FORMAT_CLAUDE
    assert (record.version, record.description, record.author) == ("2.0.0", "Review helpers", "Ada")
    assert record.display_name == "reviewkit"
    assert [s.name for s in record.skills] == ["reviewkit:triage"]
    assert [c.name for c in record.commands] == ["reviewkit:review"]
    assert [a.name for a in record.agents] == ["reviewkit:critic"]

    assert _support(record, "skill", "triage") == (SUPPORT_LOADED, "")
    assert _support(record, "mcp_server", "local") == (SUPPORT_LOADED, "")
    assert _support(record, "mcp_server", "remote")[0] == SUPPORT_UNSUPPORTED
    assert _support(record, "hook", "PreToolUse") == (SUPPORT_LOADED, "")
    assert _support(record, "hook", "Stop")[0] == SUPPORT_LOADED
    assert _support(record, "lsp_server", "pyright")[0] == SUPPORT_UNSUPPORTED
    assert _support(record, "output_style", "output styles")[0] == SUPPORT_UNSUPPORTED
    assert _support(record, "dependency", "dependencies")[0] == SUPPORT_UNSUPPORTED
    assert _support(record, "user_config", "api_token")[0] == SUPPORT_NEEDS_CONFIG
    # Every not-loaded component says why.
    assert all(c.reason for c in record.unsupported())


def test_inline_manifest_hooks_are_listed_but_not_run(tmp_path: Path) -> None:
    root = tmp_path / "p"
    _json(root / ".claude-plugin" / "plugin.json", {"name": "p", "hooks": {"SessionStart": []}})
    support, reason = _support(read_plugin(root), "hook", "SessionStart")
    assert support == SUPPORT_UNSUPPORTED and "hooks/hooks.json" in reason


def test_missing_manifest_falls_back_to_the_install_name(tmp_path: Path) -> None:
    root = tmp_path / "1.0.0"
    _write(root / "skills" / "a" / "SKILL.md", "---\nname: a\n---\n")
    record = read_plugin(root, name="plain", source="mp", version="1.0.0")
    assert (record.name, record.version, record.manifest_path) == ("plain", "1.0.0", None)
    assert [s.name for s in record.skills] == ["plain:a"]


def test_malformed_manifest_reads_as_empty(tmp_path: Path) -> None:
    root = tmp_path / "p"
    _write(root / ".claude-plugin" / "plugin.json", "{nope")
    _write(root / "commands" / "go.md", "go")
    record = read_plugin(root, name="p")
    assert record.manifest == {} and [c.name for c in record.commands] == ["p:go"]


# ─── Agent Plugins / OpenAI format ───────────────────────────────────────────


def test_agent_plugin_reads_skills_mcp_and_openai_metadata(tmp_path: Path) -> None:
    record = read_plugin(_agent_plugin(tmp_path / "nh"), source="mp")

    assert record.format == FORMAT_AGENT_PLUGINS
    assert record.id == "notes-helper@mp"
    assert record.display_name == "Notes Helper"
    assert record.description == "Keeps notes"  # manifest description beats the short one
    assert record.icon == "./assets/logo.png"
    assert record.author == "Grace"
    assert [s.name for s in record.skills] == ["notes-helper:jot"]
    # Commands declared under extensions["ai.shuffle"].
    assert [c.name for c in record.commands] == ["notes-helper:summarize"]
    assert set(record.mcp_servers) == {"notes"}
    assert _support(record, "mcp_server", "notes") == (SUPPORT_LOADED, "")
    support, reason = _support(record, "app", "drive")
    assert support == SUPPORT_UNSUPPORTED and "ChatGPT" in reason


def test_codex_legacy_manifest_and_top_level_interface(tmp_path: Path) -> None:
    root = tmp_path / "cx"
    _json(root / ".codex-plugin" / "plugin.json", {
        "name": "cx",
        "interface": {"displayName": "Codex X", "shortDescription": "Does X", "developerName": "Lin"},
        "apps": {"calendar": {}},
    })
    _write(root / "skills" / "s" / "SKILL.md", "---\nname: s\n---\n")
    _json(root / ".mcp.json", {"mcpServers": {"m": {"url": "https://example.test/mcp"}}})
    record = read_plugin(root)
    assert (record.display_name, record.description, record.author) == ("Codex X", "Does X", "Lin")
    assert _support(record, "app", "calendar")[0] == SUPPORT_UNSUPPORTED
    # .mcp.json still read when the package has no mcp.json.
    assert set(record.mcp_servers) == {"m"}


def test_mcp_json_wins_over_dot_mcp_json_in_agent_plugins(tmp_path: Path) -> None:
    root = tmp_path / "p"
    _json(root / "plugin.json", {"name": "p"})
    _json(root / "mcp.json", {"mcpServers": {"new": {"command": "a"}}})
    _json(root / ".mcp.json", {"mcpServers": {"old": {"command": "b"}}})
    assert set(read_plugin(root).mcp_servers) == {"new"}


def test_root_manifest_plugin_keeps_claude_convention_components(tmp_path: Path) -> None:
    """A plugin that used to load through a root plugin.json loses nothing."""
    root = tmp_path / "p"
    _json(root / "plugin.json", {"name": "p", "commands": "./cmds"})
    _write(root / "cmds" / "one.md", "x")
    _write(root / "agents" / "nested" / "a.md", "---\nname: a\n---\n")
    _json(root / ".mcp.json", {"srv": {"command": "x"}})
    record = read_plugin(root)
    assert [c.name for c in record.commands] == ["p:one"]
    assert [a.name for a in record.agents] == ["p:a"]
    assert set(record.mcp_servers) == {"srv"}


# ─── variables ───────────────────────────────────────────────────────────────


def test_both_formats_root_and_data_variables_resolve_to_the_same_paths(tmp_path: Path) -> None:
    root, data = tmp_path / "root", tmp_path / "data"
    value = {"args": ["${CLAUDE_PLUGIN_ROOT}", "${PLUGIN_ROOT}", "${CLAUDE_PLUGIN_DATA}", "${PLUGIN_DATA}"]}
    out = substitute_vars(value, plugin_root=root, project_dir=tmp_path, plugin_data=data, env={})
    assert out == {"args": [str(root), str(root), str(data), str(data)]}
    # Outside a plugin the names are ordinary variables.
    assert substitute_vars("${PLUGIN_ROOT}", plugin_root=None, project_dir=tmp_path, env={}) == ""


def test_runtime_loads_an_agent_plugin_with_its_variables(tmp_path: Path, monkeypatch) -> None:
    home = tmp_path / "home"
    home.mkdir()
    for var in ("HOME", "USERPROFILE"):
        monkeypatch.setenv(var, str(home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home / ".config"))
    root = _agent_plugin(home / ".fluid" / "plugins" / "cache" / "mp" / "notes-helper" / "0.3.0")
    _json(home / ".fluid" / "settings.json", {"enabledPlugins": {"notes-helper@mp": True}})
    proj = tmp_path / "proj"
    proj.mkdir()

    [plugin] = discover_plugins(proj)
    assert plugin.format == FORMAT_AGENT_PLUGINS
    assert [s.name for s in plugin.skills] == ["notes-helper:jot"]
    server = plugin.mcp_servers["notes"]
    data = home / ".fluid" / "plugins" / "data" / "notes-helper@mp"
    assert server["args"] == [f"{root}/srv.py", str(data)]
    assert server["env"]["PLUGIN_ROOT"] == str(root)
    assert server["env"]["PLUGIN_DATA"] == str(data)
    assert data.is_dir()


# ─── marketplaces ────────────────────────────────────────────────────────────


def test_claude_marketplace_is_read_as_is(tmp_path: Path) -> None:
    data = {"name": "mp", "plugins": [{"name": "a", "source": "./a"}]}
    _json(tmp_path / ".claude-plugin" / "marketplace.json", data)
    manifest = read_marketplace(tmp_path)
    assert manifest is not None and manifest.format == FORMAT_CLAUDE and manifest.data == data


def test_agents_marketplace_sources_are_normalized(tmp_path: Path) -> None:
    _json(tmp_path / ".agents" / "plugins" / "marketplace.json", {
        "name": "mp",
        "plugins": [
            {"name": "local", "source": {"source": "local", "path": "plugins/local"}},
            {"name": "dotted", "source": {"path": "./plugins/dotted"}},
            {"name": "remote", "source": {"source": "url", "url": "https://example.test/r.git", "ref": "v1"}},
            {"name": "npm", "source": {"source": "npm", "package": "@acme/plugin"}},
            "not-an-object",
        ],
    })
    manifest = read_marketplace(tmp_path)
    assert manifest is not None and manifest.format == FORMAT_AGENT_PLUGINS
    sources = {e["name"]: e["source"] for e in manifest.data["plugins"]}
    assert sources == {
        "local": "./plugins/local",
        "dotted": "./plugins/dotted",
        "remote": {"source": "url", "url": "https://example.test/r.git", "ref": "v1"},
    }
    assert manifest.skipped == ["npm"]


def test_no_marketplace_file_reads_none(tmp_path: Path) -> None:
    assert read_marketplace(tmp_path) is None


# ─── content hash ────────────────────────────────────────────────────────────


def test_content_hash_is_stable_and_tracks_edits(tmp_path: Path) -> None:
    root = _agent_plugin(tmp_path / "p")
    first = content_hash(root)
    assert first is not None and first.startswith("sha256:")
    assert content_hash(root) == first
    _write(root / "skills" / "jot" / "SKILL.md", "changed")
    assert content_hash(root) != first
    assert content_hash(tmp_path / "missing") is None


def test_content_hash_refuses_symlinks(tmp_path: Path) -> None:
    root = _agent_plugin(tmp_path / "p")
    target = _write(tmp_path / "outside.txt", "x")
    try:
        os.symlink(target, root / "link.txt")
    except (OSError, NotImplementedError):
        pytest.skip("symlinks unavailable")
    assert content_hash(root) is None
