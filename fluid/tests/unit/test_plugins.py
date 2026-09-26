"""CE5 — fluid plugin loading (Claude package layout under ~/.fluid/plugins): resolution, components, MCP servers.

HOME (and USERPROFILE, which is what ``Path.expanduser`` reads on Windows) is
redirected to a tmp dir so the developer's real ~/.fluid never leaks into an
assertion, same convention as test_settings_stack.py.
"""

import json
import logging
from pathlib import Path

import pytest

from fluid.context.instructions import discover_commands, discover_skills
from fluid.context.plugins import (
    DEFAULT_MARKETPLACE,
    discover_plugins,
    normalize_mcp_server,
    plugin_mcp_servers,
    resolve_plugin_root,
    split_plugin_ref,
    substitute_vars,
)

SKILL_MD = "---\nname: hello\ndescription: greets the user\n---\nSKILL BODY NEVER LOADED"
AGENT_MD = "---\nname: reviewer\ndescription: reviews code\n---\nAGENT PROMPT NEVER LOADED"
COMMAND_MD = "Deploy the app: $ARGUMENTS"

MCP_JSON = {
    "mcpServers": {
        "myplug-local": {
            "command": "node",
            "args": ["${CLAUDE_PLUGIN_ROOT}/server.js", "--project", "${CLAUDE_PROJECT_DIR}"],
            "env": {"TOKEN": "${MYPLUG_TOKEN}"},
        },
        "myplug-http": {"type": "http", "url": "https://example.test/mcp"},
        "myplug-sse": {"type": "sse", "url": "https://example.test/sse"},
    }
}


# ─── fixture tree ────────────────────────────────────────────────────────────


def _write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def _set_home(monkeypatch, home: Path) -> None:
    home.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home / ".config"))


def _plugin_tree(root: Path, *, manifest: dict | str | None = None) -> Path:
    """Lay out a complete plugin (skill, command, agent, .mcp.json) at ``root``."""
    if manifest is None:
        manifest = {"name": "myplug", "version": "1.2.0", "description": "a test plugin"}
    body = manifest if isinstance(manifest, str) else json.dumps(manifest)
    _write(root / ".claude-plugin" / "plugin.json", body)
    _write(root / "skills" / "hello" / "SKILL.md", SKILL_MD)
    _write(root / "commands" / "deploy.md", COMMAND_MD)
    _write(root / "agents" / "reviewer.md", AGENT_MD)
    _write(root / ".mcp.json", json.dumps(MCP_JSON))
    return root


def _setup(tmp_path, monkeypatch, *, enabled=True, settings=None, versions=("1.2.0",), **kw):
    """Home with a cached ``myplug@testmp``, a project cwd, and settings."""
    home = tmp_path / "home"
    _set_home(monkeypatch, home)
    for version in versions:
        _plugin_tree(home / ".fluid" / "plugins" / "cache" / "testmp" / "myplug" / version, **kw)

    data = {"enabledPlugins": {"myplug@testmp": bool(enabled)}}
    if settings:
        data.update(settings)
    _write(home / ".fluid" / "settings.json", json.dumps(data))

    proj = tmp_path / "proj"
    (proj / ".git").mkdir(parents=True, exist_ok=True)
    return proj


# ─── enablement ──────────────────────────────────────────────────────────────


def test_disabled_plugin_is_invisible(tmp_path, monkeypatch) -> None:
    proj = _setup(tmp_path, monkeypatch, enabled=False)
    assert discover_plugins(proj) == []
    assert discover_skills(proj) == []
    assert discover_commands(proj) == []


def test_no_settings_at_all_yields_no_plugins(tmp_path, monkeypatch) -> None:
    home = tmp_path / "home"
    _set_home(monkeypatch, home)
    proj = tmp_path / "proj"
    (proj / ".git").mkdir(parents=True)
    assert discover_plugins(proj) == []


