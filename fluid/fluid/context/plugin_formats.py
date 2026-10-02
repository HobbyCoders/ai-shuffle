"""One reader for every plugin package format fluid and the app understand.

Plugins stay on disk in their native layout and are normalized here, when they
are read, into one :class:`PluginRecord`. Both the runtime loader
(:mod:`fluid.context.plugins`) and the app's plugin management service read
plugins through this module, so a plugin lists the same components in the UI
that it loads at runtime.

Formats:

  * ``claude`` — Claude Code: ``.claude-plugin/plugin.json`` (optional; only
    ``name`` is required), ``skills/``, ``commands/``, ``agents/``,
    ``hooks/hooks.json`` and ``.mcp.json`` or an inline ``mcpServers``. A
    directory with no manifest at all is read on these conventions too.
  * ``agent-plugins`` — Agent Plugins 1.0, which OpenAI's ChatGPT/Codex
    plugins follow: a root ``plugin.json`` (or the older
    ``.codex-plugin/plugin.json``), ``skills/``, ``mcp.json``, and
    vendor fields under ``extensions.<reverse-domain>``. OpenAI's display
    metadata (``interface``) is read from ``extensions["com.openai"]`` or the
    top level, and ChatGPT-hosted apps (``.app.json``) are reported as
    unsupported. Commands and agents may be declared under
    ``extensions["ai.shuffle"]``; the Claude-convention ``commands/`` and
    ``agents/`` directories are still read, so a plugin that used to load
    through a root ``plugin.json`` keeps every component it had.

Every component carries a ``support`` value (``loaded``, ``unsupported`` or
``needs_config``) and a reason, so callers can say what will not run instead of
silently dropping it.

``${CLAUDE_PLUGIN_ROOT}`` and ``${PLUGIN_ROOT}`` resolve to the same plugin
root; ``${CLAUDE_PLUGIN_DATA}`` and ``${PLUGIN_DATA}`` resolve to the plugin's
stable data folder (:func:`plugin_data_dir`).

Nothing here raises on bad input: a malformed file reads as empty with a
``logging`` warning (CE11).
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from fluid.context.instructions import Command, Skill, _read, parse_frontmatter

log = logging.getLogger(__name__)

FORMAT_CLAUDE = "claude"
FORMAT_AGENT_PLUGINS = "agent-plugins"
FORMAT_SHUFFLE = "shuffle"

SUPPORT_LOADED = "loaded"
SUPPORT_UNSUPPORTED = "unsupported"
SUPPORT_NEEDS_CONFIG = "needs_config"

# Where each format keeps its manifest, in the order they are tried. The
# Claude manifest wins when a plugin ships both (an exported dual-format
# plugin), which is also what the loader did before this module existed.
_CLAUDE_MANIFEST = Path(".claude-plugin") / "plugin.json"
_CODEX_MANIFEST = Path(".codex-plugin") / "plugin.json"
_ROOT_MANIFEST = Path("plugin.json")
_MANIFESTS: tuple[tuple[Path, str], ...] = (
    (_CLAUDE_MANIFEST, FORMAT_CLAUDE),
    (_ROOT_MANIFEST, FORMAT_AGENT_PLUGINS),
    (_CODEX_MANIFEST, FORMAT_AGENT_PLUGINS),
)

_CLAUDE_MARKETPLACE = Path(".claude-plugin") / "marketplace.json"
_AGENTS_MARKETPLACE = Path(".agents") / "plugins" / "marketplace.json"

OPENAI_EXTENSION = "com.openai"
SHUFFLE_EXTENSION = "ai.shuffle"

# ``${CLAUDE_PLUGIN_ROOT}`` / ``${PLUGIN_ROOT}`` / ``${ANY_ENV_VAR}``.
_VAR_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")
_ROOT_VARS = frozenset({"CLAUDE_PLUGIN_ROOT", "PLUGIN_ROOT"})
_DATA_VARS = frozenset({"CLAUDE_PLUGIN_DATA", "PLUGIN_DATA"})


# ─── records ─────────────────────────────────────────────────────────────────


@dataclass(frozen=True, kw_only=True)
class PluginAgent:
    """A plugin-provided subagent definition, advertised but not read.

    Same progressive-disclosure shape as :class:`Skill`: the frontmatter's name
    and description are parsed at discovery time, the body (the agent's system
    prompt) is left on disk for :func:`fluid.context.agents.discover_agents`.
    """

    name: str  # namespaced: "<plugin>:<agent>"
    description: str
    path: str
    scope: str = "plugin"
    body_loaded: bool = False


@dataclass(frozen=True, kw_only=True)
class PluginComponent:
    """One thing a plugin offers, and whether this app can run it."""

    kind: str  # skill | command | agent | mcp_server | hook | app | lsp_server | ...
    name: str
    support: str = SUPPORT_LOADED
    reason: str = ""
    path: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "name": self.name,
            "support": self.support,
            "reason": self.reason,
        }


@dataclass(frozen=True, kw_only=True)
class PluginRecord:
    """A plugin normalized from whichever format it ships in.

    ``name`` is the plugin's own name (the manifest's, else the name it was
    installed under, else the directory's); ``source`` is the marketplace or
    source it was installed from, so ``id`` keeps the ``name@source`` IDs the
    settings files already use. ``mcp_servers`` is the raw declaration —
    variables are expanded by the runtime, which knows the project directory.
    """

    name: str
    source: str
    root: Path
    format: str
    manifest: dict[str, Any] = field(default_factory=dict)
    manifest_path: Path | None = None
    version: str | None = None
    display_name: str = ""
    description: str = ""
    author: str | None = None
    icon: str | None = None
    skills: list[Skill] = field(default_factory=list)
    commands: list[Command] = field(default_factory=list)
    agents: list[PluginAgent] = field(default_factory=list)
    mcp_servers: dict[str, dict[str, Any]] = field(default_factory=dict)
    components: list[PluginComponent] = field(default_factory=list)

    @property
    def id(self) -> str:
        return f"{self.name}@{self.source}" if self.source else self.name

    def has(self, kind: str) -> bool:
        """Whether the plugin declares any component of ``kind``."""
        return any(c.kind == kind for c in self.components)

    def unsupported(self) -> list[PluginComponent]:
        return [c for c in self.components if c.support != SUPPORT_LOADED]


# ─── manifests ───────────────────────────────────────────────────────────────


def _read_json_object(path: Path, what: str) -> dict[str, Any] | None:
    """A JSON object file, or ``None`` (with a warning) when unreadable."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        log.warning("skipping malformed %s %s: %s", what, path, exc)
        return None
    if not isinstance(data, dict):
        log.warning("skipping %s %s: expected a JSON object", what, path)
        return None
    return data


