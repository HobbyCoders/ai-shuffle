"""Plugin tools for chats: find a plugin, check one, install one.

Registered on the ``builtins`` server (``mcp__builtins__FindPlugins``,
``CheckPlugin``, ``InstallPlugin``) so code-capable sessions reach them on the
native and the delegated leg alike. The built-in ``build-plugin`` skill tells
an agent when and how to use them.

Each tool calls the same service code Settings > Plugins uses
(:class:`app.core.plugin_service.PluginService`, fluid's validator and plugin
reader), so an agent sees what the owner sees.

Nothing an agent installs runs before the owner turns it on:

* a plugin folder the agent wrote is saved as ``<name>@authored`` and waits in
  Settings > Plugins for "Approve and enable", pinned to the hash of the exact
  files (``app.core.plugin_authored``). The folder must pass the validator
  (``fluid.context.plugin_check``), which refuses hooks, stray files and text
  that looks like a credential;
* a marketplace plugin is installed switched off and marked ``approval:
  "pending"`` with the session and agent that asked, so Settings shows "Needs
  your review". Only plugins from the sources the owner added qualify, and only
  when their files come from inside that source or over HTTPS.

No tool enables a plugin. ``InstallPlugin`` is write-risk (Ask mode puts it to
the owner) and refuses sessions that are not the owner's. The tools take no
credential and return none: services are listed by name, and an MCP server's
literal ``env`` values are masked.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional

logger = logging.getLogger(__name__)

TOOL_NAMES = ("FindPlugins", "CheckPlugin", "InstallPlugin")

_MAX_RESULTS = 25
_MAX_LINES = 40


def _text(message: str, *, error: bool = False) -> Dict[str, Any]:
    out: Dict[str, Any] = {"content": [{"type": "text", "text": message}]}
    if error:
        out["isError"] = True
    return out


def _service():
    from app.core.plugin_service import get_plugin_service

    return get_plugin_service()


def _resolve(path: Any, cwd: Optional[str]) -> Optional[Path]:
    if not isinstance(path, str) or not path.strip():
        return None
    candidate = Path(path.strip()).expanduser()
    if not candidate.is_absolute() and cwd:
        candidate = Path(cwd) / candidate
    return candidate


def _state(plugin: Any) -> str:
    if getattr(plugin, "approval", None) == "pending":
        return "waiting for the owner's review"
    return "on" if getattr(plugin, "enabled", False) else "off"


# ─── find ────────────────────────────────────────────────────────────────────


def find_plugins(query: str) -> str:
    """Installed plugins and the plugins the owner's sources offer, by keywords."""
    svc = _service()
    words = [w for w in query.lower().split() if w]

    def hit(*texts: Any) -> bool:
        blob = " ".join(str(t or "") for t in texts).lower()
        return all(w in blob for w in words)

    lines: List[str] = []
    installed = {p.id: p for p in svc.get_installed_plugins()}
    for plugin in installed.values():
        if hit(plugin.id, plugin.description):
            lines.append(f"- {plugin.id} (installed, {_state(plugin)}): {plugin.description}")
    for plugin in svc.get_available_plugins():
        if plugin.id in installed or not hit(plugin.id, plugin.description):
            continue
        lines.append(f"- {plugin.id} (available): {plugin.description}")
    if not lines:
        sources = ", ".join(m.id for m in svc.get_marketplaces()) or "none"
        return (
            f"No plugin matches {query!r} in this worker's plugin sources ({sources}). "
            "Build one with the build-plugin skill, or ask the owner to add a plugin "
            "source in Settings > Plugins."
        )
    more = len(lines) - _MAX_RESULTS
    shown = lines[:_MAX_RESULTS] + ([f"... and {more} more; narrow the query"] if more > 0 else [])
    return "Plugins matching the query:\n" + "\n".join(shown)


# ─── check ───────────────────────────────────────────────────────────────────


def _component_dicts(components: Iterable[Any]) -> List[Dict[str, Any]]:
    out = []
    for comp in components:
        if isinstance(comp, dict):
            out.append(comp)
        else:
            out.append(comp.to_dict())
    return out