def test_enabled_plugin_surfaces_namespaced_components(tmp_path, monkeypatch) -> None:
    proj = _setup(tmp_path, monkeypatch)
    plugins = discover_plugins(proj)
    assert len(plugins) == 1
    plugin = plugins[0]
    assert (plugin.name, plugin.marketplace, plugin.version) == ("myplug", "testmp", "1.2.0")
    assert plugin.ref == "myplug@testmp"

    # Skill: frontmatter name wins over the directory name, then is namespaced.
    assert [(s.name, s.scope, s.body_loaded) for s in plugin.skills] == [
        ("myplug:hello", "plugin", False)
    ]
    assert plugin.skills[0].description == "greets the user"
    assert [(c.name, c.scope) for c in plugin.commands] == [("myplug:deploy", "plugin")]
    assert [(a.name, a.description, a.scope) for a in plugin.agents] == [
        ("myplug:reviewer", "reviews code", "plugin")
    ]
    # Bodies stay on disk — W4 reads the agent prompt, the Skill tool the skill.
    assert plugin.agents[0].body_loaded is False


def test_project_scope_can_enable_a_plugin(tmp_path, monkeypatch) -> None:
    """enabledPlugins merges across scopes like every other settings key."""
    proj = _setup(tmp_path, monkeypatch, enabled=False)
    _write(
        proj / ".fluid" / "settings.local.json",
        json.dumps({"enabledPlugins": {"myplug@testmp": True}}),
    )
    assert [p.name for p in discover_plugins(proj)] == ["myplug"]


# ─── root resolution ─────────────────────────────────────────────────────────


def test_highest_version_wins_numerically(tmp_path, monkeypatch) -> None:
    proj = _setup(tmp_path, monkeypatch, versions=("1.2.0", "1.10.0"))
    plugin = discover_plugins(proj)[0]
    assert plugin.root.name == "1.10.0"  # not the lexicographic "1.2.0"


def test_non_numeric_version_dir_is_the_fallback(tmp_path, monkeypatch) -> None:
    """Claude Code writes ``unknown`` for unversioned installs; a real version
    outranks it, and on its own it still resolves."""
    _setup(tmp_path, monkeypatch, versions=("unknown",))
    root, version = resolve_plugin_root("myplug", "testmp")
    assert version == "unknown"

    _plugin_tree(
        Path.home() / ".fluid" / "plugins" / "cache" / "testmp" / "myplug" / "0.1.0"
    )
    root, version = resolve_plugin_root("myplug", "testmp")
    assert version == "0.1.0"


def test_marketplace_clone_subdir_fallback(tmp_path, monkeypatch) -> None:
    home = tmp_path / "home"
    _set_home(monkeypatch, home)
    _plugin_tree(home / ".fluid" / "plugins" / "marketplaces" / "testmp" / "myplug")
    _write(
        home / ".fluid" / "settings.json",
        json.dumps({"enabledPlugins": {"myplug@testmp": True}}),
    )
    proj = tmp_path / "proj"
    (proj / ".git").mkdir(parents=True)

    plugin = discover_plugins(proj)[0]
    assert plugin.root == home / ".fluid" / "plugins" / "marketplaces" / "testmp" / "myplug"
    assert plugin.version == "1.2.0"  # from the manifest, no version dir in this layout
    assert [s.name for s in plugin.skills] == ["myplug:hello"]


def test_marketplace_root_is_the_plugin(tmp_path, monkeypatch) -> None:
    home = tmp_path / "home"
    _set_home(monkeypatch, home)
    _plugin_tree(home / ".fluid" / "plugins" / "marketplaces" / "myplug")
    _write(
        home / ".fluid" / "settings.json",
        json.dumps({"enabledPlugins": {"myplug@myplug": True}}),
    )
    proj = tmp_path / "proj"
    (proj / ".git").mkdir(parents=True)
    assert [s.name for s in discover_plugins(proj)[0].skills] == ["myplug:hello"]


def test_marketplace_root_with_a_different_name_is_not_adopted(tmp_path, monkeypatch) -> None:
    home = tmp_path / "home"
    _set_home(monkeypatch, home)
    _plugin_tree(home / ".fluid" / "plugins" / "marketplaces" / "testmp")
    assert resolve_plugin_root("other", "testmp") is None


def test_uninstalled_plugin_is_skipped_with_a_warning(tmp_path, monkeypatch, caplog) -> None:
    proj = _setup(tmp_path, monkeypatch, enabled=False)
    _write(
        proj / ".fluid" / "settings.json",
        json.dumps({"enabledPlugins": {"ghost@testmp": True}}),
    )
    with caplog.at_level(logging.WARNING):
        plugins = discover_plugins(proj)
    assert plugins == []
    assert any("not installed" in r.getMessage() for r in caplog.records)