def find_manifest(root: Path) -> tuple[Path | None, str]:
    """``(manifest path, format)`` for the plugin at ``root``.

    A directory with no manifest is a Claude-convention plugin (the manifest is
    optional there), so the format is ``claude`` and the path ``None``.
    """
    for rel, fmt in _MANIFESTS:
        candidate = root / rel
        if candidate.is_file():
            return candidate, fmt
    return None, FORMAT_CLAUDE


def detect_format(root: Path) -> str:
    """``claude`` or ``agent-plugins``, from which manifest file is present."""
    return find_manifest(root)[1]


def read_manifest(root: Path) -> dict[str, Any]:
    """The plugin manifest at ``root``, or ``{}`` when missing or malformed."""
    path, _ = find_manifest(root)
    if path is None:
        return {}
    return _read_json_object(path, "plugin manifest") or {}


def _extension(manifest: Mapping[str, Any], key: str) -> dict[str, Any]:
    extensions = manifest.get("extensions")
    if not isinstance(extensions, dict):
        return {}
    value = extensions.get(key)
    return value if isinstance(value, dict) else {}


def _openai_interface(manifest: Mapping[str, Any]) -> dict[str, Any]:
    """OpenAI display metadata: the extension block, else the Codex top level."""
    nested = _extension(manifest, OPENAI_EXTENSION).get("interface")
    if isinstance(nested, dict):
        return nested
    top = manifest.get("interface")
    return top if isinstance(top, dict) else {}


def _text(value: Any, limit: int = 500) -> str:
    return value.strip()[:limit] if isinstance(value, str) else ""


def _author(value: Any) -> str | None:
    if isinstance(value, str) and value.strip():
        return value.strip()[:200]
    if isinstance(value, dict):
        name = value.get("name")
        if isinstance(name, str) and name.strip():
            return name.strip()[:200]
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
        # Same identity the instruction walker uses (the resolved path),
        # case-folded where the filesystem is, so a symlink or a case-only
        # respelling cannot list one skill twice.
        try:
            key = os.path.normcase(str(path.resolve()))
        except OSError:
            key = os.path.normcase(str(path))
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


