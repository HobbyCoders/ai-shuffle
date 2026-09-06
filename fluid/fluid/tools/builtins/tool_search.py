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

import difflib
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


def _catalog_text(text: str) -> str:
    """One description line made safe for the trusted catalog block."""
    from fluid.tools.hardening import defang_harness_markers

    text = defang_harness_markers(text)
    return " ".join(text.split())  # one line: no newline can start a new entry


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
    # The block is a trusted system-reminder; a server's description is not.
    # Defang the harness markers and keep each entry on its own line, so a
    # description cannot close the block or forge an entry of its own.
    full = "\n".join(
        f"- {s.name}: {_catalog_text(_one_line(s.description))}" for s in ordered
    )
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
        "'select:<name>[,<name>...]' loads those exact tools — either the full "
        "wire name (mcp__browser__browser_navigate) or the leaf name "
        "(browser_navigate) that search_tools and the code surface print; "
        "names it does not recognise are reported with the closest match and "
        "the rest still load. Any other text is a keyword search over names + "
        "descriptions. Once a tool's schema appears in the result, it is "
        "callable from your next reply."
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


# ── code-mode rendering ──
# Called from inside run_python, the wire name (``mcp__browser__navigate``) is
# exactly the wrong thing to print: the kernel mounts tools under the leaf name
# (``navigate``), the wire name is not importable, and "callable from your next
# reply" is false — a loaded tool is importable in the very next cell. One
# audited session burned twelve model calls on that gap.


def _kernel_name(tool_name: str) -> str:
    """The leaf the code-mode VFS mounts a tool under (``fluid.codemode.projection``)."""
    return tool_name.split("__")[-1]


def _import_lines(specs: Sequence[ToolSpec]) -> str:
    out = []
    for spec in specs:
        leaf = _kernel_name(spec.name)
        wire = f"  (wire name: {spec.name})" if leaf != spec.name else ""
        out.append(f"- {leaf}: from tools.{leaf} import {leaf}{wire}")
    return "\n".join(out)


# ── select: name resolution ──
# The discovery surface and the load surface used to speak different languages.
# ``search_tools`` / ``read_tool_docs`` / ``from tools.X import X`` all name a
# tool by its LEAF (``browser_navigate``); ``select:`` resolved through
# ``registry.get``, which knows only the WIRE name
# (``mcp__browser__browser_navigate``). Six audited sessions in one day asked
# for the leaf names the code surface had just printed at them and got a bare
# "unknown tool(s)" — each raise aborting the enclosing cell and the calls
# queued behind it. Both spellings resolve here now, through the same
# projection the kernel mounts, so the leaf a session was taught is the leaf
# that loads.


def _projection(registry: Any, tool_filter: Any) -> Any:
    """The code-mode projection over this session's visible tools, or None."""
    from fluid.codemode.projection import Projection

    try:
        specs = list(registry.list_tools(loaded_only=False))
    except Exception:  # noqa: BLE001 - a registry double without the accessor
        return None
    try:
        return Projection(specs, tool_filter=tool_filter)
    except Exception:  # noqa: BLE001
        return None


def _candidates(registry: Any, projection: Any, visible: Any) -> list[str]:
    """Every name a ``select:`` may legally use — leaves and wire names."""
    names: list[str] = []
    try:
        names += [s.name for s in registry.list_tools(loaded_only=False) if visible(s.name)]
    except Exception:  # noqa: BLE001
        pass
    if projection is not None:
        names += projection.leaves()
    return sorted(set(names))


def _did_you_mean(name: str, candidates: Sequence[str]) -> str:
    """Read's "did you mean" clause, for tool names."""
    close = difflib.get_close_matches(name, candidates, n=3, cutoff=0.6)
    return f" (did you mean: {', '.join(close)})" if close else ""


def _resolve_selection(
    names: Sequence[str], registry: Any, projection: Any, visible: Any
) -> tuple[dict[str, str], list[str]]:
    """``({wire_name: as_requested}, unresolved)`` for one ``select:`` list."""
    resolved: dict[str, str] = {}
    missing: list[str] = []
    for name in names:
        if registry.get(name) is not None and visible(name):
            resolved.setdefault(name, name)
            continue
        spec = projection.spec_for(name) if projection is not None else None
        if spec is not None and visible(spec.name):
            resolved.setdefault(spec.name, name)
            continue
        missing.append(name)
    return resolved, missing


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

    note = ""
    if query.startswith("select:"):
        names = [n.strip() for n in query[len("select:"):].split(",") if n.strip()]
        projection = _projection(registry, tool_filter)
        resolved, missing = _resolve_selection(names, registry, projection, visible)
        # Partial success: one mistyped name used to throw away the whole call,
        # including the names that were right.
        if not resolved:
            candidates = _candidates(registry, projection, visible)
            listing = ", ".join(n + _did_you_mean(n, candidates) for n in missing)
            return ToolOutcome(
                text=(
                    f"unknown tool(s): {listing}. Both the leaf name "
                    "(browser_navigate) and the full wire name "
                    "(mcp__browser__browser_navigate) are accepted here — "
                    "search by keyword to see what exists."
                ),
                is_error=True,
            )
        if missing:
            candidates = _candidates(registry, projection, visible)
            listing = ", ".join(n + _did_you_mean(n, candidates) for n in missing)
            note = f"\nNot loaded — no such tool: {listing}."
        specs = [registry.load_schema(n) for n in resolved]
        # A leaf that resolved to a different wire name: on the JSON surface
        # the call must use the wire name, and nothing else would say so.
        renamed = [
            f"{asked} → {wire}" for wire, asked in resolved.items() if asked != wire
        ]
        if renamed and not (ctx.extra or {}).get("in_kernel_call"):
            note += (
                "\nLoaded by leaf name; call these by their full name: "
                + ", ".join(renamed)
                + "."
            )
    else:
        specs = [s for s in registry.search_tools(query) if visible(s.name)][:limit]
        specs = [registry.load_schema(s.name) for s in specs]

    if not specs:
        return ToolOutcome(
            text=f"no tools matched {query!r}; try different keywords."
        )
    lines = "\n".join(_spec_line(s) for s in specs)
    if (ctx.extra or {}).get("in_kernel_call"):
        head = (
            f"Loaded {len(specs)} tool(s). In this session tools are called "
            "from run_python, so import each one by its kernel name:\n"
            f"{_import_lines(specs)}\n"
            "Schemas (the wire form; the kernel stub takes the same "
            f"parameters):\n{lines}"
        )
    else:
        head = f"Loaded {len(specs)} tool(s) — callable from your next reply:\n{lines}"
    return ToolOutcome(text=head + note)
