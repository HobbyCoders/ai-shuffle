"""Memory — batched edits to the bounded cross-session fact store."""

from __future__ import annotations

from typing import Any

from fluid.context.memory import DEFAULT_CHAR_LIMIT, MemoryStore
from fluid.tools.registry import ToolCtx, ToolOutcome, ToolSpec

SPEC = ToolSpec(
    name="Memory",
    description=(
        "Save durable facts to persistent memory that survives across "
        "sessions. Memory is injected into every future session's prompt, so "
        "keep entries compact and high-signal.\n"
        "\n"
        "HOW: make ALL your changes in ONE call via the `operations` array "
        "(each item: {action: add|remove|replace, content?, old_text?}). The "
        "batch applies atomically and the char limit is checked only on the "
        "FINAL result — so a single call can remove or shorten stale entries "
        "to free room AND add new ones, even when the add alone would "
        "overflow. remove/replace match the one entry containing `old_text`.\n"
        "\n"
        "WHEN: save proactively when the user states a preference, "
        "correction, or personal detail, or you learn a stable fact about "
        "their environment, conventions, or workflow. The best entry is the "
        "one that stops the user repeating themselves.\n"
        "\n"
        "WRITE declarative facts, not instructions to yourself: 'User "
        "prefers concise replies' ages well; 'Always reply concisely' "
        "replays as a directive in later sessions and causes repeated or "
        "wrong work. SKIP: task progress, completed-work logs, anything "
        "stale within a week, anything re-discoverable in one tool call. "
        "Reusable procedures belong in skills, not memory.\n"
        "\n"
        "IF FULL: the batch is rejected with the current entries shown — "
        "reissue as ONE batch that consolidates old entries and adds the new "
        "one together. A success reply is terminal: the update is complete, "
        "do not repeat or double-check it."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "operations": {
                "type": "array",
                "description": "Edits applied atomically, in order.",
                "items": {
                    "type": "object",
                    "properties": {
                        "action": {
                            "type": "string",
                            "enum": ["add", "remove", "replace"],
                        },
                        "content": {
                            "type": "string",
                            "description": (
                                "The entry text (add/replace). One compact "
                                "declarative fact; may be multi-line but "
                                "never contains a '---' line."
                            ),
                        },
                        "old_text": {
                            "type": "string",
                            "description": (
                                "A fragment uniquely identifying the entry "
                                "to remove/replace."
                            ),
                        },
                    },
                    "required": ["action"],
                },
            },
        },
        "required": ["operations"],
    },
    # Static "write" keeps Memory out of plan mode's schema; the per-call
    # classifier reports "safe" so routine saves never hit the on_write
    # prompt — the write is bounded to fluid's own memory file.
    risk="write",
    classify_risk=lambda _input: "safe",
)


def _store_for(ctx: ToolCtx) -> MemoryStore:
    extra = ctx.extra or {}
    return MemoryStore(
        extra.get("memory_file"),
        char_limit=int(extra.get("memory_char_limit") or DEFAULT_CHAR_LIMIT),
    )


async def handler(input: dict[str, Any], ctx: ToolCtx) -> ToolOutcome:
    operations = input.get("operations")
    if not isinstance(operations, list) or not operations:
        return ToolOutcome(text="operations must be a non-empty array", is_error=True)
    ok, message = _store_for(ctx).apply(operations)
    return ToolOutcome(text=message, is_error=not ok)