def test_split_plugin_ref_defaults_the_marketplace() -> None:
    assert split_plugin_ref("a@mp") == ("a", "mp")
    assert split_plugin_ref("a") == ("a", DEFAULT_MARKETPLACE)


# ─── manifest handling ───────────────────────────────────────────────────────


def test_malformed_manifest_soft_fails_to_directory_conventions(
    tmp_path, monkeypatch, caplog
) -> None:
    proj = _setup(tmp_path, monkeypatch, manifest="{ not json at all")
    with caplog.at_level(logging.WARNING):
        plugins = discover_plugins(proj)
    assert any("malformed plugin manifest" in r.getMessage() for r in caplog.records)
    # The plugin still loads: the name falls back to the enabled entry's, and
    # every component is found by directory convention.
    plugin = plugins[0]
    assert plugin.name == "myplug" and plugin.manifest == {}
    assert [s.name for s in plugin.skills] == ["myplug:hello"]
    assert [c.name for c in plugin.commands] == ["myplug:deploy"]
    assert set(plugin.mcp_servers) == {"myplug-local", "myplug-http"}


def test_missing_manifest_still_loads(tmp_path, monkeypatch) -> None:
    proj = _setup(tmp_path, monkeypatch)
    (
        Path.home() / ".fluid" / "plugins" / "cache" / "testmp" / "myplug" / "1.2.0"
        / ".claude-plugin" / "plugin.json"
    ).unlink()
    plugin = discover_plugins(proj)[0]
    assert plugin.name == "myplug" and plugin.version == "1.2.0"
    assert [s.name for s in plugin.skills] == ["myplug:hello"]


def test_manifest_path_overrides(tmp_path, monkeypatch) -> None:
    """``skills`` ADDS to the default scan; ``commands``/``agents`` REPLACE."""
    proj = _setup(
        tmp_path,
        monkeypatch,
        manifest={
            "name": "myplug",
            "skills": ["./extra-skills"],
            "commands": "./ops",
            "agents": ["./crew/lead.md"],
        },
    )
    root = Path.home() / ".fluid" / "plugins" / "cache" / "testmp" / "myplug" / "1.2.0"
    _write(
        root / "extra-skills" / "bonus" / "SKILL.md",
        "---\nname: bonus\ndescription: extra\n---\nBODY",
    )
    _write(root / "ops" / "release.md", "Release it")
    _write(root / "crew" / "lead.md", "---\nname: lead\ndescription: leads\n---\nP")

    plugin = discover_plugins(proj)[0]
    assert sorted(s.name for s in plugin.skills) == ["myplug:bonus", "myplug:hello"]
    assert [c.name for c in plugin.commands] == ["myplug:release"]  # deploy.md replaced
    assert [a.name for a in plugin.agents] == ["myplug:lead"]  # reviewer.md replaced


def test_agents_scan_is_recursive(tmp_path, monkeypatch) -> None:
    proj = _setup(tmp_path, monkeypatch)
    root = Path.home() / ".fluid" / "plugins" / "cache" / "testmp" / "myplug" / "1.2.0"
    _write(root / "agents" / "nested" / "deep.md", "---\ndescription: deep\n---\nP")
    names = [a.name for a in discover_plugins(proj)[0].agents]
    assert sorted(names) == ["myplug:deep", "myplug:reviewer"]  # name falls back to the stem


# ─── MCP servers ─────────────────────────────────────────────────────────────


