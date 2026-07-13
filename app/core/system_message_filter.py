"""Shared filtering for internal/transient system-message subtypes.

The Claude Agent SDK persists some system messages into the session
JSONL/DB even though they are NOT rendered as standalone system cards
during live streaming. During live streaming these subtypes are routed
into other UI elements (thinking-token counter, subagent progress) rather
than rendered as system cards:

  - ``thinking_tokens`` — routed to the live thinking-block token counter
    via ``update_thinking_tokens`` (commit d30ed9c8).
  - ``task_progress`` — routed to its parent subagent's progress via
    ``update_subagent_progress``.

On history reload, the loaders (DB + JSONL) replay every persisted system
entry, so without filtering these would appear as raw JSON system cards
(the frontend ``SystemMessage.svelte`` does not mark them hidden by their
own subtype). Filtering them here on load makes reload match live
behavior exactly.

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


def is_hidden_system_subtype(subtype) -> bool:
    """Return True if a system subtype should never be shown to the user.

    These are internal/transient subtypes that the live stream routes into
    other UI elements (thinking counter, subagent progress) instead of
    rendering as a standalone system card.
    """
    return subtype in HIDDEN_SYSTEM_SUBTYPES