def _summary(head: str, components: Iterable[Any], services: List[str]) -> str:
    """What loads, what was adapted or skipped, what runs code, what it asks for."""
    comps = _component_dicts(components)
    loads = [c for c in comps if c.get("support") == "loaded" and c.get("kind") != "service"]
    adapted = [c for c in loads if c.get("reason")]
    skipped = [c for c in comps if c.get("support") != "loaded"]
    runs = [c for c in loads if c.get("kind") in ("hook", "mcp_server")]

    def label(c: Dict[str, Any]) -> str:
        return f"{str(c.get('kind', '')).replace('_', ' ')} {c.get('name', '')}"

    lines = [head]
    lines.append("Loads: " + (", ".join(label(c) for c in loads) or "nothing"))
    if adapted:
        lines.append("Adapted for this app:")
        lines += [f"- {label(c)}: {c['reason']}" for c in adapted]
    if skipped:
        lines.append("Not used here:")
        lines += [f"- {label(c)}: {c.get('reason') or 'not supported here'}" for c in skipped]
    if runs:
        lines.append("Runs code once the owner enables it:")
        lines += [f"- {label(c)}" + (f": {c['detail']}" if c.get("detail") else "") for c in runs]
    if services:
        lines.append("Outside services it asks for (the owner grants each in the private access card):")
        lines += [f"- {s}" for s in services]
    if len(lines) > _MAX_LINES:
        lines = lines[:_MAX_LINES] + [f"... {len(lines) - _MAX_LINES} more lines"]
    return "\n".join(lines)


def _declared_services(manifest: Any) -> List[str]:
    """``extensions["ai.shuffle"].services`` as plain lines: name, access, purpose."""
    extensions = manifest.get("extensions") if isinstance(manifest, dict) else None
    shuffle = extensions.get("ai.shuffle") if isinstance(extensions, dict) else None
    services = shuffle.get("services") if isinstance(shuffle, dict) else None
    out = []
    for item in services if isinstance(services, list) else []:
        if not isinstance(item, dict) or not isinstance(item.get("name"), str):
            continue
        access = item.get("access") if item.get("access") in ("read", "write") else "read"
        purpose = item.get("purpose") if isinstance(item.get("purpose"), str) else ""
        out.append(f"{item['name'][:100]} ({access})" + (f": {purpose[:300]}" if purpose else ""))
    return out[:20]


def check_folder(folder: Path) -> tuple[bool, str]:
    """``(ok, report)`` for a plugin folder an agent wrote."""
    from fluid.context.plugin_check import check_plugin_folder
    from fluid.context.plugin_formats import read_plugin

    result = check_plugin_folder(folder)
    if not result.ok or result.spec is None:
        return False, result.render() + "\nFix every problem and run CheckPlugin again."
    record = read_plugin(result.path, name=result.spec["name"], source="authored")
    head = (
        f"OK: {result.spec['name']} {result.spec.get('version', '0.1.0')} is a valid plugin. "
        "InstallPlugin(path=...) installs it switched off for the owner's review."
    )
    report = _summary(head, record.components, _declared_services(record.manifest))
    if result.warnings:
        report += "\nNotes:\n" + "\n".join(f"- {w}" for w in result.warnings)
    return True, report


def check_listed(plugin_id: str) -> tuple[bool, str]:
    """``(ok, report)`` for a plugin the owner's sources list or one installed."""
    svc = _service()
    installed = svc.get_plugin_details(plugin_id)
    details = installed or svc.get_available_plugin_details(plugin_id)
    if details is None:
        return False, f"No plugin {plugin_id!r} in this worker's plugin sources. Use FindPlugins."
    if installed is not None:
        head = f"{plugin_id} {details.version} (installed, {_state(details)}): {details.description}"
    else:
        head = f"{plugin_id} (available): {details.description}"
    comps = _component_dicts(details.components or [])
    services = [str(c.get("name")) for c in comps if c.get("kind") == "service"]
    refusal = install_refusal(plugin_id) if installed is None else None
    report = _summary(head, comps, services)
    if refusal:
        report += f"\nInstallPlugin would refuse it: {refusal}"
    return True, report


