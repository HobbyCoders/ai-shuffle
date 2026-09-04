"""Context-window breakdown — the numbers behind ``loop.get_context_usage()``.

Shape parity with the Claude Agent SDK's ``ContextUsageResponse`` (what Claude
Code's ``/context`` renders), so a host app can drive one widget from either
engine leg: the delegated (claude CLI) loop proxies the CLI's own breakdown
verbatim, and the native loop computes the same shape here.

Everything is estimated with the shared chars/4 heuristic
(:func:`fluid.context.trim.estimate_tokens_of_text`) — the same estimator the
trimmer triggers on. No tokenizer, no counting round-trip, so this is cheap
enough to call between every turn.

The estimate is a stand-in, not the truth: once a call has actually been made,
the provider's own prompt accounting is available and ``measured_prompt_tokens``
replaces the guessed total (the estimator can be an order of magnitude off on a
history full of images and tool-result blobs). ``measured`` in the response says
which of the two a host is looking at.

Categories — the same labels Claude Code's ``/context`` uses
------------------------------------------------------------
``System prompt``   base prompt + environment block + app "extra" blocks + the
                    agent-types index (CE7 — the index is prompt text; the
                    definitions behind it are not in the prompt at all).
                    A section kind this module does not know also lands
                    here rather than contributing zero.
                    Host-contributed ``tools`` sections WITHOUT a ``tool_name``
                    also ride here — an unattributable block of prose stays
                    honest instead of being priced as some tool's schema.
``Memory files``    the native AGENTS.md instruction stack, summed.
                    The category row carries only the total, exactly like the
                    CLI's; per-file detail rides alongside in the top-level
                    ``memoryFiles`` key (see :class:`MemoryFileUsage`).
``Skills``          the progressive-disclosure skills index. Its own category
                    rather than part of "System prompt": it is a distinct
                    compiled section, and it grows with the skill catalog
                    rather than with the agent's configuration.
``System tools``    serialized JSON schemas of the loaded bare-named builtins
                    (``Read``, ``Edit``, …). Per-tool detail: ``systemTools``.
                    Under code mode the schemas are not sent — their place in
                    the prompt is taken by host-contributed ``tools`` sections
                    (one per tool, carrying ``tool_name``); each prices here or
                    under "MCP tools" by the same naming convention, so a
                    code-mode session's tool rows match a direct-tool session's.
``MCP tools``       loaded ``mcp__<server>__<tool>`` schemas (MCP + in-proc
                    servers — the naming convention IS the classifier, same as
                    Claude Code's). Per-tool detail: ``mcpTools``.
``Custom agents``   agent definitions the HOST injected into this session's
                    prompt, priced from its descriptors. Native
                    ``.agents/agents`` definitions discovered by the loop are
                    not here: their bodies are worker prompts that never enter
                    this context, and the index line that does is already
                    "System prompt" text. Dispatch stays host-app territory
                    (rulings A6/A8). Per-agent detail: ``agents``.
``Messages``        the TRIMMED history view (what a request would actually
                    carry), not the full stored tree.
``MCP tools (deferred)`` / ``System tools (deferred)``
                    schemas of known-but-unloaded tools (ToolSearch's catalog),
                    split by the same naming convention. Marked ``isDeferred``
                    and — matching Claude Code — EXCLUDED from ``totalTokens``:
                    those schemas are not in the prompt, they are what could
                    still be loaded into it. Counting them would understate
                    free space.
``Free space``      ``maxTokens - totalTokens``, floored at 0. Omitted entirely
                    when the context window is unknown.

Empty categories are omitted (a session with no skills shows no "Skills" row).

Per-item detail keys (SDK ``ContextUsageResponse`` parity) — always present so
a host widget needs no key checks; an empty source is ``[]``, never missing:

``memoryFiles``           ``[{path, type, tokens}]``
``systemTools``           ``[{name, tokens}]`` — loaded builtins
``mcpTools``              ``[{name, serverName, tokens, isLoaded}]`` — loaded
                          AND deferred MCP tools (deferred → ``isLoaded`` False,
                          same list the CLI ships)
``deferredBuiltinTools``  ``[{name, tokens}]`` — deferred bare-named tools
``agents``                ``[{agentType, source, tokens}]``

Each category row equals the sum of its own detail list, so a UI can expand a
row without the numbers disagreeing.

``gridRows`` (the CLI's pre-rendered visual grid) is NOT fabricated here — it
is a display artifact of the CLI's own renderer; the delegated leg proxies it
when the CLI sends it.

There is no "Autocompact buffer" category: fluid has a mechanical, recoverable
eviction ladder (:mod:`fluid.context.compaction`), not lossy model-summary
autocompaction, so ``isAutoCompactEnabled`` is reported False. What the ladder
has done is reported separately in the ``compaction`` field (digested spans,
epoch stubs, trimmed results, reclaimed tokens) — native leg only; the
delegated leg proxies the CLI's dict verbatim, which lacks it.

Palette — fixed hex per category, stable across calls so a UI can colour rows
without a legend lookup, and distinct enough to stay readable side by side:

    System prompt            #6C5CE7  violet
    Memory files             #00B894  green
    Skills                   #0984E3  blue
    System tools             #FDCB6E  amber
    MCP tools                #E84393  pink
    Custom agents            #00CEC9  teal
    System tools (deferred)  #B2BEC3  grey   (deferred → deliberately muted)
    MCP tools (deferred)     #636E72  slate  (muted, distinct from the above)
    Messages                 #E17055  orange
    Free space               #2D3436  near-black
"""