def _md_files(root: Path, overrides: list[str], default: str, *, recursive: bool) -> list[Path]:
    """Markdown component files — an override list REPLACES the default dir."""
    bases = [_resolve_under(root, rel) for rel in overrides] if overrides else [root / default]
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


def _component_overrides(manifest: dict[str, Any], key: str, fmt: str) -> list[str]:
    """Manifest path overrides for ``commands``/``agents``.

    Claude puts them at the top level. An Agent Plugins manifest keeps
    non-portable kinds under ``extensions["ai.shuffle"]``; a top-level key is
    still honored so a plugin that used to load as Claude keeps its paths.
    """
    if fmt == FORMAT_AGENT_PLUGINS:
        nested = _rel_paths(_extension(manifest, SHUFFLE_EXTENSION).get(key))
        if nested:
            return nested
    return _rel_paths(manifest.get(key))


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


def _commands_for(plugin_name: str, root: Path, manifest: dict[str, Any], fmt: str) -> list[Command]:
    overrides = _component_overrides(manifest, "commands", fmt)
    return [
        Command(name=f"{plugin_name}:{path.stem}", path=str(path), scope="plugin")
        for path in _md_files(root, overrides, "commands", recursive=False)
    ]


def _agents_for(plugin_name: str, root: Path, manifest: dict[str, Any], fmt: str) -> list[PluginAgent]:
    out: list[PluginAgent] = []
    overrides = _component_overrides(manifest, "agents", fmt)
    for path in _md_files(root, overrides, "agents", recursive=True):
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
    plugin_data: Path | None = None,
) -> Any:
    """Expand ``${...}`` tokens through a server config, recursively.

    ``${CLAUDE_PLUGIN_ROOT}``/``${PLUGIN_ROOT}`` (the plugin root),
    ``${CLAUDE_PLUGIN_DATA}``/``${PLUGIN_DATA}`` (its data folder) and
    ``${CLAUDE_PROJECT_DIR}`` are the variables the two plugin formats define;
    anything else reads ``env`` — the process environment by default, or the
    caller's own map (the loader passes the process environment overlaid with
    the settings stack's ``env`` for ``mcp.json``). An absent variable expands
    to the empty string, matching Claude Code. ``os.environ`` is only read,
    never written. ``plugin_root``/``plugin_data`` are ``None`` outside a
    plugin, where those names are looked up like any other variable.
    """
    lookup = os.environ if env is None else env
    if isinstance(value, str):

        def repl(match: re.Match[str]) -> str:
            var = match.group(1)
            if var in _ROOT_VARS and plugin_root is not None:
                return str(plugin_root)
            if var in _DATA_VARS and plugin_data is not None:
                return str(plugin_data)
            if var == "CLAUDE_PROJECT_DIR":
                return str(project_dir)
            return lookup.get(var, "")

        return _VAR_RE.sub(repl, value)
    kwargs = {
        "plugin_root": plugin_root,
        "project_dir": project_dir,
        "env": env,
        "plugin_data": plugin_data,
    }
    if isinstance(value, dict):
        return {k: substitute_vars(v, **kwargs) for k, v in value.items()}
    if isinstance(value, list):
        return [substitute_vars(v, **kwargs) for v in value]
    return value


def _mcp_default_files(fmt: str) -> tuple[str, ...]:
    # Agent Plugins name the file ``mcp.json``; Claude ``.mcp.json``. An Agent
    # Plugins package that ships only ``.mcp.json`` (it used to load as
    # Claude) still has it read.
    return ("mcp.json", ".mcp.json") if fmt == FORMAT_AGENT_PLUGINS else (".mcp.json",)


def raw_mcp_servers(root: Path, manifest: dict[str, Any], fmt: str = FORMAT_CLAUDE) -> dict[str, Any]:
    """The plugin's declared servers: an inline ``mcpServers`` dict, a path to a
    JSON file, or the format's conventional file at the plugin root."""
    declared = manifest.get("mcpServers")
    if isinstance(declared, dict):
        return declared
    path: Path | None = None
    if isinstance(declared, str) and declared.strip():
        path = _resolve_under(root, declared)
    else:
        for name in _mcp_default_files(fmt):
            if (root / name).is_file():
                path = root / name
                break
    if path is None:
        return {}
    if not path.is_file():
        log.warning("plugin MCP config not found: %s", path)
        return {}
    data = _read_json_object(path, "plugin MCP config")
    if data is None:
        return {}
    servers = data.get("mcpServers", data)
    return servers if isinstance(servers, dict) else {}


