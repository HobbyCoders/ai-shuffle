"""Fluid plugin loading — skills, commands, agents, MCP servers.

Ruling CE5. Plugins live in fluid's own home (``~/.fluid/plugins``) using the
Claude Code plugin package format, so an installed Claude plugin can be copied
in wholesale by the settings importer and just works. The enabled set comes
from the merged ``enabledPlugins`` map in the fluid settings stack
(:mod:`fluid.context.settings_stack`); provider homes (``~/.claude``) are never
read at runtime.

On-disk layout, in resolution order:

  1. ``~/.fluid/plugins/cache/<marketplace>/<plugin>/<version>/`` — the
     installed copy; the highest version wins (numeric-aware, so ``1.10.0``
     beats ``1.2.0``; non-numeric directory names like ``unknown`` sort below
     every numeric one and are compared lexicographically among themselves).
  2. ``~/.fluid/plugins/marketplaces/<marketplace>/<plugin>/`` — a marketplace
     git clone with the plugin in a subdirectory.
  3. ``~/.fluid/plugins/marketplaces/<marketplace>/`` — the clone root itself,
     when the marketplace *is* the plugin (its manifest names it).

Manifest: ``.claude-plugin/plugin.json``, else ``plugin.json`` at the root. A
missing or malformed manifest is not fatal (CE11) — the plugin still loads on
directory convention alone.

Components (v1):

  * skills   ``skills/*/SKILL.md``    → :class:`~fluid.context.instructions.Skill`
  * commands ``commands/*.md``        → :class:`~fluid.context.instructions.Command`
  * agents   ``agents/**/*.md``       → :class:`PluginAgent` (body unread; W4 consumes)
  * MCP      ``.mcp.json`` or the manifest's ``mcpServers`` key

All three component kinds are namespaced ``<plugin>:<name>`` — Claude Code's own
convention, and what keeps a plugin from shadowing a user's project skill.
Manifest path overrides are honored: ``skills`` ADDS to the default scan while
``commands`` and ``agents`` REPLACE theirs.

Out of scope for v1 (log-and-ignore): LSP servers, themes, output styles,
plugin hooks, plugin settings.json, userConfig, dependencies.

Nothing here raises: a broken plugin is skipped with a ``logging`` warning and
never takes the rest of discovery down with it (CE11).
"""

from __future__ import annotations

import json
import logging
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from fluid.context.settings_stack import (
    SETTING_SOURCES,
    enabled_plugins,
    load_settings_stack,
    mcp_toggles,
)
from fluid.context.instructions import Command, Skill, parse_frontmatter

log = logging.getLogger(__name__)

# A bare ``name`` in enabledPlugins means Anthropic's own marketplace.
DEFAULT_MARKETPLACE = "claude-plugins-official"

# ``${CLAUDE_PLUGIN_ROOT}`` / ``${CLAUDE_PROJECT_DIR}`` / ``${ANY_ENV_VAR}``.
_VAR_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")

_MANIFEST_NAMES = (Path(".claude-plugin") / "plugin.json", Path("plugin.json"))

# Manifest keys v1 knowingly does not act on (CE5: log and ignore, never fail).
_IGNORED_KEYS = (
    "lspServers", "themes", "outputStyles", "hooks", "settings", "userConfig", "dependencies",
)


# ─── records ─────────────────────────────────────────────────────────────────


@dataclass(frozen=True, kw_only=True)
class PluginAgent:
    """A plugin-provided subagent definition, advertised but not read.

    Same progressive-disclosure shape as :class:`Skill`: the frontmatter's name
    and description are parsed at discovery time, the body (the agent's system
    prompt) is left on disk for W4's agent loader to read.
    """

    name: str  # namespaced: "<plugin>:<agent>"
    description: str
    path: str
    scope: str = "plugin"
    body_loaded: bool = False


@dataclass(frozen=True, kw_only=True)
class Plugin:
    """One resolved Claude Code plugin and everything fluid takes from it."""

    name: str
    marketplace: str
    root: Path
    version: str | None = None
    manifest: dict[str, Any] = field(default_factory=dict)
    skills: list[Skill] = field(default_factory=list)
    commands: list[Command] = field(default_factory=list)
    agents: list[PluginAgent] = field(default_factory=list)
    mcp_servers: dict[str, dict[str, Any]] = field(default_factory=dict)

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


def _manifest_path(root: Path) -> Path | None:
    for rel in _MANIFEST_NAMES:
        candidate = root / rel
        if candidate.is_file():
            return candidate
    return None


def read_manifest(root: Path) -> dict[str, Any]:
    """The plugin manifest at ``root``, or ``{}`` when missing or malformed."""
    path = _manifest_path(root)
    if path is None:
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        log.warning("skipping malformed plugin manifest %s: %s", path, exc)
        return {}
    if not isinstance(data, dict):
        log.warning("skipping plugin manifest %s: expected a JSON object", path)
        return {}
    return data


