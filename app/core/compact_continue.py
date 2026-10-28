"""Compact, then continue: the lifecycle for a turn the context limit cut off.

A delegated (Claude Code) turn can stop because its conversation no longer
fits. The engine says so explicitly: ``DelegatedLoop`` reports the outcome
``needs_compaction`` from the CLI's own ``result.terminal_reason``
(``blocking_limit`` / ``prompt_too_long``; see
``fluid.engine.delegated_proto.context_outcome`` for the evidence and the
fallback for CLIs too old to report it). The adapter carries it into the
durable ``turn.completed`` payload as ``terminal_reason="needs_compaction"``.
Native sessions compact before every model request and never report it.

The turn scheduler acts on that outcome (``turn_scheduler``):

1. **Admit one pair.** When the cut turn settles and nothing the owner sent is
   queued, ONE atomic admission queues a Fluid ``/compact`` followed by the
   continuation turn (the Compact-first paired admission in
   ``app.db.turns.enqueue_turn``). Its ``command_id`` is derived from the cut
   turn, so a retried or replayed settlement finds the pair it already made:
   one continuation per compaction generation, durable across a restart. The
   continuation's ``parent_turn_id`` is the cut turn.
2. **Gate the continuation on the compaction.** Before the continuation
   starts, the scheduler reads what the compaction did, from the log:
   compacted → it runs; failed → the compaction is retried, at most
   :data:`MAX_COMPACTION_ATTEMPTS` times; nothing to reclaim or out of retries
   → ONE emergency compaction, whose ``/compact`` row carries
   ``turns.EMERGENCY_COMPACTION_KEY`` so the delegated leg may let Claude
   Code's summarizer stand in when Fluid has nothing more (owner rule: the
   summarizer is only for an automatic compaction at the context limit, and a
   turn the limit cut off is that case; a ``/compact`` a person sends stays
   Fluid-only). The compaction card says ``summarizer: true`` when it ran.
   That fails too → the continuation is dropped with a note that the chat is
   at its context limit and what the owner can do. A message the owner queued
   meanwhile always wins: the continuation is dropped and the owner's message
   resumes the work. A user Stop aborts both rows (Stop aborts every queued
   turn) and the stop latch keeps anything new from starting.
3. **New generation per cut.** A continuation that is itself cut at the limit
   is a new interrupted turn and gets its own pair, up to
   :data:`MAX_GENERATIONS` in a row with no owner message between them — a
   runaway guard against a task that refills the context faster than
   compaction can reclaim it.

This module holds the vocabulary; the decisions live in the scheduler.
"""

from __future__ import annotations

from typing import Any, Dict, Optional

from fluid.events.model import NEEDS_COMPACTION

# The prompt for the automatic continuation turn. Honest + explicitly labeled so
# it is never mistaken for a human instruction when it renders in the chat.
AUTO_CONTINUE_PROMPT = (
    "[auto-continue] Your previous turn was cut off at the context limit and "
    "the context has now been compacted. Continue the task you were working on, "
    "picking up exactly where you left off."
)

#: The compaction the pair runs first: Fluid's own (manual compaction is
#: Fluid-only), the same command Compact first sends.
COMPACT_PROMPT = "/compact"

#: ``turns.source`` of both rows of the pair (internal: never a queue chip).
SOURCE = "auto_continue"

#: Payload key on both rows: ``{"interrupted_turn_id", "generation",
#: "attempt"}``. Marks the pair and links it to the work it continues.
PAYLOAD_KEY = "compaction_continuation"

#: ``command_id`` prefix of the continuation row; the rest is the cut turn's id.
COMMAND_PREFIX = "compact-continue-"

#: Compaction attempts per generation before the continuation is given up on.
MAX_COMPACTION_ATTEMPTS = 3

#: Consecutive automatic generations (no owner message between them).
MAX_GENERATIONS = 20


def auto_continue_enabled() -> bool:
    """Whether the ``auto_continue_on_compact`` setting is ON (default ON)."""
    from app.core.config import settings
    return bool(getattr(settings, "auto_continue_on_compact", True))


def needs_compaction(terminal: Optional[Dict[str, Any]]) -> bool:
    """True iff a settled turn's terminal payload carries the engine's
    ``needs_compaction`` outcome. Never raises."""
    return bool(terminal) and terminal.get("terminal_reason") == NEEDS_COMPACTION


def usage_fraction(usage: Optional[Dict[str, Any]]) -> Optional[float]:
    """Fraction of the context window in use (``totalTokens / maxTokens``).

    Returns ``None`` when either figure is missing/zero so the caller can skip a
    session it cannot evaluate rather than act on a guess.
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