def mcp_server_support(cfg: Any) -> tuple[str, str]:
    """``(support, reason)`` for one raw server entry, without logging."""
    if not isinstance(cfg, dict):
        return SUPPORT_UNSUPPORTED, "the server entry is not a JSON object"
    kind = str(cfg.get("type") or cfg.get("transport") or "").strip().lower()
    if kind == "sse":
        return SUPPORT_UNSUPPORTED, "SSE transport is not supported"
    if kind in ("http", "streamable_http") or (not kind and "url" in cfg):
        if not cfg.get("url"):
            return SUPPORT_UNSUPPORTED, "HTTP server has no url"
        return SUPPORT_LOADED, ""
    if not cfg.get("command"):
        return SUPPORT_UNSUPPORTED, "server has no command and no url"
    return SUPPORT_LOADED, ""


def normalize_mcp_server(name: str, cfg: dict[str, Any]) -> dict[str, Any] | None:
    """Map a plugin-format server entry onto fluid's client config shape.

    stdio entries pass through (command/args/env/cwd); ``type: "http"`` becomes
    fluid's ``streamable_http`` transport; ``type: "sse"`` is skipped with a
    warning — fluid's MCP client has no SSE transport, and silently downgrading
    it would connect to the wrong endpoint.
    """
    support, reason = mcp_server_support(cfg)
    if support != SUPPORT_LOADED:
        log.warning("skipping MCP server %r: %s", name, reason)
        return None
    out = {k: v for k, v in cfg.items() if k not in ("type", "transport")}
    kind = str(cfg.get("type") or cfg.get("transport") or "").strip().lower()
    if kind in ("http", "streamable_http") or (not kind and "url" in cfg):
        out["transport"] = "streamable_http"
    return out


# ─── other components ────────────────────────────────────────────────────────


def _hook_events(value: Any) -> list[str]:
    """Event names from a hooks declaration (``{"hooks": {...}}`` or bare)."""
    if isinstance(value, dict) and isinstance(value.get("hooks"), (dict, list)):
        value = value["hooks"]
    if isinstance(value, dict):
        return [str(k) for k in value]
    if isinstance(value, list):
        out = []
        for item in value:
            if isinstance(item, str):
                out.append(item)
            elif isinstance(item, dict) and item.get("event"):
                out.append(str(item["event"]))
        return out
    return []


def _hook_components(root: Path, manifest: dict[str, Any]) -> list[PluginComponent]:
    """Hooks: ``hooks/hooks.json`` runs through the app's hook service; hooks
    declared anywhere else are listed but not run."""
    out: list[PluginComponent] = []
    hooks_file = root / "hooks" / "hooks.json"
    if hooks_file.is_file():
        data = _read_json_object(hooks_file, "plugin hooks file")
        for event in _hook_events(data or {}):
            out.append(PluginComponent(kind="hook", name=event, path=str(hooks_file)))
    declared = manifest.get("hooks")
    if isinstance(declared, (dict, list)):
        for event in _hook_events(declared):
            out.append(PluginComponent(
                kind="hook", name=event, support=SUPPORT_UNSUPPORTED,
                reason="only hooks in hooks/hooks.json run here",
            ))
    elif isinstance(declared, str) and declared.strip():
        target = _resolve_under(root, declared)
        try:
            same = target.resolve() == hooks_file.resolve()
        except OSError:
            same = False
        if not same:
            out.append(PluginComponent(
                kind="hook", name=declared.strip(), support=SUPPORT_UNSUPPORTED,
                reason="only hooks in hooks/hooks.json run here",
            ))
    return out


