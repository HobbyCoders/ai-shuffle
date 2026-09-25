"""Plugin loading edges: enabledPlugins refs are confined to the plugins root,
and malformed manifests / MCP configs degrade to "skip" instead of failing."""

from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest

from fluid.context import plugins as plugins_mod
from fluid.context.plugins import (
    Plugin,
    _latest_version_dir,
    _plain_component,
    _raw_mcp_servers,
    _skill_files,
    discover_plugins,
    load_plugin,
    normalize_mcp_server,
    plugin_mcp_servers,
    read_manifest,
    resolve_plugin_root,
)


def _write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def _evil_plugin(root: Path) -> Path:
    _write(root / ".claude-plugin" / "plugin.json", json.dumps({"name": "evil"}))
    _write(root / ".mcp.json", json.dumps({"mcpServers": {"pwn": {"command": "sh"}}}))
    return root


@pytest.mark.parametrize("ref", [
    "../../../repo/evil@mp",           # name climbs out of cache/<mp>/
    "evil@../../repo",                 # marketplace climbs out of marketplaces/
    "..@mp",
])
def test_enabled_plugin_refs_cannot_escape_the_plugins_root(tmp_path: Path, ref: str) -> None:
    """A project-scope settings file can enable plugins; a ``../`` ref must not
    turn a directory in the checkout into a plugin whose MCP servers start."""
    home = tmp_path / "home" / "plugins"
    (home / "cache" / "mp").mkdir(parents=True)
    (home / "marketplaces").mkdir(parents=True)
    _evil_plugin(tmp_path / "home" / "repo" / "evil")
    _evil_plugin(tmp_path / "repo" / "evil")
    settings = {"enabledPlugins": {ref: True}}
    assert discover_plugins(tmp_path, settings=settings, home=home) == []


def test_resolve_refuses_non_plain_components_but_takes_plain_ones(tmp_path: Path) -> None:
    home = tmp_path / "plugins"
    _evil_plugin(home / "marketplaces" / "mp" / "good")
    assert resolve_plugin_root("good", "mp", home=home) == (home / "marketplaces" / "mp" / "good", None)
    assert resolve_plugin_root("good", "C:mp", home=home) is None
    assert resolve_plugin_root("", "mp", home=home) is None
    assert _plain_component("good-plugin.v2")
    assert not _plain_component("a\\b") and not _plain_component("a\0b")


def test_marketplace_root_plugin_must_name_itself(tmp_path: Path) -> None:
    home = tmp_path / "plugins"
    _write(home / "marketplaces" / "mp" / "plugin.json", json.dumps({"name": "other"}))
    assert resolve_plugin_root("mine", "mp", home=home) is None
    assert resolve_plugin_root("other", "mp", home=home) == (home / "marketplaces" / "mp", None)


def test_empty_cache_dir_falls_through_and_unlistable_cache_is_skipped(
    tmp_path: Path, monkeypatch, caplog
) -> None:
    home = tmp_path / "plugins"
    (home / "cache" / "mp" / "p").mkdir(parents=True)  # no version dirs
    _evil_plugin(home / "marketplaces" / "mp" / "p")
    assert resolve_plugin_root("p", "mp", home=home)[1] is None

    def unlistable(self):
        raise PermissionError("denied")

    monkeypatch.setattr(Path, "iterdir", unlistable)
    with caplog.at_level(logging.WARNING, logger=plugins_mod.log.name):
        assert _latest_version_dir(home / "cache" / "mp" / "p") is None
    assert "cannot list plugin cache dir" in caplog.text


@pytest.mark.parametrize("body", ["{not json", "[1, 2]"])
def test_read_manifest_malformed_or_non_object(tmp_path: Path, body: str) -> None:
    _write(tmp_path / "plugin.json", body)
    assert read_manifest(tmp_path) == {}


def test_skill_files_manifest_entries_file_dir_and_missing(tmp_path: Path) -> None:
    root = tmp_path / "plug"
    direct = _write(root / "extra" / "one" / "SKILL.md", "---\nname: one\n---\n")
    loose = _write(root / "loose.md", "---\nname: loose\n---\n")
    _write(root / "skills" / "base" / "SKILL.md", "---\nname: base\n---\n")
    manifest = {"skills": ["./extra/one", "./loose.md", "./nowhere", "./skills"]}
    found = _skill_files(root, manifest)
    assert found == [root / "skills" / "base" / "SKILL.md", direct, loose]


def test_raw_mcp_servers_path_variants(tmp_path: Path) -> None:
    root = tmp_path
    assert _raw_mcp_servers(root, {"mcpServers": "./missing.json"}) == {}
    _write(root / "bad.json", "{oops")
    assert _raw_mcp_servers(root, {"mcpServers": "./bad.json"}) == {}
    _write(root / "list.json", "[]")
    assert _raw_mcp_servers(root, {"mcpServers": "./list.json"}) == {}
    _write(root / "odd.json", json.dumps({"mcpServers": ["x"]}))
    assert _raw_mcp_servers(root, {"mcpServers": "./odd.json"}) == {}
    _write(root / "flat.json", json.dumps({"srv": {"command": "x"}}))
    assert _raw_mcp_servers(root, {"mcpServers": "./flat.json"}) == {"srv": {"command": "x"}}
    assert _raw_mcp_servers(root, {}) == {}  # no .mcp.json at the root


def test_load_plugin_skips_bad_and_disabled_servers(tmp_path: Path) -> None:
    home = tmp_path / "plugins"
    root = home / "cache" / "mp" / "p" / "1.0.0"
    _write(root / "plugin.json", json.dumps({
        "name": "p", "hooks": "./hooks.json",
        "mcpServers": {
            "not-an-object": "x",
            "off": {"command": "a"},
            "web": {"url": "https://example.test/mcp"},
            "stdio": {"command": "b", "env": {"K": "v"}},
        },
    }))
    plugin = load_plugin("p", "mp", cwd=tmp_path, disabled_mcp={"off"}, home=home)
    assert plugin is not None and plugin.version == "1.0.0"
    assert set(plugin.mcp_servers) == {"web", "stdio"}
    assert plugin.mcp_servers["web"]["transport"] == "streamable_http"
    assert plugin.mcp_servers["stdio"]["env"] == {"K": "v", "CLAUDE_PLUGIN_ROOT": str(root)}


def test_normalize_mcp_server_rejects_incomplete_entries() -> None:
    assert normalize_mcp_server("h", {"type": "http"}) is None
    assert normalize_mcp_server("s", {"args": ["x"]}) is None


def test_discover_plugins_skips_a_plugin_that_raises(tmp_path: Path, monkeypatch) -> None:
    home = tmp_path / "plugins"
    _evil_plugin(home / "marketplaces" / "mp" / "good")

    real = plugins_mod.load_plugin

    def flaky(name, marketplace, **kw):
        if name == "bad":
            raise RuntimeError("boom")
        return real(name, marketplace, **kw)

    monkeypatch.setattr(plugins_mod, "load_plugin", flaky)
    settings = {"enabledPlugins": {"bad@mp": True, "good@mp": True, "@mp": True}}
    found = discover_plugins(tmp_path, settings=settings, home=home)
    assert [p.ref for p in found] == ["evil@mp"]


def test_plugin_mcp_servers_first_claim_wins(tmp_path: Path) -> None:
    a = Plugin(name="a", marketplace="m", root=tmp_path, mcp_servers={"s": {"command": "a"}})
    b = Plugin(name="b", marketplace="m", root=tmp_path, mcp_servers={"s": {"command": "b"}})
    assert plugin_mcp_servers([a, b]) == {"s": {"command": "a"}}
