"""Context-boundary auto-continue for autonomous turns.

The Claude Code CLI runs the agentic loop until it decides the NEXT turn
would cross the autocompact threshold. Instead of compacting in place and
continuing, it ends the current agentic loop and DEFERS compaction to the start
of the next query, without surfacing the deferral (verified against the CLI once
bundled with claude-agent-sdk 0.2.126; re-check on CLI upgrades). From the
app's side the turn simply ends: the final assistant step was a ``tool_use``
whose ``tool_result`` came back, then the loop stopped with no assistant follow-up and ``ResultMessage.stop_reason == "tool_use"`` (the
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

This module exposes only the detection PRIMITIVES
(:func:`auto_continue_enabled`, :func:`is_abnormal_boundary_stop`,
:func:`context_over_threshold`) and the continuation prompt. The composed
decision and the per-session one-shot guard live in the v2 turn scheduler.
"""

from __future__ import annotations

import logging
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


def usage_over_threshold(usage: Optional[Dict[str, Any]]) -> Tuple[bool, Optional[int]]:
    """Pure threshold math over a raw ``get_context_usage()`` payload.

    Returns ``(over, total_tokens)``. Shared by the live auto-continue path and
    the idle-at-edge watchdog (which feeds it a *persisted* snapshot), so the two
    backstops decide "at the compaction edge" identically. Never raises: a
    missing/empty payload or absent ``totalTokens`` yields ``(False, None)``.
    """
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


def usage_fraction(usage: Optional[Dict[str, Any]]) -> Optional[float]:
    """Fraction of the context window in use (``totalTokens / maxTokens``).

    The idle-at-edge watchdog compares this against its configured ~85% fraction.
    Returns ``None`` when either figure is missing/zero so the caller can skip a
    session it cannot evaluate rather than nudge on a guess.
    """
    if not usage:
        return None
    total = usage.get("totalTokens")
    max_tokens = usage.get("maxTokens")
    if not total or not max_tokens:
        return None
    try:
        return float(total) / float(max_tokens)
    except (TypeError, ValueError, ZeroDivisionError):
        return None


def _persisted_usage(session_id: str) -> Optional[Dict[str, Any]]:
    """Unwrap the persisted ``sessions.context_usage_json`` snapshot for a session.

    The stored value is wrapped ``{"captured_at": iso, "data": <raw sdk usage>}``
    (see ``db.sessions.set_session_context_usage``); return the inner ``data``.
    Never raises — returns ``None`` on any read/shape failure.
    """
    try:
        from app.db.sessions import get_session_context_usage_snapshot
        wrapped = get_session_context_usage_snapshot(session_id)
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "compact-boundary: reading persisted context-usage failed for "
            "session=%s: %s", session_id, exc,
        )
        return None
    if not wrapped:
        return None
    data = wrapped.get("data") if isinstance(wrapped, dict) else None
    return data if isinstance(data, dict) else None


async def context_over_threshold(session_id: str) -> Tuple[bool, Optional[int]]:
    """Whether context usage is at/over the autocompact threshold.

    Returns ``(over, total_tokens)``. The degraded-state paths (session not
    pooled/connected, the adapter's control call raising, an empty live payload)
    each log WHY and then FALL BACK to the last persisted ``context_usage_json``
    snapshot before declining. The state is often degraded precisely because the
    session hit the edge, so the last-known usage is the safe fallback. Only when
    neither a live nor a persisted reading exists do we decline (and say so). We
    never auto-continue on a pure guess.
    """
    live_usage: Optional[Dict[str, Any]] = None
    fallback_reason: Optional[str] = None
    try:
        from app.core.session_pool import get_session_pool

        # peek(): non-spawning, no LRU promotion. None for parked / remote / never
        # opened sessions.
        state = get_session_pool().peek(session_id)
        if not state or not getattr(state, "is_connected", False):
            fallback_reason = "session not pooled/connected"
        else:
            live_usage = await state.adapter.get_context_usage()
            if not live_usage:
                fallback_reason = "live get_context_usage returned empty"
    except Exception as exc:  # noqa: BLE001
        fallback_reason = f"get_context_usage raised ({exc})"

    usage = live_usage
    if fallback_reason is not None:
        persisted = _persisted_usage(session_id)
        if persisted is not None:
            logger.info(
                "compact-boundary: live context-usage unavailable for session=%s "
                "(%s) — falling back to persisted snapshot",
                session_id, fallback_reason,
            )
            usage = persisted
        else:
            logger.warning(
                "compact-boundary: cannot verify context threshold for session=%s "
                "(%s, and no persisted snapshot) — declining auto-continue",
                session_id, fallback_reason,
            )
            return False, None

    over, total = usage_over_threshold(usage)
    if not over:
        logger.info(
            "compact-boundary: context under threshold for session=%s "
            "(total=%s) — declining auto-continue", session_id, total,
        )
    return over, total