def test_mcp_servers_are_substituted_and_normalized(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("MYPLUG_TOKEN", "s3cret")
    proj = _setup(tmp_path, monkeypatch)
    plugin = discover_plugins(proj)[0]

    local = plugin.mcp_servers["myplug-local"]
    assert local["command"] == "node"
    assert local["args"][0] == f"{plugin.root}/server.js"  # ${CLAUDE_PLUGIN_ROOT}
    assert local["args"][2] == str(proj)  # ${CLAUDE_PROJECT_DIR}
    # ${MYPLUG_TOKEN} from the environment, plus the plugin root the server
    # process is given as Claude Code gives it.
    assert local["env"] == {"TOKEN": "s3cret", "CLAUDE_PLUGIN_ROOT": str(plugin.root)}
    assert "type" not in local  # stdio is fluid's client default

    # http becomes fluid's streamable_http transport.
    assert plugin.mcp_servers["myplug-http"] == {
        "url": "https://example.test/mcp",
        "transport": "streamable_http",
    }


def test_sse_server_is_skipped_with_a_warning(tmp_path, monkeypatch, caplog) -> None:
    proj = _setup(tmp_path, monkeypatch)
    with caplog.at_level(logging.WARNING):
        plugin = discover_plugins(proj)[0]
    assert "myplug-sse" not in plugin.mcp_servers
    assert any("sse" in r.getMessage() for r in caplog.records)


def test_disabled_mcp_servers_are_honored(tmp_path, monkeypatch) -> None:
    proj = _setup(tmp_path, monkeypatch, settings={"disabledMcpServers": ["myplug-local"]})
    plugin = discover_plugins(proj)[0]
    assert set(plugin.mcp_servers) == {"myplug-http"}


def test_inline_manifest_mcp_servers(tmp_path, monkeypatch) -> None:
    """A plugin.json ``mcpServers`` dict is read instead of any .mcp.json."""
    proj = _setup(
        tmp_path,
        monkeypatch,
        manifest={
            "name": "myplug",
            "mcpServers": {"inline": {"command": "npx", "args": ["thing@1"]}},
        },
    )
    plugin = discover_plugins(proj)[0]
    assert plugin.mcp_servers == {
        "inline": {
            "command": "npx", "args": ["thing@1"],
            "env": {"CLAUDE_PLUGIN_ROOT": str(plugin.root)},
        }
    }


def test_manifest_mcp_servers_path(tmp_path, monkeypatch) -> None:
    proj = _setup(
        tmp_path, monkeypatch, manifest={"name": "myplug", "mcpServers": "./config/servers.json"}
    )
    root = Path.home() / ".fluid" / "plugins" / "cache" / "testmp" / "myplug" / "1.2.0"
    _write(
        root / "config" / "servers.json",
        json.dumps({"mcpServers": {"fromfile": {"command": "srv"}}}),
    )
    assert discover_plugins(proj)[0].mcp_servers == {
        "fromfile": {"command": "srv", "env": {"CLAUDE_PLUGIN_ROOT": str(root)}}
    }


def test_malformed_mcp_json_soft_fails(tmp_path, monkeypatch, caplog) -> None:
    proj = _setup(tmp_path, monkeypatch)
    root = Path.home() / ".fluid" / "plugins" / "cache" / "testmp" / "myplug" / "1.2.0"
    (root / ".mcp.json").write_text("{oops", encoding="utf-8")
    with caplog.at_level(logging.WARNING):
        plugin = discover_plugins(proj)[0]
    assert plugin.mcp_servers == {}
    assert [s.name for s in plugin.skills] == ["myplug:hello"]  # rest of the plugin survives


def test_normalize_mcp_server_edge_cases(caplog) -> None:
    assert normalize_mcp_server("s", {"command": "x", "args": ["a"]}) == {
        "command": "x", "args": ["a"]
    }
    with caplog.at_level(logging.WARNING):
        assert normalize_mcp_server("s", {"type": "sse", "url": "u"}) is None
        assert normalize_mcp_server("s", {"type": "http"}) is None  # no url
        assert normalize_mcp_server("s", {"nonsense": 1}) is None  # neither command nor url
    # A bare url with no type is still an http server.
    assert normalize_mcp_server("s", {"url": "u"})["transport"] == "streamable_http"


def test_substitute_vars_leaves_unknown_env_empty(monkeypatch, tmp_path) -> None:
    monkeypatch.delenv("FLUID_NOT_SET_PROBE", raising=False)
    out = substitute_vars(
        {"a": ["${CLAUDE_PLUGIN_ROOT}", {"b": "${FLUID_NOT_SET_PROBE}!"}], "n": 3},
        plugin_root=tmp_path,
        project_dir="/proj",
    )
    assert out == {"a": [str(tmp_path), {"b": "!"}], "n": 3}


def test_plugin_mcp_servers_first_plugin_wins(tmp_path, monkeypatch) -> None:
    proj = _setup(tmp_path, monkeypatch)
    plugins = discover_plugins(proj)
    assert set(plugin_mcp_servers(plugins)) == {"myplug-local", "myplug-http"}


# ─── discovery integration ───────────────────────────────────────────────────


def test_plugin_skills_and_commands_join_discovery(tmp_path, monkeypatch) -> None:
    proj = _setup(tmp_path, monkeypatch)
    _write(
        proj / ".agents" / "skills" / "hello" / "SKILL.md",
        "---\nname: hello\ndescription: the project's own\n---\nBODY",
    )
    _write(proj / ".agents" / "commands" / "deploy.md", "project deploy")

    assert [skill.name for skill in discover_skills(proj)] == ["hello"]
    assert [command.name for command in discover_commands(proj)] == ["deploy"]
    # Opting in appends the enabled plugin's components, namespaced, after the
    # native ones — a plugin can never shadow a project skill or command.
    assert [skill.name for skill in discover_skills(proj, include_plugins=True)] == [
        "hello",
        "myplug:hello",
    ]
    assert [command.name for command in discover_commands(proj, include_plugins=True)] == [
        "deploy",
        "myplug:deploy",
    ]


async def test_skill_builtin_loads_an_enabled_plugin_skill(tmp_path, monkeypatch) -> None:
    """Deferred disclosure: the plugin skill's body loads on demand by name."""
    from fluid.tools.builtins.skill import handler
    from fluid.tools.registry import ToolCtx

    proj = _setup(tmp_path, monkeypatch)
    outcome = await handler({"name": "myplug:hello"}, ToolCtx(cwd=str(proj)))
    assert not outcome.is_error
    assert "SKILL BODY NEVER LOADED" in outcome.text


# ─── NativeLoop seam ─────────────────────────────────────────────────────────


def _loop(cwd, *, mcp_servers=None):
    from fluid.engine.native import NativeLoop
    from fluid.providers.base import ProviderCaps
    from fluid.store.base import MemoryStore
    from fluid.tools.registry import ToolCtx

    class _Provider:
        name = "anthropic"
        caps = ProviderCaps()

        async def stream(self, request):  # pragma: no cover - never streamed
            if False:
                yield None

    return NativeLoop(
        provider=_Provider(),
        store=MemoryStore(),
        model="m",
        ctx=ToolCtx(cwd=str(cwd)),
        mcp_servers=mcp_servers,
    )


def test_native_loop_does_not_merge_provider_plugin_mcp_servers(tmp_path, monkeypatch) -> None:
    proj = _setup(tmp_path, monkeypatch)
    loop = _loop(proj)
    assert loop._mcp_connected is False
    assert loop._mcp_servers == {}


def test_host_supplied_server_wins_a_name_collision(tmp_path, monkeypatch, caplog) -> None:
    proj = _setup(tmp_path, monkeypatch)
    host = {"command": "host-binary"}
    with caplog.at_level(logging.WARNING):
        loop = _loop(proj, mcp_servers={"myplug-local": host})
    assert loop._mcp_servers == {"myplug-local": host}
    assert not [r for r in caplog.records if "duplicate" in r.getMessage()]


def test_native_loop_never_reads_provider_settings(tmp_path, monkeypatch) -> None:
    from fluid.engine.native import NativeLoop
    from fluid.providers.base import ProviderCaps
    from fluid.store.base import MemoryStore
    from fluid.tools.registry import ToolCtx

    proj = _setup(tmp_path, monkeypatch, settings={"env": {"FROM_SETTINGS": "1"}})

    class _Provider:
        name = "anthropic"
        caps = ProviderCaps()

        async def stream(self, request):  # pragma: no cover
            if False:
                yield None

    loop = NativeLoop(
        provider=_Provider(),
        store=MemoryStore(),
        model="m",
        ctx=ToolCtx(cwd=str(proj)),
    )
    assert "settings_env" not in loop.ctx.extra
    assert loop._mcp_servers == {}


def test_build_engine_merges_plugin_mcp_servers(tmp_path, monkeypatch) -> None:
    from fluid.config.loader import BUILTIN_DEFAULTS, build_engine, deep_merge
    from fluid.config.model import HarnessConfig
    from fluid.providers.base import ProviderCaps
    from fluid.store.base import MemoryStore

    proj = _setup(tmp_path, monkeypatch)

    class _Provider:
        name = "anthropic"
        caps = ProviderCaps()

        async def stream(self, request):  # pragma: no cover
            if False:
                yield None

    native = HarnessConfig.from_dict(deep_merge(BUILTIN_DEFAULTS, {"cwd": str(proj)}))
    engine = build_engine(native, MemoryStore(), provider=_Provider())
    # Enabled plugin servers join the lazy-connect set (sse is skipped).
    assert set(engine._mcp_servers) == {"myplug-local", "myplug-http"}

    # A config-declared server of the same name always wins the collision, and
    # dropping the "user" source turns plugin discovery off entirely.
    host = {"transport": "stdio", "command": "host-binary"}
    pinned = HarnessConfig.from_dict(deep_merge(
        BUILTIN_DEFAULTS, {"cwd": str(proj), "mcp": {"myplug-local": host}}
    ))
    engine = build_engine(pinned, MemoryStore(), provider=_Provider())
    assert engine._mcp_servers["myplug-local"] == host

    isolated = HarnessConfig.from_dict(deep_merge(
        BUILTIN_DEFAULTS, {"cwd": str(proj), "setting_sources": ["project", "local"]}
    ))
    engine = build_engine(isolated, MemoryStore(), provider=_Provider())
    assert engine._mcp_servers == {}


@pytest.mark.parametrize("bad", ["", "@mp"])
def test_blank_plugin_refs_are_ignored(tmp_path, monkeypatch, bad) -> None:
    proj = _setup(tmp_path, monkeypatch, enabled=False)
    _write(proj / ".fluid" / "settings.json", json.dumps({"enabledPlugins": {bad: True}}))
    assert discover_plugins(proj) == []


# ─── Skill-tool name resolution ──────────────────────────────────────────────


def _one_skill_plugin(home: Path, plugin: str, skill: str, body: str) -> None:
    """Cache a single-skill plugin under ``testmp`` and enable it."""
    root = home / ".fluid" / "plugins" / "cache" / "testmp" / plugin / "1.0.0"
    _write(
        root / ".claude-plugin" / "plugin.json",
        json.dumps({"name": plugin, "version": "1.0.0"}),
    )
    _write(
        root / "skills" / skill / "SKILL.md",
        f"---\nname: {skill}\ndescription: d\n---\n{body}",
    )


async def test_skill_tool_accepts_the_bare_name_of_a_namespaced_skill(
    tmp_path, monkeypatch
) -> None:
    """A plugin whose skill shares its name is indexed ``unslop:unslop``, and a
    model reading that routinely asks for ``unslop`` — which used to cost a
    failed call before the retry."""
    from fluid.tools.builtins.skill import handler
    from fluid.tools.registry import ToolCtx

    home = tmp_path / "home"
    _set_home(monkeypatch, home)
    _one_skill_plugin(home, "unslop", "unslop", "UNSLOP BODY")
    _write(
        home / ".fluid" / "settings.json",
        json.dumps({"enabledPlugins": {"unslop@testmp": True}}),
    )
    proj = tmp_path / "proj"
    (proj / ".git").mkdir(parents=True)
    assert [s.name for s in discover_skills(proj, include_plugins=True)] == [
        "unslop:unslop"
    ]

    for asked in ("unslop:unslop", "unslop", "UnSlop"):
        out = await handler({"name": asked}, ToolCtx(cwd=str(proj)))
        assert not out.is_error, out.text
        assert "UNSLOP BODY" in out.text


async def test_skill_tool_never_guesses_between_two_namespaces(
    tmp_path, monkeypatch
) -> None:
    from fluid.tools.builtins.skill import handler
    from fluid.tools.registry import ToolCtx

    home = tmp_path / "home"
    _set_home(monkeypatch, home)
    _one_skill_plugin(home, "alpha", "review", "ALPHA BODY")
    _one_skill_plugin(home, "beta", "review", "BETA BODY")
    _write(
        home / ".fluid" / "settings.json",
        json.dumps({"enabledPlugins": {"alpha@testmp": True, "beta@testmp": True}}),
    )
    proj = tmp_path / "proj"
    (proj / ".git").mkdir(parents=True)

    out = await handler({"name": "review"}, ToolCtx(cwd=str(proj)))
    assert out.is_error
    assert "alpha:review" in out.text and "beta:review" in out.text
