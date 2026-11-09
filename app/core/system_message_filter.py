"""Shared filtering for internal/transient system-message subtypes.

Some system-message subtypes are never shown as standalone system cards:

  - ``thinking_tokens`` — live thinking-token estimates.
  - ``task_progress`` — a subagent's progress updates.

The live stream drops them (``query_engine`` adds this set to its
suppressed subtypes), but persisted transcripts and older DB rows can
still carry them. The history loaders (DB, JSONL, backfill) replay every
persisted system entry, so they filter these subtypes here to make reload
match live behavior.

This is deliberately conservative: only subtypes that are provably
routed-and-returned during live streaming are listed. Legitimately shown
system messages (init/setup, errors, compact boundaries, hook responses,
etc.) are NOT filtered.
"""

# System subtypes that are suppressed (routed-and-returned) during live
# streaming and must therefore also be hidden on history reload.
HIDDEN_SYSTEM_SUBTYPES = frozenset({
    "thinking_tokens",
    "task_progress",
})


def thinking_progress_frame(data):
    """The live working-line frame for one ``thinking_tokens`` message, or None.

    The frame is its own type so it never becomes a transcript row: the turn
    recorder does not keep it, and reload keeps hiding any ``thinking_tokens``
    row (#1271). The event behind it does reach the engine's event log, batched
    and prunable, at most about once a second.
    """
    estimate = data.get("estimated_tokens") if isinstance(data, dict) else None
    if isinstance(estimate, bool) or not isinstance(estimate, (int, float)) or estimate <= 0:
        return None
    return {"type": "thinking_progress", "estimated_tokens": int(estimate)}


def is_hidden_system_subtype(subtype) -> bool:
    """Return True if a system subtype should never be shown to the user.

    These are internal/transient subtypes that the live stream routes into
    other UI elements (thinking counter, subagent progress) instead of
    rendering as a standalone system card.
    """
    return subtype in HIDDEN_SYSTEM_SUBTYPES
