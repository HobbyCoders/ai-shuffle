"""Live tool-surface discovery — the one inventory (ruling T1).

Everything a session can be given, enumerated from the registries that actually
build it, never from a hardcoded list. Seven group kinds:

============  ==================================================  ==================
group         source                                              names
============  ==================================================  ==================
harness       fluid's ``register_builtins`` into a throwaway
              ``ToolRegistry``                                    bare (``Read``…)
app           ``app.mcp.builtin_tools`` server definitions        ``mcp__builtins__*``
ai            ``app.mcp.ai_tools_sdk`` (``TOOL_CONFIG_MAP``)      ``mcp__ai-tools__*``
computer_use  ``app.mcp.computer_use_sdk._TOOL_NAMES``            ``mcp__computer-use__*``
browser       the bundled browser server's ``list_tools``         ``mcp__browser__*``
app_config    ``app.mcp.app_config_sdk``                          ``mcp__app-config__*``
mcp:<server>  each configured user MCP server's ``list_tools``    ``mcp__<server>__*``
============  ==================================================  ==================

MCP enumeration (``browser`` and every ``mcp:<server>`` group) runs through
fluid's own MCP client, which spawns/dials the server, handshakes, and calls
``tools/list``. That costs a process spawn, so results are cached for
``MCP_CACHE_TTL_S`` per server; ``refresh=True`` busts the cache. A server that
fails to answer appears as a group with ``tools: []`` plus an entry in
``errors`` — never silently dropped, so the editor can show it as broken.

Descriptions are the first sentence of the spec description (see
``first_sentence``). Risk is normalized onto the T1 vocabulary:
``safe`` → ``read``, ``write`` → ``write``, ``destructive`` → ``exec``; a tool
whose source carries no risk at all reports ``None``.

Known gap: the ``computer_use`` group reports names without descriptions.
Its ``@tool`` definitions live inside ``create_computer_use_server``, which
starts the on-screen overlay and a ``DisplayManager`` as a side effect — far
too much for an inventory request — so this module reads the module-level
``_TOOL_NAMES`` list instead and leaves ``description`` null.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# How long an MCP server's ``tools/list`` answer is reused (ruling T1).
MCP_CACHE_TTL_S = 600.0

# How long one server may take to spawn/handshake/answer before it is recorded
# as an error group.
MCP_LIST_TIMEOUT_S = 25.0

# fluid's ToolSpec risk vocabulary → the T1 response vocabulary.
_RISK_MAP = {"safe": "read", "write": "write", "destructive": "exec"}

# server name -> (expires_at_monotonic, tools, error)
_MCP_CACHE: Dict[str, Tuple[float, List[Dict[str, Any]], Optional[str]]] = {}

_SENTENCE_END = re.compile(r"(?<=[.!?])\s")


def first_sentence(text: Optional[str]) -> Optional[str]:
    """The first sentence of a tool description, whitespace-collapsed.

    Tool descriptions in both fluid and the SDK are multi-paragraph prompts;
    the picker shows one line per tool.
    """
    if not text:
        return None
    flat = " ".join(str(text).split())
    if not flat:
        return None
    parts = _SENTENCE_END.split(flat, maxsplit=1)
    return parts[0].strip() or None


def _risk(value: Any) -> Optional[str]:
    if not isinstance(value, str):
        return None
    return _RISK_MAP.get(value)


def _tool_row(name: str, description: Any, risk: Any = None) -> Dict[str, Any]:
    return {
        "name": name,
        "description": first_sentence(description),
        "risk": _risk(risk),
    }


# ---------------------------------------------------------------------------
# In-process groups
# ---------------------------------------------------------------------------

def harness_tools() -> List[Dict[str, Any]]:
    """fluid's builtins, from a throwaway registry.

    Registering into a fresh ``ToolRegistry`` is the only honest source: adding
    a builtin upstream changes this list with no edit here (and the T8 endpoint
    test iterates the same registry, so no test needs an edit either).
    """
    from fluid.tools.builtins import register_builtins
    from fluid.tools.registry import ToolRegistry

    registry = ToolRegistry()
    register_builtins(registry)
    rows = [
        _tool_row(spec.name, spec.description, spec.risk)
        for spec in registry.list_tools(loaded_only=False)
    ]
    rows.sort(key=lambda r: r["name"])
    return rows


def _sdk_server_tools(server_cfg: Any) -> List[Any]:
    """The raw ``@tool`` objects an in-proc SDK server was assembled from."""
    from app.core.provider.sdk_tool_records import recorded_sdk_server_tools

    return list(recorded_sdk_server_tools(server_cfg) or [])


def app_tools() -> List[Dict[str, Any]]:
    """``mcp__builtins__*`` — the app's bridge tools.

    Built with no session/project context: the factory only closes over those
    ids, so the definitions (name + description) it produces are the same ones
    a real session gets.
    """
    from app.mcp.builtin_tools import create_builtin_tools_server, get_builtin_tool_names

    described: Dict[str, Any] = {}
    try:
        server = create_builtin_tools_server(is_admin=True)
        if server is not None:
            for t in _sdk_server_tools(server):
                described[f"mcp__builtins__{t.name}"] = t.description
    except Exception:
        logger.warning("could not build the builtins server for discovery", exc_info=True)

    names = list(dict.fromkeys(list(described) + get_builtin_tool_names()))
    rows = [_tool_row(name, described.get(name)) for name in names]
    rows.sort(key=lambda r: r["name"])
    return rows


def _ai_tool_definitions() -> Dict[str, Any]:
    """``{bare tool name: description}`` for every AI tool the module defines.

    The ``_create_*_tools`` builders only close over the Node worker (which is
    never called here), so passing ``None`` yields the real definitions.
    """
    from app.mcp import ai_tools_sdk

    out: Dict[str, Any] = {}
    builders = (
        (ai_tools_sdk._create_image_tools, True),
        (ai_tools_sdk._create_video_tools, True),
        (ai_tools_sdk._create_3d_tools, True),
        (ai_tools_sdk._create_audio_gen_tools, True),
        (ai_tools_sdk._create_catalog_tools, False),
        (ai_tools_sdk._create_generic_tools, True),
    )
    for builder, takes_worker in builders:
        try:
            defs = builder(None) if takes_worker else builder()
        except Exception:
            logger.debug("ai-tools definition builder failed: %s", builder, exc_info=True)
            continue
        for t in defs or ():
            out[t.name] = t.description
    return out


def ai_tool_names_for_config(
    ai_tools_config: Optional[Dict[str, Any]], *, every: bool = False
) -> List[str]:
    """``mcp__ai-tools__*`` names for one legacy ``ai_tools`` map.

    ``every=True`` ignores the map and returns the full set — what the v82
    migration needs to compute "the categories the user turned off".
    """
    from app.mcp.ai_tools_sdk import TOOL_CONFIG_MAP, _is_tool_enabled

    if every:
        return [f"mcp__ai-tools__{name}" for name in TOOL_CONFIG_MAP]
    config = ai_tools_config or {}
    return [
        f"mcp__ai-tools__{name}"
        for name in TOOL_CONFIG_MAP
        if _is_tool_enabled(name, config)
    ]


def ai_tools_config_from_names(names: List[str]) -> Dict[str, bool]:
    """The legacy ``ai_tools`` toggle map implied by a set of permitted tools.

    ``create_ai_tools_server`` still gates on the per-category booleans, so T5's
    "attach the server when at least one AI tool is permitted" is expressed by
    synthesizing the map the picker's selection implies. The two catch-all tools
    (``runFalModel`` / ``searchModels``) map to EVERY category, so they are
    skipped here — otherwise permitting them alone would silently turn every
    category back on.
    """
    from app.mcp.ai_tools_sdk import TOOL_CONFIG_MAP

    permitted = {n.split("__")[-1] for n in names}
    catch_all = max(
        (len(m["config_keys"]) for m in TOOL_CONFIG_MAP.values() if "config_keys" in m),
        default=0,
    )
    out: Dict[str, bool] = {}
    for tool_name, mapping in TOOL_CONFIG_MAP.items():
        if tool_name not in permitted:
            continue
        keys = mapping.get("config_keys") or [mapping.get("config_key")]
        if len(keys) >= catch_all > 1:
            continue
        for key in keys:
            if key:
                out[key] = True
    return out


def ai_tools() -> Tuple[List[Dict[str, Any]], bool]:
    """``mcp__ai-tools__*`` plus whether the fal.ai key is configured.

    Availability is per tool in the response shape, but every AI tool runs on
    the same credential, so they share one verdict.
    """
    from app.mcp.ai_tools_sdk import TOOL_CONFIG_MAP, _get_fal_api_key

    try:
        available = bool(_get_fal_api_key())
    except Exception:
        available = False

    described = _ai_tool_definitions()
    rows = []
    for name in TOOL_CONFIG_MAP:
        row = _tool_row(f"mcp__ai-tools__{name}", described.get(name))
        row["available"] = available
        rows.append(row)
    rows.sort(key=lambda r: r["name"])
    return rows, available


def computer_use_tools() -> Tuple[List[Dict[str, Any]], bool, Optional[str]]:
    """``mcp__computer-use__*`` names plus the host's availability verdict.

    Descriptions are null here on purpose — see the module docstring.
    """
    from app.mcp.computer_use_sdk import _TOOL_NAMES

    try:
        from app.mcp.computer_use.scaling import computer_use_available

        ok, reason = computer_use_available()
    except Exception as exc:  # noqa: BLE001 — availability is best-effort
        ok, reason = False, str(exc)
    rows = [_tool_row(f"mcp__computer-use__{name}", None) for name in _TOOL_NAMES]
    rows.sort(key=lambda r: r["name"])
    return rows, bool(ok), (None if ok else (reason or None))


def app_config_tools() -> List[Dict[str, Any]]:
    """``mcp__app-config__*`` — admin-only app configuration tools."""
    from app.mcp.app_config_sdk import create_app_config_server, get_app_config_tool_names

    described: Dict[str, Any] = {}
    try:
        server = create_app_config_server(is_admin=True)
        if server is not None:
            for t in _sdk_server_tools(server):
                described[f"mcp__app-config__{t.name}"] = t.description
    except Exception:
        logger.warning("could not build the app-config server for discovery", exc_info=True)

    names = list(dict.fromkeys(list(described) + get_app_config_tool_names()))
    rows = [_tool_row(name, described.get(name)) for name in names]
    rows.sort(key=lambda r: r["name"])
    return rows


# ---------------------------------------------------------------------------
# MCP groups (browser + user servers)
# ---------------------------------------------------------------------------

def _fluid_mcp_config(server: Any) -> Optional[Dict[str, Any]]:
    """Translate an ``McpServerConfig`` into fluid's MCP client config.

    fluid's client keys transport off ``transport`` (``stdio`` |
    ``http``/``streamable_http``) and supports neither ``sse`` nor per-server
    headers, so those servers cannot be enumerated and return ``None``.
    """
    stype = getattr(server, "type", None)
    stype = getattr(stype, "value", stype)
    if stype in (None, "stdio"):
        if not getattr(server, "command", None):
            return None
        return {
            "transport": "stdio",
            "command": server.command,
            "args": list(server.args or []),
            "env": dict(server.env) if getattr(server, "env", None) else None,
        }
    if stype in ("http", "streamable_http"):
        if not getattr(server, "url", None):
            return None
        return {"transport": "http", "url": server.url}
    return None


async def _list_mcp_tools(name: str, config: Dict[str, Any]) -> List[Dict[str, Any]]:
    """One ``tools/list`` round-trip through fluid's MCP client.

    The client is a standalone connection (its own ``start()``/``aclose()``), so
    this needs no engine and no session — it is the same code path
    ``ToolRegistry.connect_mcp`` uses, minus the registration.
    """
    from fluid.tools.mcp import McpClient

    client = McpClient(name, config)
    try:
        await asyncio.wait_for(client.start(), timeout=MCP_LIST_TIMEOUT_S)
        descriptors = await asyncio.wait_for(
            client.list_tools(), timeout=MCP_LIST_TIMEOUT_S
        )
    finally:
        try:
            await asyncio.wait_for(client.aclose(), timeout=5.0)
        except Exception:
            logger.debug("mcp client %s did not close cleanly", name, exc_info=True)
    rows = [
        _tool_row(f"mcp__{name}__{d.name}", d.description, d.risk) for d in descriptors
    ]
    rows.sort(key=lambda r: r["name"])
    return rows


async def mcp_server_tools(
    name: str, server: Any, *, refresh: bool = False
) -> Tuple[List[Dict[str, Any]], Optional[str]]:
    """``(tools, error)`` for one MCP server, cached ``MCP_CACHE_TTL_S``."""
    now = time.monotonic()
    if not refresh:
        cached = _MCP_CACHE.get(name)
        if cached is not None and cached[0] > now:
            return list(cached[1]), cached[2]

    config = _fluid_mcp_config(server)
    if config is None:
        error = f"{name}: transport not supported by the harness MCP client"
        _MCP_CACHE[name] = (now + MCP_CACHE_TTL_S, [], error)
        return [], error

    try:
        rows = await _list_mcp_tools(name, config)
        error = None
    except asyncio.TimeoutError:
        rows, error = [], f"{name}: timed out after {MCP_LIST_TIMEOUT_S:.0f}s"
    except Exception as exc:  # noqa: BLE001 — a broken server must still show
        rows, error = [], f"{name}: {exc}"
        logger.info("MCP tool discovery failed for %s", name, exc_info=True)

    _MCP_CACHE[name] = (now + MCP_CACHE_TTL_S, rows, error)
    return list(rows), error


def clear_mcp_cache() -> None:
    """Drop every cached ``tools/list`` answer (tests; ``?refresh=true``)."""
    _MCP_CACHE.clear()


# ---------------------------------------------------------------------------
# The surface
# ---------------------------------------------------------------------------

def _group(
    gid: str,
    label: str,
    kind: str,
    tools: List[Dict[str, Any]],
    *,
    server: Optional[str] = None,
    available: bool = True,
    admin_only: bool = False,
) -> Dict[str, Any]:
    return {
        "id": gid,
        "label": label,
        "kind": kind,
        "server": server,
        "available": available,
        "admin_only": admin_only,
        "tools": tools,
    }


async def build_tool_surface(*, refresh: bool = False) -> Dict[str, Any]:
    """Every tool a session can be given, grouped (ruling T1).

    Group order is the editor's: harness, app, ai, browser, computer_use,
    app_config, then MCP servers alphabetically.
    """
    errors: List[Dict[str, str]] = []
    groups: List[Dict[str, Any]] = []

    def _safe(gid: str, fn):
        try:
            return fn()
        except Exception as exc:  # noqa: BLE001 — one broken group must not 500
            logger.exception("tool surface group %s failed", gid)
            errors.append({"group": gid, "message": str(exc)})
            return None

    harness = _safe("harness", harness_tools)
    groups.append(_group("harness", "Harness tools", "harness", harness or []))

    app = _safe("app", app_tools)
    groups.append(_group("app", "App tools", "app", app or []))

    ai_result = _safe("ai", ai_tools)
    ai_rows, ai_available = ai_result if ai_result else ([], False)
    groups.append(
        _group("ai", "AI generation", "ai", ai_rows, available=ai_available)
    )

    # Browser + user MCP servers, both enumerated through fluid's MCP client.
    browser_name, servers = _configured_mcp_servers(errors)

    if browser_name is not None:
        rows, error = await mcp_server_tools(
            browser_name, servers.pop(browser_name), refresh=refresh
        )
        if error:
            errors.append({"group": "browser", "message": error})
        groups.append(
            _group("browser", "Browser", "browser", rows, server=browser_name)
        )

    cu_result = _safe("computer_use", computer_use_tools)
    cu_rows, cu_available, cu_reason = cu_result if cu_result else ([], False, None)
    if cu_reason:
        errors.append({"group": "computer_use", "message": cu_reason})
    groups.append(
        _group(
            "computer_use",
            "Computer use",
            "computer_use",
            cu_rows,
            server="computer-use",
            available=cu_available,
        )
    )

    cfg_rows = _safe("app_config", app_config_tools)
    groups.append(
        _group(
            "app_config",
            "App configuration",
            "app_config",
            cfg_rows or [],
            server="app-config",
            admin_only=True,
        )
    )

    for name in sorted(servers):
        rows, error = await mcp_server_tools(name, servers[name], refresh=refresh)
        gid = f"mcp:{name}"
        if error:
            errors.append({"group": gid, "message": error})
        groups.append(_group(gid, name, "mcp", rows, server=name))

    return {
        "groups": groups,
        "discovered_at": datetime.now(timezone.utc).isoformat(),
        "errors": errors,
    }


def _configured_mcp_servers(
    errors: List[Dict[str, str]],
) -> Tuple[Optional[str], Dict[str, Any]]:
    """``(browser server name or None, {name: McpServerConfig})``.

    Disabled servers (settings.json ``disabledMcpServers``) are skipped: a
    session never gets their tools, so the picker must not offer them.
    """
    try:
        from app.core.builtin_mcp_servers import BROWSER_SERVER_NAME
        from app.core.mcp_service import get_mcp_service

        service = get_mcp_service()
        disabled = set(service.get_disabled_servers() or ())
        servers = {
            s.name: s
            for s in service.get_all_servers_with_builtins()
            if s.name not in disabled
        }
    except Exception as exc:  # noqa: BLE001
        logger.exception("could not list MCP servers for tool discovery")
        errors.append({"group": "mcp", "message": str(exc)})
        return None, {}

    browser = BROWSER_SERVER_NAME if BROWSER_SERVER_NAME in servers else None
    return browser, servers