# ─── install ─────────────────────────────────────────────────────────────────


def _https_source(source: Any) -> bool:
    """Whether a marketplace entry's files come from inside the owner's source
    (a relative path) or over HTTPS. ``ssh``/``git@``/``http`` and anything
    else are refused for an agent's install."""
    if isinstance(source, str):
        return source.startswith(("./", ".\\"))
    if not isinstance(source, dict):
        return False
    kind = source.get("source")
    if kind == "github":
        return isinstance(source.get("repo"), str) and bool(source["repo"].strip())
    url = source.get("url")
    if not isinstance(url, str):
        return False
    url = url.strip()
    if kind == "url":
        return url.startswith("https://")
    if kind == "git-subdir":
        # owner/repo shorthand resolves to https://github.com/owner/repo.
        return url.startswith("https://") or (
            "://" not in url and "@" not in url and ":" not in url and url.count("/") == 1
        )
    return False


def install_refusal(plugin_id: str) -> Optional[str]:
    """Why an agent may not install ``plugin_id`` from a source, or None."""
    from app.core import connector_packages

    if connector_packages.kind_for(plugin_id):
        return "official connector runtimes are installed by the owner in Settings > Plugins"
    name, sep, source = plugin_id.rpartition("@")
    if not sep or not name or not source:
        return "plugin_id must be name@source as FindPlugins lists it"
    if source.strip().rstrip(". ").lower() == "authored":
        return "a plugin you wrote is installed with path=<folder>"
    svc = _service()
    if source not in {m.id for m in svc.get_marketplaces()}:
        return (
            f"{source!r} is not one of this worker's plugin sources; only the owner adds "
            "sources, in Settings > Plugins"
        )
    entry = svc._find_plugin_in_manifest(source, name)
    if entry is None:
        # A source with no catalog that carries skills itself installs as
        # <source>@<source>; its files are already inside the owner's clone.
        if name == source:
            return None
        return f"{source!r} does not list a plugin named {name!r}"
    if not _https_source(entry.get("source")):
        return (
            "its files come from outside the source and not over HTTPS; the owner can "
            "install it in Settings > Plugins"
        )
    return None


def install_folder(folder: Path) -> Dict[str, Any]:
    from fluid.context.plugin_authoring import PluginDraftError, save_authored_plugin

    ok, report = check_folder(folder)
    if not ok:
        return _text("Plugin not installed.\n" + report, error=True)
    from fluid.context.plugin_check import check_plugin_folder

    spec = check_plugin_folder(folder).spec
    try:
        # The folder the app's plugin list reads authored drafts from.
        saved = save_authored_plugin(spec, home=_service().PLUGINS_DIR)
    except PluginDraftError as exc:
        return _text(f"Plugin not installed: {exc}", error=True)
    listed = ", ".join(saved.components)
    if saved.unchanged:
        head = f"{saved.id} version {saved.version} was already installed with these files ({listed})."
    else:
        head = f"Installed {saved.id} version {saved.version} ({listed})."
    return _text(
        f"{head} It is off until the owner opens Settings > Plugins, reviews it and "
        "chooses Approve and enable. Tell the owner, and list what it adds. To change "
        "it later, raise the version, check it and install again; each version needs "
        "its own review."
    )


def install_listed(plugin_id: str, requested_by: Dict[str, Any]) -> Dict[str, Any]:
    refusal = install_refusal(plugin_id)
    if refusal:
        return _text(f"Plugin not installed: {refusal}.", error=True)
    svc = _service()
    if any(p.id == plugin_id for p in svc.get_installed_plugins()):
        return _text(
            f"{plugin_id} is already installed. The owner turns it on or off in Settings > Plugins."
        )
    info = svc.install_plugin(plugin_id, "user", enable=False, requested_by=requested_by)
    head = (
        f"Installed {info.id} version {info.version}, switched off and waiting for the "
        "owner's review. Nothing in it runs until the owner enables it in Settings > "
        "Plugins. Tell the owner, and say what it adds."
    )
    comps = _component_dicts(info.components or [])
    services = [str(c.get("name")) for c in comps if c.get("kind") == "service"]
    return _text(_summary(head, comps, services))


