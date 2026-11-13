"""Deferred tools for an MCP client with no deferral of its own (ACP agents).

The Claude Code CLI keeps a bridged tool out of its context when the tool
list marks it ``anthropic/alwaysLoad: false``; fluid's native loop has
ToolSearch. An ACP agent (Google's ``agy acp``) has neither: every tool the
bundled bridge lists costs its full schema on every request. For such a
client the bridge lists a small always-loaded core plus two tools:

* :data:`LOAD_TOOL` announces every other tool by name, grouped by server, in
  its own description, and returns full schemas as text when called.
* :data:`CALL_TOOL` runs one of those tools by its real name. The bridge
  unwraps it before anything else sees the call, so the session's tool
  filter, hooks, permissions, registry guards and per-tool timeouts all apply
  to the real tool, never to the proxy.

The announcement lives in the tool list, which the agent reads when it starts
the bridge server (``session/new`` and ``session/resume`` both pass it), so it
is fixed for the session and survives a resume that does not resend the
system prompt.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from typing import Any

LOAD_TOOL = "LoadTools"
CALL_TOOL = "CallTool"
PROXY_TOOLS = frozenset({LOAD_TOOL, CALL_TOOL})

# Spellings an agent may put in front of a name it read off its own tool list.
_NAME_PREFIXES = ("mcp__fluid_bundled__", "fluid_bundled/", "mcp_fluid_bundled_", "fluid_bundled_")


def real_name(name: Any) -> str:
    """``name`` without a bundled-server spelling in front of it."""
    text = str(name or "").strip()
    for prefix in _NAME_PREFIXES:
        if text.startswith(prefix):
            return text[len(prefix):]
    return text


def unwrap_call(args: Mapping[str, Any] | None) -> tuple[str, dict[str, Any]]:
    """``(real tool name, its input)`` from a :data:`CALL_TOOL` call's arguments."""
    args = args if isinstance(args, Mapping) else {}
    inner = args.get("input")
    if isinstance(inner, str):
        # Some models send the object as JSON text.
        try:
            inner = json.loads(inner)
        except ValueError:
            inner = None
    return real_name(args.get("name")), dict(inner) if isinstance(inner, Mapping) else {}


def catalog_rows(names: Iterable[str]) -> list[str]:
    """One ``server: tool, tool`` line per server; bare-named tools under ``core``.

    The same grouping as the native leg's deferred catalog: names only, so
    the list stays a few tokens per tool. Sorted, so the text is stable.
    """
    groups: dict[str, list[str]] = {}
    for name in sorted(set(names)):
        if name.startswith("mcp__"):
            server, _, tool = name[len("mcp__"):].partition("__")
            groups.setdefault(server, []).append(tool or name)
        else:
            groups.setdefault("core", []).append(name)
    order = sorted(groups, key=lambda s: (s != "core", s))
    rows = []
    for server in order:
        prefix = "" if server == "core" else f"mcp__{server}__"
        rows.append(f"{server} ({prefix}<tool>): {', '.join(groups[server])}")
    return rows


def proxy_tools(deferred: Iterable[str]) -> list[dict[str, Any]]:
    """The :data:`LOAD_TOOL` and :data:`CALL_TOOL` entries for a tool list."""
    rows = "\n".join(catalog_rows(deferred))
    return [
        {
            "name": LOAD_TOOL,
            "description": (
                "Load the full schema of tools that are not in your tool list. "
                "They are announced here by name only; call them with "
                f"{CALL_TOOL} after reading the schema. Pass exact names in "
                "`names` (the full mcp__<server>__<tool> form, or a core tool's "
                "bare name) or words in `query` to find tools by purpose.\n\n"
                f"Tools available through {CALL_TOOL}, by server:\n{rows}"
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "names": {
                        "type": "array", "items": {"type": "string"},
                        "description": "Exact tool names to load.",
                    },
                    "query": {
                        "type": "string",
                        "description": "Words to search tool names and descriptions for.",
                    },
                },
            },
        },
        {
            "name": CALL_TOOL,
            "description": (
                f"Run a tool listed by {LOAD_TOOL}. `name` is its full name "
                "(mcp__<server>__<tool>, or a core tool's bare name); `input` "
                "is the object its schema describes. Load the schema first "
                "unless you already have it. The call is checked and approved "
                "exactly as a direct call of that tool."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "The tool's full name."},
                    "input": {"type": "object", "description": "The tool's arguments."},
                },
                "required": ["name", "input"],
            },
        },
    ]


def load_text(specs: Iterable[Any], missing: Iterable[str] = ()) -> str:
    """The :data:`LOAD_TOOL` result: one JSON schema line per tool."""
    lines = [
        json.dumps(
            {"name": s.name, "description": s.description, "input_schema": s.input_schema},
            ensure_ascii=False,
        )
        for s in specs
    ]
    missing = list(missing)
    if not lines:
        text = "No such tools."
    else:
        text = f"Loaded {len(lines)} tool(s); run them with {CALL_TOOL}:\n" + "\n".join(lines)
    if missing and lines:
        text += f"\nNot found: {', '.join(missing)}."
    elif missing:
        text = f"No such tools: {', '.join(missing)}. Use `query` to search by purpose."
    return text