from __future__ import annotations

import json
from collections.abc import Collection, Sequence
from dataclasses import dataclass
from typing import Any

from fluid.context.compiler import PromptSection
from fluid.context.trim import estimate_tokens_of_messages, estimate_tokens_of_text
from fluid.messages import Message

SYSTEM_PROMPT = "System prompt"
# PromptSection.kind marking host-contributed tool documentation. One section
# per tool, with ``tool_name`` set to the full registry name; the breakdown
# classifies it exactly like a schema in ``tools_api`` would be.
TOOLS_SECTION_KIND = "tools"
MEMORY_FILES = "Memory files"
SKILLS = "Skills"
SYSTEM_TOOLS = "System tools"
MCP_TOOLS = "MCP tools"
CUSTOM_AGENTS = "Custom agents"
SYSTEM_TOOLS_DEFERRED = "System tools (deferred)"
MCP_TOOLS_DEFERRED = "MCP tools (deferred)"
MESSAGES = "Messages"
FREE_SPACE = "Free space"

CATEGORY_COLORS: dict[str, str] = {
    SYSTEM_PROMPT: "#6C5CE7",
    MEMORY_FILES: "#00B894",
    SKILLS: "#0984E3",
    SYSTEM_TOOLS: "#FDCB6E",
    MCP_TOOLS: "#E84393",
    CUSTOM_AGENTS: "#00CEC9",
    SYSTEM_TOOLS_DEFERRED: "#B2BEC3",
    MCP_TOOLS_DEFERRED: "#636E72",
    MESSAGES: "#E17055",
    FREE_SPACE: "#2D3436",
}

# Section kinds priced under a category of their own: memory files, the
# skills index, and host ``tools`` sections (attributed ones price as tools;
# an unattributed one is folded back into system_tokens below). Every OTHER
# kind prices as "System prompt" — base, environment, extra, the agents index
# (ruling CE7: the index is prompt text), and any kind the compiler grows
# later — so no section can contribute zero by being unrecognised.
_ATTRIBUTED_KINDS = frozenset({"memory", "skills", TOOLS_SECTION_KIND})

# Discovery scope → the CLI's ``memoryFiles[].type`` vocabulary. Claude Code
# labels an instruction file by where it came from: "User" (the user's home
# config), "Project" (the working tree), "Managed" (enterprise policy — fluid
# discovers no such scope, so it is never emitted). A nested AGENTS.md is still
# a file in the tree, so it labels "Project" too.
_MEMORY_TYPES = {"global": "User", "project": "Project", "nested": "Project"}
_DEFAULT_MEMORY_TYPE = "Project"

# The Claude Code naming convention the registry keeps for ecosystem compat:
# MCP and in-process-server tools are ``mcp__<server>__<tool>``; builtins keep
# bare names. The prefix therefore IS the System-vs-MCP classifier.
_MCP_PREFIX = "mcp__"


