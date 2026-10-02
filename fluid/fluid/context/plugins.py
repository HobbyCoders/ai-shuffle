"""Fluid plugin loading — skills, commands, agents, MCP servers.

Ruling CE5. Plugins live in fluid's own home (``~/.fluid/plugins``). The
enabled set comes from the merged ``enabledPlugins`` map in the fluid settings
stack (:mod:`fluid.context.settings_stack`); provider homes (``~/.claude``) are
never read at runtime.

Package formats are read by :mod:`fluid.context.plugin_formats` — Claude Code
(``.claude-plugin/plugin.json``) and Agent Plugins / OpenAI (root
``plugin.json`` or ``.codex-plugin/plugin.json``) — so an installed Claude
plugin can be copied in wholesale by the settings importer and just works, and
an Agent Plugins package loads its skills and MCP servers the same way. This
module only resolves WHERE a plugin lives and what the runtime takes from it.

On-disk layout, in resolution order:

  1. ``~/.fluid/plugins/cache/<marketplace>/<plugin>/<version>/`` — the
     installed copy; the highest version wins (numeric-aware, so ``1.10.0``
     beats ``1.2.0``; non-numeric directory names like ``unknown`` sort below
     every numeric one and are compared lexicographically among themselves).
  2. ``~/.fluid/plugins/marketplaces/<marketplace>/<plugin>/`` — a marketplace
     clone with the plugin in a subdirectory.
  3. ``~/.fluid/plugins/marketplaces/<marketplace>/`` — the clone root itself,
     when the marketplace *is* the plugin (its manifest names it).

A missing or malformed manifest is not fatal (CE11) — the plugin still loads on
directory convention alone.

Components the runtime loads:

  * skills   ``skills/*/SKILL.md``    → :class:`~fluid.context.instructions.Skill`
  * commands ``commands/*.md``        → :class:`~fluid.context.instructions.Command`
  * agents   ``agents/**/*.md``       → :class:`PluginAgent` (body read by ``agents.discover_agents``)
  * MCP      ``.mcp.json`` / ``mcp.json`` or the manifest's ``mcpServers`` key

All three component kinds are namespaced ``<plugin>:<name>`` — Claude Code's own
convention, and what keeps a plugin from shadowing a user's project skill.
Manifest path overrides are honored: ``skills`` ADDS to the default scan while
``commands`` and ``agents`` REPLACE theirs.

Everything else a plugin declares (hooks, which the app's hook service runs;
LSP servers, output styles, ChatGPT apps, userConfig, dependencies) is listed
on the plugin's :class:`~fluid.context.plugin_formats.PluginRecord` with its
support status, and not loaded here.

Plugins from the ``authored`` source (written by the ``save_plugin`` tool) load
only as the exact content the owner approved
(:func:`fluid.context.plugin_authoring.is_approved`).

Nothing here raises: a broken plugin is skipped with a ``logging`` warning and
never takes the rest of discovery down with it (CE11).
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from fluid.context.settings_stack import (
    SETTING_SOURCES,
    enabled_plugins,
    load_settings_stack,
    mcp_toggles,
)
from fluid.context.instructions import Command, Skill
from fluid.context.plugin_authoring import AUTHORED_SOURCE, is_approved
from fluid.context.plugin_formats import (  # noqa: F401 — re-exported API
    FORMAT_AGENT_PLUGINS,
    FORMAT_CLAUDE,
    PluginAgent,
    PluginComponent,
    PluginRecord,
    _md_files,
    _rel_paths,
    _resolve_under,
    _skill_files,
    find_manifest,
    normalize_mcp_server,
    raw_mcp_servers,
    read_manifest,
    read_plugin,
    substitute_vars,
)

log = logging.getLogger(__name__)

# A bare ``name`` in enabledPlugins means Anthropic's own marketplace.
DEFAULT_MARKETPLACE = "claude-plugins-official"


def _raw_mcp_servers(root: Path, manifest: dict[str, Any]) -> dict[str, Any]:
    """Claude-format server declarations (kept for existing callers)."""
    return raw_mcp_servers(root, manifest, FORMAT_CLAUDE)


# ─── records ─────────────────────────────────────────────────────────────────


@dataclass(frozen=True, kw_only=True)
class Plugin:
    """One resolved plugin and everything fluid takes from it."""

    name: str
    marketplace: str
    root: Path
    version: str | None = None
    manifest: dict[str, Any] = field(default_factory=dict)
    skills: list[Skill] = field(default_factory=list)
    commands: list[Command] = field(default_factory=list)
    agents: list[PluginAgent] = field(default_factory=list)
    mcp_servers: dict[str, dict[str, Any]] = field(default_factory=dict)
    format: str = FORMAT_CLAUDE
    components: list[PluginComponent] = field(default_factory=list)

    @property
    def ref(self) -> str:
        """The ``<plugin>@<marketplace>`` key this plugin was enabled under."""
        return f"{self.name}@{self.marketplace}"


# ─── root resolution ─────────────────────────────────────────────────────────


def plugins_home() -> Path:
    """``~/.fluid/plugins`` — fluid's plugin install root (Claude package layout)."""
    return Path.home() / ".fluid" / "plugins"


