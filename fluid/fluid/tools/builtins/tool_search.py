"""ToolSearch — the model-facing progressive-disclosure seam.

MCP tool schemas stay out of the cached prompt prefix until needed (the
Manus/Anthropic "code execution with MCP" lesson). This builtin is how the
model pulls a deferred tool in on the JSON surface: search the registry's full
catalog (loaded or not), load the matches' schemas, and return them. Loaded
tools enter ``list_tools(loaded_only=True)`` and are therefore callable from
the NEXT model call onward.

The registry is injected by the engine as ``ctx.extra["tool_registry"]``.

A restricted session (``NativeLoop(allowed_tools=...)``, ruling CE12) also
injects ``ctx.extra["tool_filter"]``: a ``(name) -> bool`` predicate that is
this session's *view* of the shared registry. The registry stays unfiltered —
one registry may back several sessions with different surfaces — so every read
path here goes through the predicate, and a filtered tool is neither searchable
nor loadable.
"""

from __future__ import annotations

import json
import re
from collections.abc import Sequence
from typing import Any

from fluid.tools.registry import ToolCtx, ToolOutcome, ToolSpec

# Catalog lines are an index, not documentation: one truncated line per tool.
_CATALOG_DESC_CAP = 100


def _one_line(text: str) -> str:
    for line in (text or "").strip().splitlines():
        line = line.strip()
        if not line:
            continue
        if len(line) <= _CATALOG_DESC_CAP:
            return line
        # Cut at a word boundary: a mid-word (or mid-JSON) chop reads as
        # corruption, and the catalog only needs enough to pick keywords.
        cut = line.rfind(" ", 0, _CATALOG_DESC_CAP)
        if cut < _CATALOG_DESC_CAP // 2:
            cut = _CATALOG_DESC_CAP - 1
        return line[:cut].rstrip() + "…"
    return ""


# The literal first line of every catalog block. Rendering and parsing share it
# so a resumed session can recognise its own past announcements.
DEFERRED_CATALOG_HEADER = "The following deferred tools are available."
# Full-tier lines are ``- name: desc``; names-only tier lines are bare
# ``- name`` — the parser accepts both. Server-summary lines use ``* `` so
# they are never mistaken for tool names.
_CATALOG_NAME_RE = re.compile(r"^- ([^\s:]+?):?(?:\s|$)", re.MULTILINE)

# Catalog budget: the listing rides a user turn once per newly-known tool, so
# an unbounded catalog from a large MCP server (thousands of tools) would eat
# the window before work starts. Tiers degrade: full (name + one-liner) →
# names only → one summary line per server.
CATALOG_BUDGET_CHARS = 12_000

_CATALOG_PREAMBLE = (
    "{header} Their schemas are NOT loaded — calling one directly will fail; "
    "load its schema first with the ToolSearch tool (query "
    '"select:<name>[,<name>...]" or keywords). A tool listed here is '
    "available: never claim it is unavailable, and never substitute another "
    "route for it — load it instead."
)


def _server_of(name: str) -> str:
    parts = name.split("__", 2)
    return parts[1] if len(parts) == 3 else "(local)"


def render_deferred_catalog(
    specs: Sequence[ToolSpec], *, budget_chars: int = CATALOG_BUDGET_CHARS
) -> str | None:
    """System-reminder block announcing known-but-unloaded tools.

    The deferred tier of progressive disclosure, budgeted: the full listing
    (name + truncated one-line description) is used while it fits
    ``budget_chars``; past that it degrades to bare names, and past *that* to
    one line per server — so a pathological catalog can never eat the window.
    Returns ``None`` when there is nothing to announce.
    """
    if not specs:
        return None
    ordered = sorted(specs, key=lambda s: s.name)
    full = "\n".join(f"- {s.name}: {_one_line(s.description)}" for s in ordered)
    if len(full) > budget_chars:
        names_only = "\n".join(f"- {s.name}" for s in ordered)
        if len(names_only) <= budget_chars:
            full = names_only
        else:
            by_server: dict[str, int] = {}
            for s in ordered:
                by_server[_server_of(s.name)] = by_server.get(_server_of(s.name), 0) + 1
            full = "\n".join(
                f"* {server}: {count} tools — find them with ToolSearch keywords"
                for server, count in sorted(by_server.items())
            )
    return (
        "<system-reminder>\n"
        + _CATALOG_PREAMBLE.format(header=DEFERRED_CATALOG_HEADER)
        + f"\n{full}\n</system-reminder>"
    )