def resolve_plugin_root(name: str, marketplace: str) -> tuple[Path, str | None] | None:
    """Where ``name@marketplace`` lives on disk, with its version when known.

    Returns ``None`` when the plugin is enabled in settings but not installed —
    a stale ``enabledPlugins`` entry must not break the session (CE11).
    """
    home = plugins_home()

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
    if mp_root.is_dir() and _manifest_path(mp_root) is not None:
        manifest_name = read_manifest(mp_root).get("name")
        if not manifest_name or str(manifest_name) == name:
            return mp_root, None
    return None


# ─── component scanning ──────────────────────────────────────────────────────


def _rel_paths(value: Any) -> list[str]:
    """A manifest component override as a list of relative paths.

    The on-disk convention writes ``"./commands/x.md"`` or a list of those; a
    single string is accepted too.
    """
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return [v for v in value if isinstance(v, str)]
    return []


def _resolve_under(root: Path, rel: str) -> Path:
    """Resolve a manifest-relative path (``./skills`` style) under ``root``."""
    cleaned = rel.strip()
    for prefix in ("./", ".\\"):
        if cleaned.startswith(prefix):
            cleaned = cleaned[len(prefix):]
            break
    return root / cleaned if cleaned else root


def _skill_files(root: Path, manifest: dict[str, Any]) -> list[Path]:
    """Every ``SKILL.md`` this plugin offers.

    The default ``skills/`` scan always runs — a manifest ``skills`` entry ADDS
    directories, it does not replace the convention. An entry may point at a
    directory of skills, at one skill directory, or straight at a SKILL.md.
    """
    found: list[Path] = []
    seen: set[str] = set()

    def add(path: Path) -> None:
        key = str(path).lower()
        if path.is_file() and key not in seen:
            seen.add(key)
            found.append(path)

    def scan(base: Path) -> None:
        if base.is_file():
            add(base)
            return
        if not base.is_dir():
            return
        direct = base / "SKILL.md"
        if direct.is_file():
            add(direct)
            return
        for skill_md in sorted(base.glob("*/SKILL.md")):
            add(skill_md)

    scan(root / "skills")
    for rel in _rel_paths(manifest.get("skills")):
        scan(_resolve_under(root, rel))
    return found


def _md_files(root: Path, manifest: dict[str, Any], key: str, *, recursive: bool) -> list[Path]:
    """Markdown component files — manifest override REPLACES the default dir."""
    overrides = _rel_paths(manifest.get(key))
    bases = [_resolve_under(root, rel) for rel in overrides] if overrides else [root / key]
    found: list[Path] = []
    seen: set[str] = set()
    for base in bases:
        if base.is_file():
            candidates = [base]
        elif base.is_dir():
            candidates = sorted(base.rglob("*.md") if recursive else base.glob("*.md"))
        else:
            continue
        for path in candidates:
            resolved = str(path).lower()
            if resolved not in seen:
                seen.add(resolved)
                found.append(path)
    return found


def _read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return ""


def _skills_for(plugin_name: str, root: Path, manifest: dict[str, Any]) -> list[Skill]:
    out: list[Skill] = []
    for skill_md in _skill_files(root, manifest):
        fields, _ = parse_frontmatter(_read(skill_md))
        local = fields.get("name") or skill_md.parent.name
        out.append(
            Skill(
                name=f"{plugin_name}:{local}",
                description=fields.get("description", ""),
                path=str(skill_md),
                scope="plugin",
                body_loaded=False,
            )
        )
    return out


def _commands_for(plugin_name: str, root: Path, manifest: dict[str, Any]) -> list[Command]:
    return [
        Command(name=f"{plugin_name}:{path.stem}", path=str(path), scope="plugin")
        for path in _md_files(root, manifest, "commands", recursive=False)
    ]


def _agents_for(plugin_name: str, root: Path, manifest: dict[str, Any]) -> list[PluginAgent]:
    out: list[PluginAgent] = []
    for path in _md_files(root, manifest, "agents", recursive=True):
        fields, _ = parse_frontmatter(_read(path))
        local = fields.get("name") or path.stem
        out.append(
            PluginAgent(
                name=f"{plugin_name}:{local}",
                description=fields.get("description", ""),
                path=str(path),
            )
        )
    return out


# ─── MCP servers ─────────────────────────────────────────────────────────────


def substitute_vars(
    value: Any,
    *,
    plugin_root: Path | None,
    project_dir: Path | str,
    env: Mapping[str, str] | None = None,
) -> Any:
    """Expand ``${...}`` tokens through a server config, recursively.

    ``${CLAUDE_PLUGIN_ROOT}`` and ``${CLAUDE_PROJECT_DIR}`` are the two the
    plugin format defines; anything else reads ``env`` — the process
    environment by default, or the caller's own map (the loader passes the
    process environment overlaid with the settings stack's ``env`` for
    ``mcp.json``). An absent variable expands to the empty string, matching
    Claude Code. ``os.environ`` is only read, never written. ``plugin_root``
    is ``None`` outside a plugin, where ``${CLAUDE_PLUGIN_ROOT}`` is looked up
    like any other variable.
    """
    lookup = os.environ if env is None else env
    if isinstance(value, str):

        def repl(match: re.Match[str]) -> str:
            var = match.group(1)
            if var == "CLAUDE_PLUGIN_ROOT" and plugin_root is not None:
                return str(plugin_root)
            if var == "CLAUDE_PROJECT_DIR":
                return str(project_dir)
            return lookup.get(var, "")

        return _VAR_RE.sub(repl, value)
    if isinstance(value, dict):
        return {
            k: substitute_vars(v, plugin_root=plugin_root, project_dir=project_dir, env=env)
            for k, v in value.items()
        }
    if isinstance(value, list):
        return [
            substitute_vars(v, plugin_root=plugin_root, project_dir=project_dir, env=env)
            for v in value
        ]
    return value