def split_plugin_ref(ref: str) -> tuple[str, str]:
    """``"name@marketplace"`` → ``(name, marketplace)``; a bare name defaults."""
    name, sep, marketplace = ref.partition("@")
    name = name.strip()
    marketplace = marketplace.strip() if sep else ""
    return name, marketplace or DEFAULT_MARKETPLACE


def _plain_component(value: str) -> bool:
    """One ordinary path segment: no separators, no ``.``/``..``, no NUL, no ``:``.

    Plugin and marketplace names come from ``enabledPlugins`` keys — a
    project-scope settings file included — and are joined onto the plugins
    root; a ``../`` in either would load a "plugin" (and start its MCP
    servers) from anywhere on disk. On Windows a drive prefix (``D:x``)
    replaces the root it is joined onto, so ``:`` is refused too.
    """
    return (
        bool(value)
        and value not in (".", "..")
        and "/" not in value
        and "\\" not in value
        and "\0" not in value
        and ":" not in value
    )


def _version_key(name: str) -> tuple[int, tuple[int, ...], str]:
    """Sort key for a version directory name.

    Dotted all-numeric names compare component-wise (so ``1.10.0`` > ``1.2.0``);
    anything else — Claude Code writes ``unknown`` for unversioned installs —
    sorts below every numeric version and falls back to lexicographic order.
    """
    parts = name.split(".")
    if parts and all(p.isdigit() for p in parts):
        return (1, tuple(int(p) for p in parts), "")
    return (0, (), name)


def _latest_version_dir(base: Path) -> Path | None:
    try:
        versions = [d for d in base.iterdir() if d.is_dir()]
    except OSError as exc:
        log.warning("cannot list plugin cache dir %s: %s", base, exc)
        return None
    if not versions:
        return None
    return max(versions, key=lambda d: _version_key(d.name))


def resolve_plugin_root(name: str, marketplace: str, *, home: Path | None = None) -> tuple[Path, str | None] | None:
    """Where ``name@marketplace`` lives on disk, with its version when known.

    Returns ``None`` when the plugin is enabled in settings but not installed —
    a stale ``enabledPlugins`` entry must not break the session (CE11).
    """
    home = plugins_home() if home is None else home
    if not (_plain_component(name) and _plain_component(marketplace)):
        log.warning("refusing plugin ref %r@%r: not a plain name", name, marketplace)
        return None

    cache = home / "cache" / marketplace / name
    if cache.is_dir():
        version_dir = _latest_version_dir(cache)
        if version_dir is not None:
            return version_dir, version_dir.name

    clone = home / "marketplaces" / marketplace / name
    if clone.is_dir():
        return clone, None

    # The marketplace repo IS the plugin: accept its root only when a manifest
    # there actually names this plugin (or names nothing at all).
    mp_root = home / "marketplaces" / marketplace
    if mp_root.is_dir() and find_manifest(mp_root)[0] is not None:
        manifest_name = read_manifest(mp_root).get("name")
        if not manifest_name or str(manifest_name) == name:
            return mp_root, None
    return None


def plugin_data_dir(name: str, marketplace: str, *, home: Path | None = None) -> Path:
    """The stable per-plugin data folder ``${CLAUDE_PLUGIN_DATA}``/``${PLUGIN_DATA}``
    point at: ``~/.fluid/plugins/data/<name>@<marketplace>``. It survives plugin
    updates (the version directory does not)."""
    home = plugins_home() if home is None else home
    return home / "data" / f"{name}@{marketplace}"


# ─── MCP servers ─────────────────────────────────────────────────────────────


def _mcp_servers_for(
    root: Path,
    manifest: dict[str, Any],
    *,
    project_dir: Path | str,
    disabled: set[str],
    fmt: str = FORMAT_CLAUDE,
    servers: dict[str, Any] | None = None,
    data_dir: Path | None = None,
) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    declared = raw_mcp_servers(root, manifest, fmt) if servers is None else servers
    root_vars = ("CLAUDE_PLUGIN_ROOT",) if fmt == FORMAT_CLAUDE else ("PLUGIN_ROOT", "CLAUDE_PLUGIN_ROOT")
    data_vars = ("CLAUDE_PLUGIN_DATA",) if fmt == FORMAT_CLAUDE else ("PLUGIN_DATA", "CLAUDE_PLUGIN_DATA")
    for name, raw in declared.items():
        if not isinstance(raw, dict):
            log.warning("skipping MCP server %r: expected a JSON object", name)
            continue
        if name in disabled:
            log.debug("MCP server %r disabled by settings", name)
            continue
        expanded = substitute_vars(
            raw, plugin_root=root, project_dir=project_dir, plugin_data=data_dir
        )
        normalized = normalize_mcp_server(str(name), expanded)
        if normalized is None:
            continue
        if normalized.get("transport", "stdio") == "stdio":
            # Both formats export the plugin root (and data folder) to the
            # server process as well as substituting them in the config; a
            # server that reads the variable at runtime (to find its own data
            # files) needs both.
            env = dict(normalized.get("env") or {})
            for var in root_vars:
                env.setdefault(var, str(root))
            if data_dir is not None:
                for var in data_vars:
                    env.setdefault(var, str(data_dir))
            normalized = {**normalized, "env": env}
        out[str(name)] = normalized
    if data_dir is not None and out:
        # Created on first use, like Claude Code, so a server can write to it.
        try:
            data_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            log.warning("cannot create plugin data folder %s: %s", data_dir, exc)
    return out


