"""Recover tools from owned Fluid servers or older caller-provided MCP servers.

Owned servers expose their tools directly. The weak side table supports plugins
that hand over an opaque MCP Server instance: it is per-instance, never by name,
and does not keep session-bound handler closures alive after their server dies.
The function names remain for compatibility with existing factories.
"""

from __future__ import annotations

import weakref
from typing import Any, List, Optional

from fluid.tools.inproc import InprocServer

_RECORDS: "weakref.WeakKeyDictionary[Any, List[Any]]" = weakref.WeakKeyDictionary()


def record_sdk_server_tools(server_cfg: Any, tools: List[Any]) -> None:
    """Record tools for an opaque MCP server; owned Fluid servers need no copy."""
    instance = server_cfg.get("instance") if isinstance(server_cfg, dict) else server_cfg
    if instance is None or isinstance(instance, InprocServer):
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
    if isinstance(instance, InprocServer):
        return list(instance.tools)
    try:
        return _RECORDS.get(instance)
    except TypeError:
        return None
