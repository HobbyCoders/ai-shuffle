"""Side table: SDK MCP server instance → the @tool objects it was built from.

The omni harness executes in-process tools through its own ToolRegistry, not
through the SDK's mcp.Server wrapper. But by the time an in-proc server
reaches the adapter it is an opaque ``{"type": "sdk", "instance": mcp.Server}``
config — the raw ``@tool`` objects (name/description/input_schema/handler) are
no longer reachable from it.

Every in-proc server in this app flows through ``sdk_compat.patch_sdk_server``,
which uniquely still holds BOTH the server config and the original tools list.
It records the pair here; ``HarnessAdapter._build_registry`` looks the tools
back up by instance and registers them with omni (see omni_tool_bridge).

Keyed weakly by the ``mcp.Server`` instance — tools are closures over
per-session state (session_id, project scope, admin flag), so recordings must
be per-instance, never by server name, and must not outlive the server.

This module is import-safe in every deployment: no omni imports, no SDK
imports. Flag-off installs record and never read.
"""

from __future__ import annotations

import weakref
from typing import Any, List, Optional

_RECORDS: "weakref.WeakKeyDictionary[Any, List[Any]]" = weakref.WeakKeyDictionary()


def record_sdk_server_tools(server_cfg: Any, tools: List[Any]) -> None:
    """Remember the @tool objects an SDK in-proc server was assembled from.

    ``server_cfg`` is the dict returned by ``create_sdk_mcp_server()`` (or the
    bare instance). Best-effort: an unhashable/weakref-less instance is skipped
    rather than raised — recording is an optimization for the omni path, never
    a correctness requirement for the SDK path.
    """
    instance = server_cfg.get("instance") if isinstance(server_cfg, dict) else server_cfg
    if instance is None:
        return
    try:
        _RECORDS[instance] = list(tools)
    except TypeError:
        pass


def recorded_sdk_server_tools(server_cfg: Any) -> Optional[List[Any]]:
    """The @tool objects recorded for this server config/instance, or None."""
    instance = server_cfg.get("instance") if isinstance(server_cfg, dict) else server_cfg
    if instance is None:
        return None
    try:
        return _RECORDS.get(instance)
    except TypeError:
        return None