@dataclass(frozen=True, kw_only=True)
class MemoryFileUsage:
    """Per-instruction-file detail behind the ``Memory files`` total.

    :meth:`to_dict` is one entry of the response's ``memoryFiles`` list, whose
    shape the SDK/CLI fixes as ``{path: str, type: str, tokens: int}``.
    """

    path: str
    type: str = _DEFAULT_MEMORY_TYPE
    tokens: int

    def to_dict(self) -> dict[str, Any]:
        return {"path": self.path, "type": self.type, "tokens": self.tokens}


def tool_schema_tokens(tool_api: dict[str, Any]) -> int:
    """Estimated cost of ONE tool as the provider receives it (its JSON).

    Priced per tool — not the joined list — so category totals equal the sum
    of their per-tool detail rows exactly.
    """
    return estimate_tokens_of_text(json.dumps(tool_api, default=str))


def _is_mcp(name: str) -> bool:
    return name.startswith(_MCP_PREFIX)


def _server_name(tool_name: str) -> str:
    # mcp__<server>__<tool> → <server>; malformed names degrade to "".
    parts = tool_name.split("__", 2)
    return parts[1] if len(parts) >= 3 else ""


def tool_section_rows(
    sections: Sequence[PromptSection],
    *,
    mounted: Collection[str] | None = None,
) -> tuple[list[dict[str, Any]], int]:
    """Per-tool rows + unattributed-token count for host ``tools`` sections.

    One section per tool, ``tool_name`` naming the registry entry: the row shape
    matches the schema-derived detail exactly (``systemTools`` /
    ``mcpTools`` entries), so a UI cannot tell them apart and needn't. Sections
    without a usable ``tool_name`` are NOT rows — their tokens are returned as
    the second element for the caller to fold into "System prompt", keeping the
    invariant that every token of the prompt lands in exactly one row.

    ``mounted`` (see :func:`build_context_usage`) names the tools this leg
    actually serves. A section for a tool that is NOT mounted is prose about a
    tool the model cannot call: its tokens are real and stay in the total, but
    they fold into the unattributed bucket rather than being billed as that
    tool's cost, so the breakdown never lists a tool the session does not have.
    """
    rows: list[dict[str, Any]] = []
    unattributed = 0
    for s in sections:
        if s.kind != TOOLS_SECTION_KIND:
            continue
        name = getattr(s, "tool_name", None)
        if not name or (mounted is not None and name not in mounted):
            unattributed += estimate_tokens_of_text(s.text)
            continue
        tokens = estimate_tokens_of_text(s.text)
        if _is_mcp(name):
            rows.append({
                "name": name,
                "serverName": _server_name(name),
                "tokens": tokens,
                "isLoaded": True,
            })
        else:
            rows.append({"name": name, "tokens": tokens})
    return rows, unattributed


def memory_file_usage(sections: Sequence[PromptSection]) -> list[MemoryFileUsage]:
    """Per-file token estimates for the instruction stack, in stack order."""
    return [
        MemoryFileUsage(
            path=s.path or "",
            type=_MEMORY_TYPES.get(s.scope or "", _DEFAULT_MEMORY_TYPE),
            tokens=estimate_tokens_of_text(s.text),
        )
        for s in sections
        if s.kind == "memory"
    ]


