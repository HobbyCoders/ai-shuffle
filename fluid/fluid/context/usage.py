"""Context-window breakdown — the numbers behind ``loop.get_context_usage()``.

Shape parity with the Claude Agent SDK's ``ContextUsageResponse`` (what Claude
Code's ``/context`` renders), so a host app can drive one widget from either
engine leg: the delegated (claude CLI) loop proxies the CLI's own breakdown
verbatim, and the native loop computes the same shape here.

Everything is estimated with the shared chars/4 heuristic
(:func:`fluid.context.trim.estimate_tokens_of_text`) — the same estimator the
trimmer triggers on. No tokenizer, no counting round-trip, so this is cheap
enough to call between every turn.

Categories
----------
``System prompt``   base prompt + environment block + app "extra" blocks.
``Memory files``    the instruction stack (CLAUDE.md / AGENTS.md), summed.
                    The category row carries only the total, exactly like the
                    CLI's; per-file detail rides alongside in the top-level
                    ``memoryFiles`` key (see :class:`MemoryFileUsage`).
``Skills``          the progressive-disclosure skills index. Its own category
                    rather than part of "System prompt": it is a distinct
                    compiled section, and it grows with the skill catalog
                    rather than with the agent's configuration.
``Tools``           serialized JSON schemas of the tools actually sent.
``Deferred tools``  schemas of known-but-unloaded tools (ToolSearch's catalog).
                    Marked ``isDeferred`` and — matching Claude Code — EXCLUDED
                    from ``totalTokens``: those schemas are not in the prompt,
                    they are what could still be loaded into it. Counting them
                    would understate free space.
``Messages``        the TRIMMED history view (what a request would actually
                    carry), not the full stored tree.
``Free space``      ``maxTokens - totalTokens``, floored at 0. Omitted entirely
                    when the context window is unknown.

Empty categories are omitted (a session with no skills shows no "Skills" row).

There is no "Autocompact buffer" category: fluid has cache-safe trimming, not
autocompaction, and ``isAutoCompactEnabled`` is reported False rather than
fabricating a buffer the engine would never apply.

Palette — fixed hex per category, stable across calls so a UI can colour rows
without a legend lookup, and distinct enough to stay readable side by side:

    System prompt   #6C5CE7  violet
    Memory files    #00B894  green
    Skills          #0984E3  blue
    Tools           #FDCB6E  amber
    Deferred tools  #B2BEC3  grey   (deferred → deliberately muted)
    Messages        #E17055  orange
    Free space      #2D3436  near-black
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Iterable, Sequence

from fluid.context.compiler import PromptSection
from fluid.context.trim import estimate_tokens_of_messages, estimate_tokens_of_text
from fluid.messages import Message

SYSTEM_PROMPT = "System prompt"
MEMORY_FILES = "Memory files"
SKILLS = "Skills"
TOOLS = "Tools"
DEFERRED_TOOLS = "Deferred tools"
MESSAGES = "Messages"
FREE_SPACE = "Free space"

CATEGORY_COLORS: dict[str, str] = {
    SYSTEM_PROMPT: "#6C5CE7",
    MEMORY_FILES: "#00B894",
    SKILLS: "#0984E3",
    TOOLS: "#FDCB6E",
    DEFERRED_TOOLS: "#B2BEC3",
    MESSAGES: "#E17055",
    FREE_SPACE: "#2D3436",
}

# Sections that make up the "System prompt" category (memory/skills get their own).
_SYSTEM_KINDS = ("base", "environment", "extra")

# Discovery scope → the CLI's ``memoryFiles[].type`` vocabulary. Claude Code
# labels an instruction file by where it came from: "User" (the user's home
# config), "Project" (the working tree), "Managed" (enterprise policy — fluid
# discovers no such scope, so it is never emitted). A nested CLAUDE.md is still
# a file in the tree, so it labels "Project" too.
_MEMORY_TYPES = {"global": "User", "project": "Project", "nested": "Project"}
_DEFAULT_MEMORY_TYPE = "Project"


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


def tools_schema_tokens(tools_api: Iterable[dict[str, Any]] | None) -> int:
    """Estimated cost of a tool list as the provider receives it (its JSON)."""
    tools = list(tools_api or ())
    if not tools:
        return 0
    return estimate_tokens_of_text(json.dumps(tools, default=str))


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


def build_context_usage(
    *,
    sections: Sequence[PromptSection] = (),
    system_text: str | None = None,
    history: Sequence[Message] = (),
    tools_api: Sequence[dict[str, Any]] = (),
    deferred_tools_api: Sequence[dict[str, Any]] = (),
    model: str,
    context_window: int | None = None,
) -> dict[str, Any]:
    """Assemble the breakdown dict (see the module docstring for the rules).

    ``sections`` is a compiled prompt's labeled sections. When a caller only has
    the joined prompt string (an app that passed ``system=`` straight through,
    never compiling), pass it as ``system_text`` instead: the whole thing then
    prices as "System prompt", with no memory/skills split to be had.
    """
    system_tokens = sum(
        estimate_tokens_of_text(s.text) for s in sections if s.kind in _SYSTEM_KINDS
    )
    if not sections and system_text:
        system_tokens = estimate_tokens_of_text(system_text)
    memory = memory_file_usage(sections)
    memory_tokens = sum(m.tokens for m in memory)
    skills_tokens = sum(
        estimate_tokens_of_text(s.text) for s in sections if s.kind == "skills"
    )
    tool_tokens = tools_schema_tokens(tools_api)
    deferred_tokens = tools_schema_tokens(deferred_tools_api)
    message_tokens = estimate_tokens_of_messages(list(history))

    counted = [
        (SYSTEM_PROMPT, system_tokens),
        (MEMORY_FILES, memory_tokens),
        (SKILLS, skills_tokens),
        (TOOLS, tool_tokens),
        (MESSAGES, message_tokens),
    ]
    total = sum(tokens for _, tokens in counted)

    categories: list[dict[str, Any]] = [
        {"name": name, "tokens": tokens, "color": CATEGORY_COLORS[name]}
        for name, tokens in counted
        if tokens > 0
    ]
    if deferred_tokens > 0:
        # Outside the accounting on purpose — see the module docstring.
        categories.append({
            "name": DEFERRED_TOOLS, "tokens": deferred_tokens,
            "color": CATEGORY_COLORS[DEFERRED_TOOLS], "isDeferred": True,
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
        # fluid trims tool results; it never autocompacts. Reported False rather
        # than inventing an "Autocompact buffer" the engine would never apply.
        "isAutoCompactEnabled": False,
        # Per-file detail behind the "Memory files" category. Always present —
        # the CLI's schema makes it a required list, so an empty stack is [],
        # never a missing key, and a host widget needs no key check.
        "memoryFiles": [m.to_dict() for m in memory],
    }