# ─── tools ───────────────────────────────────────────────────────────────────


def build_plugin_tools(
    tool: Callable[..., Any],
    *,
    is_admin: bool,
    cwd: Optional[str],
    session_id: Optional[str] = None,
    profile_id: Optional[str] = None,
) -> list:
    """The three plugin tools for one session's ``builtins`` server."""
    from app.db.async_database import run_sync

    @tool(
        "FindPlugins",
        "Search the plugins this worker can install (the plugin sources the owner "
        "added) and the ones already installed, by keywords. Use it before building "
        "a plugin: if one fits, check it and install that instead. Read-only.",
        {
            "type": "object",
            "properties": {"query": {"type": "string", "description": "Keywords, e.g. 'cloudflare dns'."}},
            "required": ["query"],
            "additionalProperties": False,
        },
    )
    async def find_tool(args: dict) -> dict:
        query = str(args.get("query") or "").strip()
        if not query:
            return _text("query is required", error=True)
        try:
            return _text(await run_sync(find_plugins, query))
        except Exception as exc:  # noqa: BLE001 — a search never breaks the turn
            logger.warning("FindPlugins failed", exc_info=True)
            return _text(f"Plugin search failed: {type(exc).__name__}", error=True)

    @tool(
        "CheckPlugin",
        "Check a plugin before installing it: pass path= for a plugin folder you "
        "wrote (runs the format validator and lists every problem), or "
        "plugin_id='name@source' for one FindPlugins listed. Reports what would "
        "load, what is adapted or skipped for this app and why, which hooks and MCP "
        "servers run code, and which outside services it asks for. Read-only.",
        {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "A plugin folder (absolute, or relative to the working directory)."},
                "plugin_id": {"type": "string", "description": "name@source from FindPlugins."},
            },
            "additionalProperties": False,
        },
    )
    async def check_tool(args: dict) -> dict:
        path, plugin_id = args.get("path"), str(args.get("plugin_id") or "").strip()
        if bool(path) == bool(plugin_id):
            return _text("Pass exactly one of path or plugin_id.", error=True)
        try:
            if plugin_id:
                ok, report = await run_sync(check_listed, plugin_id)
            else:
                folder = _resolve(path, cwd)
                if folder is None:
                    return _text("path is required", error=True)
                ok, report = await run_sync(check_folder, folder)
        except Exception as exc:  # noqa: BLE001
            logger.warning("CheckPlugin failed", exc_info=True)
            return _text(f"Plugin check failed: {type(exc).__name__}", error=True)
        return _text(report, error=not ok)

    @tool(
        "InstallPlugin",
        "Install a plugin for the owner, switched off. Pass path= for a plugin folder "
        "you wrote (it must pass CheckPlugin), or plugin_id='name@source' for one "
        "FindPlugins listed. The owner reviews and enables it in Settings > Plugins "
        "before anything in it runs; no tool can enable it.",
        {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "A plugin folder you wrote."},
                "plugin_id": {"type": "string", "description": "name@source from FindPlugins."},
            },
            "additionalProperties": False,
        },
    )
    async def install_tool(args: dict) -> dict:
        if not is_admin:
            return _text("Only the owner's sessions can install plugins.", error=True)
        path, plugin_id = args.get("path"), str(args.get("plugin_id") or "").strip()
        if bool(path) == bool(plugin_id):
            return _text("Pass exactly one of path or plugin_id.", error=True)
        try:
            if plugin_id:
                requested_by = {
                    "session_id": session_id,
                    "profile_id": profile_id,
                    "requested_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
                }
                return await run_sync(install_listed, plugin_id, requested_by)
            folder = _resolve(path, cwd)
            if folder is None:
                return _text("path is required", error=True)
            return await run_sync(install_folder, folder)
        except ValueError as exc:
            return _text(f"Plugin not installed: {exc}", error=True)
        except Exception as exc:  # noqa: BLE001 — report, never crash the turn
            logger.warning("InstallPlugin failed", exc_info=True)
            return _text(f"Plugin not installed: {type(exc).__name__}", error=True)

    return [find_tool, check_tool, install_tool]