# ─── discovery ───────────────────────────────────────────────────────────────


def load_plugin(
    name: str,
    marketplace: str,
    *,
    cwd: Path | str | os.PathLike[str] = ".",
    disabled_mcp: set[str] | None = None,
    home: Path | None = None,
) -> Plugin | None:
    """Resolve and read one plugin. ``None`` when it is not installed."""
    resolved = resolve_plugin_root(name, marketplace, home=home)
    if resolved is None:
        log.warning(
            "enabled plugin %s@%s is not installed under %s — skipping",
            name, marketplace, plugins_home() if home is None else home,
        )
        return None
    root, version = resolved
    if marketplace == AUTHORED_SOURCE and not is_approved(name, root, home=home):
        # An agent-written plugin loads only as the exact content the owner
        # approved; a new or edited version waits for review (fail closed).
        log.warning(
            "authored plugin %s@%s (version %s) is not approved — skipping",
            name, marketplace, root.name,
        )
        return None
    record = read_plugin(root, name=name, source=marketplace, version=version)
    skipped = [f"{c.kind}:{c.name}" for c in record.unsupported()]
    if skipped:
        log.debug("plugin %s: not loading %s", record.name, skipped)
    data_dir = plugin_data_dir(name, marketplace, home=home)
    return Plugin(
        name=record.name,
        marketplace=marketplace,
        root=root,
        version=record.version,
        manifest=record.manifest,
        skills=record.skills,
        commands=record.commands,
        agents=record.agents,
        mcp_servers=_mcp_servers_for(
            root,
            record.manifest,
            project_dir=cwd,
            disabled=disabled_mcp or set(),
            fmt=record.format,
            servers=record.mcp_servers,
            data_dir=data_dir,
        ),
        format=record.format,
        components=record.components,
    )


def discover_plugins(
    cwd: str | os.PathLike[str],
    *,
    settings: dict[str, Any] | None = None,
    sources: tuple[str, ...] | list[str] = SETTING_SOURCES,
    home: Path | None = None,
) -> list[Plugin]:
    """Every enabled, installed plugin visible from ``cwd``.

    ``settings`` defaults to the merged fluid settings stack for ``cwd``
    restricted to ``sources``; pass an already-loaded one to avoid re-reading
    it. Results are sorted by ``name@marketplace`` so the skills/commands
    indexes they feed stay byte-stable across runs (CE7).

    Never raises: one unreadable plugin is skipped, the rest still load (CE11).
    """
    resolved_settings = (
        load_settings_stack(cwd, sources=tuple(sources)) if settings is None else settings
    )
    enabled = [ref for ref, on in enabled_plugins(resolved_settings).items() if on]
    if not enabled:
        return []
    _, disabled_servers = mcp_toggles(resolved_settings)
    disabled = set(disabled_servers)
    out: list[Plugin] = []
    for ref in sorted(enabled):
        name, marketplace = split_plugin_ref(ref)
        if not name:
            continue
        try:
            plugin = load_plugin(name, marketplace, cwd=cwd, disabled_mcp=disabled, home=home)
        except Exception as exc:  # noqa: BLE001 — one bad plugin never breaks the rest
            log.warning("skipping plugin %s: %s", ref, exc)
            continue
        if plugin is not None:
            out.append(plugin)
    return out


# ─── aggregate views ─────────────────────────────────────────────────────────


def plugin_skills(plugins: list[Plugin]) -> list[Skill]:
    return [s for p in plugins for s in p.skills]


def plugin_commands(plugins: list[Plugin]) -> list[Command]:
    return [c for p in plugins for c in p.commands]


def plugin_mcp_servers(plugins: list[Plugin]) -> dict[str, dict[str, Any]]:
    """All plugin servers, name-keyed. The first plugin to claim a name keeps
    it — discovery order is deterministic, so the winner is stable."""
    out: dict[str, dict[str, Any]] = {}
    for plugin in plugins:
        for name, cfg in plugin.mcp_servers.items():
            if name in out:
                log.warning("duplicate plugin MCP server %r (kept the first)", name)
                continue
            out[name] = cfg
    return out