def _app_components(root: Path, manifest: dict[str, Any]) -> list[PluginComponent]:
    """ChatGPT-hosted app connectors (``.app.json``): listed, never run."""
    declared = _extension(manifest, OPENAI_EXTENSION).get("apps", manifest.get("apps"))
    path: Path | None = None
    names: list[str] = []
    if isinstance(declared, str) and declared.strip():
        path = _resolve_under(root, declared)
    elif isinstance(declared, dict):
        names = [str(k) for k in declared]
    elif (root / ".app.json").is_file():
        path = root / ".app.json"
    if path is not None and path.is_file():
        data = _read_json_object(path, "plugin app config") or {}
        apps = data.get("apps", data)
        names = [str(k) for k in apps] if isinstance(apps, dict) else []
        if not names:
            names = [path.name]
    reason = "hosted by ChatGPT; it cannot run outside it"
    return [
        PluginComponent(kind="app", name=n, support=SUPPORT_UNSUPPORTED, reason=reason)
        for n in names
    ]


def _claude_only_components(root: Path, manifest: dict[str, Any]) -> list[PluginComponent]:
    """Claude Code component kinds this app does not run yet."""
    out: list[PluginComponent] = []
    lsp = manifest.get("lspServers")
    lsp_names: list[str] = []
    if isinstance(lsp, dict):
        lsp_names = [str(k) for k in lsp]
    elif isinstance(lsp, str) and lsp.strip():
        lsp_names = [lsp.strip()]
    elif (root / ".lsp.json").is_file():
        data = _read_json_object(root / ".lsp.json", "plugin LSP config") or {}
        lsp_names = [str(k) for k in data][:50] or [".lsp.json"]
    for name in lsp_names:
        out.append(PluginComponent(
            kind="lsp_server", name=name, support=SUPPORT_UNSUPPORTED,
            reason="LSP servers are not supported",
        ))
    if manifest.get("outputStyles") or (root / "output-styles").is_dir():
        out.append(PluginComponent(
            kind="output_style", name="output styles", support=SUPPORT_UNSUPPORTED,
            reason="output styles are not supported",
        ))
    if manifest.get("dependencies"):
        out.append(PluginComponent(
            kind="dependency", name="dependencies", support=SUPPORT_UNSUPPORTED,
            reason="plugin dependencies are not installed automatically",
        ))
    return out


def _user_config_component(manifest: dict[str, Any]) -> list[PluginComponent]:
    user_config = manifest.get("userConfig")
    if not isinstance(user_config, dict) or not user_config:
        return []
    return [PluginComponent(
        kind="user_config", name=", ".join(sorted(str(k) for k in user_config))[:200],
        support=SUPPORT_NEEDS_CONFIG,
        reason="asks for settings this app does not collect yet",
    )]


# ─── the reader ──────────────────────────────────────────────────────────────


def read_plugin(
    root: Path,
    *,
    name: str | None = None,
    source: str = "",
    version: str | None = None,
) -> PluginRecord:
    """Read the plugin at ``root`` (any supported format) into a record.

    ``name`` is the name it was installed under and ``version`` the installed
    version directory, both used only when the manifest does not say. Never
    raises on bad plugin content.
    """
    manifest_path, fmt = find_manifest(root)
    manifest: dict[str, Any] = {}
    if manifest_path is not None:
        manifest = _read_json_object(manifest_path, "plugin manifest") or {}
    plugin_name = str(manifest.get("name") or name or root.name)
    declared_version = manifest.get("version") or version
    interface = _openai_interface(manifest) if fmt == FORMAT_AGENT_PLUGINS else {}

    description = _text(manifest.get("description"), 500) or _text(
        interface.get("shortDescription"), 500
    )
    display_name = (
        _text(interface.get("displayName"), 200)
        or _text(manifest.get("displayName"), 200)
        or plugin_name
    )
    icon = (
        _text(interface.get("logo"), 500)
        or _text(interface.get("composerIcon"), 500)
        or _text(manifest.get("icon"), 500)
        or None
    )
    author = _author(manifest.get("author")) or _author(interface.get("developerName"))

    skills = _skills_for(plugin_name, root, manifest)
    commands = _commands_for(plugin_name, root, manifest, fmt)
    agents = _agents_for(plugin_name, root, manifest, fmt)
    servers = {
        str(k): v for k, v in raw_mcp_servers(root, manifest, fmt).items()
    }

    components: list[PluginComponent] = []
    components += [
        PluginComponent(kind="skill", name=s.name.split(":", 1)[-1], path=s.path) for s in skills
    ]
    components += [
        PluginComponent(kind="command", name=c.name.split(":", 1)[-1], path=c.path) for c in commands
    ]
    components += [
        PluginComponent(kind="agent", name=a.name.split(":", 1)[-1], path=a.path) for a in agents
    ]
    for server_name, cfg in servers.items():
        support, reason = mcp_server_support(cfg)
        components.append(PluginComponent(
            kind="mcp_server", name=server_name, support=support, reason=reason,
        ))
    components += _hook_components(root, manifest)
    components += _app_components(root, manifest)
    components += _claude_only_components(root, manifest)
    components += _user_config_component(manifest)

    return PluginRecord(
        name=plugin_name,
        source=source,
        root=root,
        format=fmt,
        manifest=manifest,
        manifest_path=manifest_path,
        version=str(declared_version) if declared_version else None,
        display_name=display_name,
        description=description,
        author=author,
        icon=icon,
        skills=skills,
        commands=commands,
        agents=agents,
        mcp_servers=servers,
        components=components,
    )


