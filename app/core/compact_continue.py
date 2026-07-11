"""Context-boundary auto-continue for autonomous turns.

The bundled Claude CLI runs the agentic loop until it decides the NEXT turn
would cross the autocompact threshold. Instead of compacting in place and
continuing, it ends the current agentic loop and DEFERS compaction to the start
of the next query (internal ``willRetriggerNextTurn`` telemetry that never
reaches the SDK). From the app's side the turn simply ends: the final assistant
step was a ``tool_use`` whose ``tool_result`` came back, then the loop stopped
with no assistant follow-up and ``ResultMessage.stop_reason == "tool_use"`` (the
model wanted to keep going). On an overnight autonomous run the session then
sits silent mid-task until a human sends a message — that message is what
triggers the deferred compaction and resumes work.

This module detects that boundary cut at turn end and decides whether to spawn
ONE automatic continuation query. The continuation makes the CLI compact at the
start of the (server-initiated) turn and the model resumes its task.

Gating (all must hold):
  * setting ``auto_continue_on_compact`` is ON (default),
  * the turn ended abnormally — ``stop_reason == "tool_use"`` (not a natural
    ``end_turn``),
  * live context usage is at/over the autocompact threshold,
  * the turn was not user-stopped / interrupted,
  * no user message is already queued for the session (that path resumes work
    on its own),
  * this session has not ALREADY been auto-continued at a boundary since the
    last genuine user message (one-shot guard — never chain nudges).

The one-shot guard resets on the next genuine user message
(:func:`reset_auto_continue`, called from the user-query entry point). If the
continuation turn itself cuts at the boundary again, compaction already ran at
its start, so a second cut means something else is wrong — we do NOT re-nudge,
we log it.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Dict, Optional, Tuple

logger = logging.getLogger(__name__)


# The prompt for the automatic continuation turn. Honest + explicitly labeled so
# it is never mistaken for a human instruction when it renders in the chat.
AUTO_CONTINUE_PROMPT = (
    "[auto-continue] Your previous turn was cut off at the context limit and "
    "the context has now been compacted. Continue the task you were working on, "
    "picking up exactly where you left off."
)

# The abnormal ``ResultMessage.stop_reason`` that marks a turn ended by the
# CLI's lazy-compaction boundary: the model's last step was a tool_use (it
# wanted to keep going) but the agentic loop stopped. A natural completion is
# ``end_turn``; ``max_tokens`` / ``stop_sequence`` / ``None`` are also NOT
# boundary cuts.
_BOUNDARY_STOP_REASON = "tool_use"


# Per-session one-shot guard. A session id is present iff it has already been
# auto-continued at a boundary and no genuine user message has arrived since.
_auto_continued_sessions: set[str] = set()


@dataclass
class AutoContinueDecision:
    """Result of :func:`should_auto_continue`."""

    should_continue: bool
    reason: str
    pre_tokens: Optional[int] = None


# ─── one-shot guard ────────────────────────────────────────────────────────


def mark_auto_continued(session_id: str) -> None:
    """Latch that this session has been auto-continued at a boundary."""
    _auto_continued_sessions.add(session_id)


def has_auto_continued(session_id: str) -> bool:
    """True iff this session is currently latched (already auto-continued)."""
    return session_id in _auto_continued_sessions


def reset_auto_continue(session_id: str) -> None:
    """Clear the one-shot latch — called on the next genuine user message."""
    _auto_continued_sessions.discard(session_id)


# ─── detection primitives ──────────────────────────────────────────────────


def auto_continue_enabled() -> bool:
    """Whether the ``auto_continue_on_compact`` setting is ON (default ON)."""
    from app.core.config import settings
    return bool(getattr(settings, "auto_continue_on_compact", True))


def is_abnormal_boundary_stop(final_metadata: Optional[Dict[str, Any]]) -> bool:
    """True iff the turn ended on the lazy-compaction boundary stop reason.

    The app records ``metadata["stop_reason"]`` from the SDK ``ResultMessage``
    at turn end (query_engine). A boundary cut leaves it ``"tool_use"``; a
    natural completion leaves ``"end_turn"``. Never raises.
    """
    if not final_metadata:
        return False
    return final_metadata.get("stop_reason") == _BOUNDARY_STOP_REASON


async def context_over_threshold(session_id: str) -> Tuple[bool, Optional[int]]:
    """Whether live context usage is at/over the autocompact threshold.

    Returns ``(over, total_tokens)``. Any failure — session not pooled, SDK
    method unavailable, the control call raising — is treated as "don't nudge"
    and returns ``(False, None)``: we never auto-continue on a guess.
    """
    try:
        from app.core.session_pool import get_session_pool

        # peek(): non-spawning, no LRU promotion. None for parked / remote / never
        # opened sessions.
        state = get_session_pool().peek(session_id)
        if not state or not getattr(state, "is_connected", False):
            return False, None
        usage = await state.adapter.get_context_usage()
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "compact-boundary: get_context_usage failed for session=%s: %s",
            session_id, exc,
        )
        return False, None

    if not usage:
        return False, None

    total = usage.get("totalTokens")
    if total is None:
        return False, None

    # Autocompact disabled → the boundary cut cannot be a compaction deferral.
    if usage.get("isAutoCompactEnabled") is False:
        return False, total

    threshold = usage.get("autoCompactThreshold")
    if threshold is None:
        # No explicit threshold surfaced: fall back to maxTokens, which the SDK
        # documents as already reduced by the autocompact buffer.
        threshold = usage.get("maxTokens")
    if not threshold:
        return False, total

    return total >= threshold, total


# ─── the decision ──────────────────────────────────────────────────────────


async def should_auto_continue(
    session_id: str,
    final_metadata: Optional[Dict[str, Any]],
    *,
    is_stopped: bool,
    is_interrupted: bool,
    has_queued_user_message: bool,
) -> AutoContinueDecision:
    """Decide whether to spawn one automatic continuation turn.

    Pure of side effects (does NOT latch the one-shot guard — the caller does
    that only once it commits to spawning). Every guard maps to a distinct
    ``reason`` so the decision is diagnosable in logs / tests.
    """
    if not auto_continue_enabled():
        return AutoContinueDecision(False, "disabled")
    if is_stopped:
        return AutoContinueDecision(False, "stopped")
    if is_interrupted:
        return AutoContinueDecision(False, "interrupted")
    if has_queued_user_message:
        return AutoContinueDecision(False, "queued_message")
    if has_auto_continued(session_id):
        # Second boundary in a row with no user message between — compaction
        # already ran at the continuation's start, so this cut is something
        # else. Never chain nudges.
        return AutoContinueDecision(False, "already_continued")
    if not is_abnormal_boundary_stop(final_metadata):
        return AutoContinueDecision(False, "natural_end")

    over, pre_tokens = await context_over_threshold(session_id)
    if not over:
        return AutoContinueDecision(False, "below_threshold", pre_tokens)

    return AutoContinueDecision(True, "auto_continue", pre_tokens)