def parse_deferred_catalog(text: str) -> set[str]:
    """Tool names announced by catalog blocks inside ``text``.

    The inverse of :func:`render_deferred_catalog`, used on session resume: a
    fresh engine reads what a previous process already announced instead of
    replaying the whole catalog into a session that already contains it.
    """
    names: set[str] = set()
    if not text or DEFERRED_CATALOG_HEADER not in text:
        return names
    start = 0
    while True:
        found = text.find(DEFERRED_CATALOG_HEADER, start)
        if found < 0:
            return names
        end = text.find("</system-reminder>", found)
        block = text[found:] if end < 0 else text[found:end]
        names.update(_CATALOG_NAME_RE.findall(block))
        start = found + len(DEFERRED_CATALOG_HEADER) if end < 0 else end

SPEC = ToolSpec(
    name="ToolSearch",
    description=(
        "Find and load deferred tools (e.g. MCP server tools) so they can be "
        "called. Deferred tools are known by name and description but their "
        "schemas stay out of context until loaded here. Query forms: "
        "'select:<name>[,<name>...]' loads those exact tools; any other text "
        "is a keyword search over names + descriptions. Once a tool's schema "
        "appears in the result, it is callable from your next reply."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": (
                    "'select:name1,name2' for exact tools, or keywords "
                    "(e.g. 'browser screenshot')."
                ),
            },
            "max_results": {
                "type": "integer",
                "description": "Maximum keyword-search matches to load (default 5).",
            },
        },
        "required": ["query"],
    },
    risk="safe",
    # Engine-injected registry handle; absent the engine drops the tool from
    # the request schemas (nothing to search without a registry).
    requires_extra=("tool_registry",),
)


def _spec_line(spec: ToolSpec) -> str:
    return json.dumps(
        {
            "name": spec.name,
            "description": spec.description,
            "input_schema": spec.input_schema,
        },
        ensure_ascii=False,
    )


async def handler(input: dict[str, Any], ctx: ToolCtx) -> ToolOutcome:
    registry = (ctx.extra or {}).get("tool_registry")
    if registry is None:
        return ToolOutcome(
            text="ToolSearch unavailable: no tool registry injected by the host.",
            is_error=True,
        )
    query = str(input.get("query", "")).strip()
    if not query:
        return ToolOutcome(text="empty query", is_error=True)
    limit = int(input.get("max_results") or 5)
    # Session tool restriction (CE12): a tool the session may not use is
    # reported exactly like one that does not exist — the restriction is not a
    # thing for the model to argue with.
    tool_filter = (ctx.extra or {}).get("tool_filter")

    def visible(name: str) -> bool:
        return tool_filter is None or bool(tool_filter(name))

    if query.startswith("select:"):
        names = [n.strip() for n in query[len("select:"):].split(",") if n.strip()]
        missing = [n for n in names if registry.get(n) is None or not visible(n)]
        if missing:
            return ToolOutcome(
                text=f"unknown tool(s): {', '.join(missing)}", is_error=True
            )
        specs = [registry.load_schema(n) for n in names]
    else:
        specs = [s for s in registry.search_tools(query) if visible(s.name)][:limit]
        specs = [registry.load_schema(s.name) for s in specs]

    if not specs:
        return ToolOutcome(
            text=f"no tools matched {query!r}; try different keywords."
        )
    lines = "\n".join(_spec_line(s) for s in specs)
    return ToolOutcome(
        text=(
            f"Loaded {len(specs)} tool(s) — callable from your next reply:\n{lines}"
        )
    )