# ─── marketplaces ────────────────────────────────────────────────────────────


@dataclass(frozen=True, kw_only=True)
class MarketplaceManifest:
    """A marketplace catalog, entries normalized to Claude's ``source`` shapes.

    Agent Plugins entries (``.agents/plugins/marketplace.json``) are rewritten
    so the existing install path can resolve them: ``source.path`` becomes a
    relative ``"./..."`` string and ``source.url`` a ``{"source": "url"}``
    object. npm (``source.package``) entries are dropped for now and listed in
    ``skipped``.
    """

    format: str
    path: Path
    data: dict[str, Any]
    skipped: list[str] = field(default_factory=list)


def _normalize_agents_source(source: Any) -> Any | None:
    if isinstance(source, str):
        return source
    if not isinstance(source, dict):
        return None
    if isinstance(source.get("path"), str) and source["path"].strip():
        rel = source["path"].strip().replace("\\", "/")
        return rel if rel.startswith("./") else f"./{rel.lstrip('/')}"
    if isinstance(source.get("url"), str) and source["url"].strip():
        out = {"source": "url", "url": source["url"].strip()}
        for key in ("ref", "sha"):
            if isinstance(source.get(key), str):
                out[key] = source[key]
        return out
    return None


def read_marketplace(root: Path) -> MarketplaceManifest | None:
    """The marketplace catalog at ``root`` in either format, or ``None``."""
    claude = root / _CLAUDE_MARKETPLACE
    if claude.is_file():
        data = _read_json_object(claude, "marketplace.json")
        return None if data is None else MarketplaceManifest(
            format=FORMAT_CLAUDE, path=claude, data=data
        )
    agents = root / _AGENTS_MARKETPLACE
    if not agents.is_file():
        return None
    data = _read_json_object(agents, "marketplace.json")
    if data is None:
        return None
    entries = data.get("plugins")
    normalized: list[Any] = []
    skipped: list[str] = []
    for entry in entries if isinstance(entries, list) else []:
        if not isinstance(entry, dict):
            continue
        source = _normalize_agents_source(entry.get("source", ""))
        if source is None:
            skipped.append(str(entry.get("name") or "?")[:100])
            continue
        normalized.append({**entry, "source": source})
    return MarketplaceManifest(
        format=FORMAT_AGENT_PLUGINS,
        path=agents,
        data={**data, "plugins": normalized},
        skipped=skipped,
    )


# ─── content hashing ─────────────────────────────────────────────────────────


def content_hash(root: Path, *, max_files: int = 2000) -> str | None:
    """A stable ``sha256:`` digest of every file under ``root``.

    Paths are relative and ``/``-separated so the digest is the same on every
    OS. ``None`` when the tree cannot be read, holds a symlink (its target
    could change without changing the digest) or is too large, so a caller
    pinning approval to the digest fails closed.
    """
    digest = hashlib.sha256()
    files: list[Path] = []
    try:
        if not root.is_dir() or root.is_symlink():
            return None
        for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
            base = Path(dirpath)
            for name in dirnames + filenames:
                if (base / name).is_symlink():
                    return None
            files.extend(base / name for name in filenames)
            if len(files) > max_files:
                return None
        files.sort(key=lambda p: p.relative_to(root).as_posix())
        for path in files:
            rel = path.relative_to(root).as_posix()
            data = path.read_bytes()
            digest.update(rel.encode("utf-8") + b"\0")
            digest.update(str(len(data)).encode("ascii") + b"\0")
            digest.update(data)
    except OSError:
        return None
    return f"sha256:{digest.hexdigest()}"