def _raw_mcp_servers(root: Path, manifest: dict[str, Any]) -> dict[str, Any]:
    """The plugin's declared servers: an inline ``mcpServers`` dict, a path to a
    JSON file, or the conventional ``.mcp.json`` at the plugin root."""
    declared = manifest.get("mcpServers")
    if isinstance(declared, dict):
        return declared
    path: Path | None = None
    if isinstance(declared, str) and declared.strip():
        path = _resolve_under(root, declared)
    else:
        default = root / ".mcp.json"
        path = default if default.is_file() else None
    if path is None:
        return {}
    if not path.is_file():
        log.warning("plugin MCP config not found: %s", path)
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        log.warning("skipping malformed plugin MCP config %s: %s", path, exc)
        return {}
    if not isinstance(data, dict):
        log.warning("skipping plugin MCP config %s: expected a JSON object", path)
        return {}
    servers = data.get("mcpServers", data)
    return servers if isinstance(servers, dict) else {}


def normalize_mcp_server(name: str, cfg: dict[str, Any]) -> dict[str, Any] | None:
    """Map a Claude-format server entry onto fluid's client config shape.

    stdio entries pass through (command/args/env/cwd); ``type: "http"`` becomes
    fluid's ``streamable_http`` transport; ``type: "sse"`` is skipped with a
    warning — fluid's MCP client has no SSE transport, and silently downgrading
    it would connect to the wrong endpoint.
    """
    kind = str(cfg.get("type") or cfg.get("transport") or "").strip().lower()
    out = {k: v for k, v in cfg.items() if k not in ("type", "transport")}
    if kind == "sse":
        log.warning("skipping MCP server %r: sse transport is not supported", name)
        return None
    if kind in ("http", "streamable_http") or (not kind and "url" in cfg):
        if not out.get("url"):
            log.warning("skipping MCP server %r: http entry has no url", name)
            return None
        out["transport"] = "streamable_http"
        return out
    if not out.get("command"):
        log.warning("skipping MCP server %r: no command and no url", name)
        return None
    return out


def _mcp_servers_for(
    root: Path,
    manifest: dict[str, Any],
    *,
    project_dir: Path | str,
    disabled: set[str],
) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for name, raw in _raw_mcp_servers(root, manifest).items():
        if not isinstance(raw, dict):
            log.warning("skipping MCP server %r: expected a JSON object", name)
            continue
        if name in disabled:
            log.debug("MCP server %r disabled by settings", name)
            continue
        expanded = substitute_vars(raw, plugin_root=root, project_dir=project_dir)
        normalized = normalize_mcp_server(str(name), expanded)
        if normalized is not None:
            out[str(name)] = normalized
    return out


# ─── discovery ───────────────────────────────────────────────────────────────


def load_plugin(
    name: str,
    marketplace: str,
    *,
    cwd: Path | str | os.PathLike[str] = ".",
    disabled_mcp: set[str] | None = None,
) -> Plugin | None:
    """Resolve and read one plugin. ``None`` when it is not installed."""
    resolved = resolve_plugin_root(name, marketplace)
    if resolved is None:
        log.warning(
            "enabled plugin %s@%s is not installed under %s — skipping",
            name, marketplace, plugins_home(),
        )
        return None
    root, version = resolved
    manifest = read_manifest(root)
    plugin_name = str(manifest.get("name") or name or root.name)
    ignored = [key for key in _IGNORED_KEYS if key in manifest]
    if ignored:
        log.debug("plugin %s: ignoring unsupported manifest keys %s", plugin_name, ignored)
    declared_version = manifest.get("version") or version
    return Plugin(
        name=plugin_name,
        marketplace=marketplace,
        root=root,
        version=str(declared_version) if declared_version else None,
        manifest=manifest,
        skills=_skills_for(plugin_name, root, manifest),
        commands=_commands_for(plugin_name, root, manifest),
        agents=_agents_for(plugin_name, root, manifest),
        mcp_servers=_mcp_servers_for(
            root, manifest, project_dir=cwd, disabled=disabled_mcp or set()
        ),
    )


def discover_plugins(
    cwd: str | os.PathLike[str],
    *,
    settings: dict[str, Any] | None = None,
    sources: tuple[str, ...] | list[str] = SETTING_SOURCES,
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
            plugin = load_plugin(name, marketplace, cwd=cwd, disabled_mcp=disabled)
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


def plugin_agents(plugins: list[Plugin]) -> list[PluginAgent]:
    return [a for p in plugins for a in p.agents]


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