def agent_usage(agents: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    """Normalize host-declared agent descriptors into the SDK ``agents`` shape.

    A descriptor names its agent (``agentType`` or ``name``), where it came
    from (``source``, defaulting to "app" — fluid discovered nothing, the host
    declared it), and its cost: an explicit ``tokens`` wins; otherwise the
    injected ``text``/``description`` is estimated.
    """
    out: list[dict[str, Any]] = []
    for a in agents:
        tokens = a.get("tokens")
        if tokens is None:
            tokens = estimate_tokens_of_text(a.get("text") or a.get("description"))
        out.append({
            "agentType": a.get("agentType") or a.get("name") or "",
            "source": a.get("source") or "app",
            "tokens": int(tokens),
        })
    return out


def build_context_usage(
    *,
    sections: Sequence[PromptSection] = (),
    system_text: str | None = None,
    history: Sequence[Message] = (),
    tools_api: Sequence[dict[str, Any]] = (),
    deferred_tools_api: Sequence[dict[str, Any]] = (),
    agents: Sequence[dict[str, Any]] = (),
    model: str,
    context_window: int | None = None,
    compaction: dict[str, int] | None = None,
    measured_prompt_tokens: int | None = None,
    mounted_tools: Collection[str] | None = None,
) -> dict[str, Any]:
    """Assemble the breakdown dict (see the module docstring for the rules).

    ``sections`` is a compiled prompt's labeled sections. When a caller only has
    the joined prompt string (an app that passed ``system=`` straight through,
    never compiling), pass it as ``system_text`` instead: the whole thing then
    prices as "System prompt", with no memory/skills split to be had.

    ``agents`` is host-declared (see :func:`agent_usage`): pass a descriptor
    per agent definition the host injected into the context — and only then,
    or the row would double-count text already priced elsewhere.

    ``measured_prompt_tokens`` is what a provider actually billed for the prompt
    (input + cache-read + cache-creation) on the call this breakdown describes.
    Given it, ``totalTokens`` IS that number instead of a chars/4 guess, and
    "Messages" absorbs the difference — it is the one category whose size the
    estimator gets badly wrong (images, tool-result blobs, cached prefixes), so
    the other rows keep their own estimates and Messages carries the remainder,
    floored at 0. Free space and ``percentage`` then follow from the real total.
    The response says which it was in ``measured``.

    ``mounted_tools`` names the tools this leg actually serves — the
    capability-filtered surface, not the registry. Hosts that assemble
    ``tools_api`` or per-tool prompt sections from registered specs bill tools
    the session cannot call: an audited leg was charged 546 tokens for
    SendMessage and 245 for ListAgents on a host that never wired agent
    messaging, and the same names were advertised in the prompt, so models
    imported them and two workers briefed to coordinate through them could
    not. Given the set, an unmounted tool gets no row: its schema tokens leave
    the total (that schema is not sent) and its prompt-section tokens fall
    back to "System prompt" (that text IS sent, and every token must still
    land somewhere). ``None`` bills everything, as before.
    """
    section_rows, unattributed = tool_section_rows(sections, mounted=mounted_tools)
    mounted = (
        None if mounted_tools is None else frozenset(mounted_tools)
    )

    def _billable(name: str) -> bool:
        return mounted is None or name in mounted

    system_tools_rows = [r for r in section_rows if "serverName" not in r]
    mcp_section_rows = [r for r in section_rows if "serverName" in r]
    system_tokens = sum(
        estimate_tokens_of_text(s.text)
        for s in sections
        if s.kind not in _ATTRIBUTED_KINDS
    ) + unattributed
    if not sections and system_text:
        system_tokens = estimate_tokens_of_text(system_text)
    memory = memory_file_usage(sections)
    memory_tokens = sum(m.tokens for m in memory)
    skills_tokens = sum(
        estimate_tokens_of_text(s.text) for s in sections if s.kind == "skills"
    )

    # Loaded tools, split by the naming convention; per-tool detail kept. A
    # code-mode session sends no schemas — its prompt-carried documentation
    # arrives as ``tools`` sections instead, and those rows join the same lists.
    system_tools = [
        {"name": t.get("name", ""), "tokens": tool_schema_tokens(t)}
        for t in tools_api
        if not _is_mcp(t.get("name", "")) and _billable(t.get("name", ""))
    ] + system_tools_rows
    mcp_tools = [
        {
            "name": t.get("name", ""),
            "serverName": _server_name(t.get("name", "")),
            "tokens": tool_schema_tokens(t),
            "isLoaded": True,
        }
        for t in tools_api
        if _is_mcp(t.get("name", "")) and _billable(t.get("name", ""))
    ] + mcp_section_rows
    deferred_builtin = [
        {"name": t.get("name", ""), "tokens": tool_schema_tokens(t)}
        for t in deferred_tools_api
        if not _is_mcp(t.get("name", "")) and _billable(t.get("name", ""))
    ]
    deferred_mcp = [
        {
            "name": t.get("name", ""),
            "serverName": _server_name(t.get("name", "")),
            "tokens": tool_schema_tokens(t),
            "isLoaded": False,
        }
        for t in deferred_tools_api
        if _is_mcp(t.get("name", "")) and _billable(t.get("name", ""))
    ]

    agent_entries = agent_usage(agents)
    agent_tokens = sum(a["tokens"] for a in agent_entries)
    message_tokens = estimate_tokens_of_messages(list(history))

    counted = [
        (SYSTEM_PROMPT, system_tokens),
        (MEMORY_FILES, memory_tokens),
        (SKILLS, skills_tokens),
        (SYSTEM_TOOLS, sum(t["tokens"] for t in system_tools)),
        (MCP_TOOLS, sum(t["tokens"] for t in mcp_tools)),
        (CUSTOM_AGENTS, agent_tokens),
        (MESSAGES, message_tokens),
    ]
    measured = measured_prompt_tokens is not None and measured_prompt_tokens >= 0
    if measured_prompt_tokens is not None and measured_prompt_tokens >= 0:
        others = sum(tokens for name, tokens in counted if name != MESSAGES)
        if measured_prompt_tokens >= others:
            message_tokens = measured_prompt_tokens - others
            counted = [
                (name, message_tokens if name == MESSAGES else tokens)
                for name, tokens in counted
            ]
        else:
            # The provider billed less than the chars/4 estimate of the fixed
            # rows alone (a compact tokenizer). Scale every row to the billed
            # total so Messages does not vanish behind an over-estimate.
            estimated = others + message_tokens
            scale = measured_prompt_tokens / estimated if estimated else 0.0
            counted = [(name, int(round(tokens * scale))) for name, tokens in counted]
        total = measured_prompt_tokens
    else:
        total = sum(tokens for _, tokens in counted)

    categories: list[dict[str, Any]] = [
        {"name": name, "tokens": tokens, "color": CATEGORY_COLORS[name]}
        for name, tokens in counted
        if tokens > 0
    ]
    for name, entries in (
        (MCP_TOOLS_DEFERRED, deferred_mcp),
        (SYSTEM_TOOLS_DEFERRED, deferred_builtin),
    ):
        tokens = sum(t["tokens"] for t in entries)
        if tokens > 0:
            # Outside the accounting on purpose — see the module docstring.
            categories.append({
                "name": name, "tokens": tokens,
                "color": CATEGORY_COLORS[name], "isDeferred": True,
            })
    if context_window:
        categories.append({
            "name": FREE_SPACE, "tokens": max(0, context_window - total),
            "color": CATEGORY_COLORS[FREE_SPACE],
        })

    percentage: float | None = None
    if context_window:
        percentage = round(total / context_window * 100, 1)

    return {
        "categories": categories,
        "totalTokens": total,
        "maxTokens": context_window,
        "rawMaxTokens": context_window,
        "percentage": percentage,
        "model": model,
        # True when totalTokens came from the provider's own prompt accounting
        # rather than the chars/4 estimator — the one thing a host cannot infer
        # from the SDK-shaped rows themselves.
        "measured": measured,
        # fluid never autocompacts in the CLI's sense (a model-written summary
        # replacing history). Its eviction ladder is mechanical and recoverable
        # — reported via ``compaction`` below, not by fabricating an
        # "Autocompact buffer" the engine would never apply.
        "isAutoCompactEnabled": False,
        # Eviction-ladder stats (native leg only; None when the engine has no
        # compactor or no session yet): digestedSpans/epochStubs/
        # trimmedToolResults/reclaimedTokens.
        "compaction": compaction,
        # Per-item detail behind the category rows. Always present — the CLI's
        # schema makes memoryFiles/mcpTools/agents required lists, so an empty
        # source is [], never a missing key, and a host widget needs no key
        # check. mcpTools carries loaded AND deferred entries (isLoaded flags
        # which), exactly like the CLI's.
        "memoryFiles": [m.to_dict() for m in memory],
        "systemTools": system_tools,
        "mcpTools": mcp_tools + deferred_mcp,
        "deferredBuiltinTools": deferred_builtin,
        "agents": agent_entries,
    }
