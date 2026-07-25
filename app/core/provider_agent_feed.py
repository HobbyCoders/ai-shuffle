"""Cross-session event forwarding for ProviderAgent workers (chunk P3-A2).

A ProviderAgent worker (``app/core/provider_agent.py``) runs its turn on its
OWN session, with its own :class:`~app.core.turn_recorder.TurnRecorder` and its
own ``session_events`` log. Nothing about that reaches the parent, so before
this module the parent saw only a tool call that returned text after a long
silence.

This module is the tap that makes the worker render as the EXISTING subagent
card. It emits, on the PARENT session's log, the contract triple the frontend
reducer is built from
(``frontend/src/lib/stores/session/v2/reducer.ts:996/:1026/:1094``):

* ``subagent.started``  — when the worker turn is dispatched;
* ``subagent.progress`` — assistant text / tool calls / tool results, bounded;
* ``subagent.completed`` — the terminal status plus a bounded result preview.

Four constraints shape the design; each is load-bearing:

1. **The parent has a FENCED run in flight.** Appending with a fence token we
   do not own raises ``FenceViolation`` (``app/db/session_events.py:73-86``),
   and ``TurnRecorder`` is hard-bound to one ``session_id`` for its whole life
   (``app/core/session_runtime.py:583-586``) so the parent's recorder cannot be
   reused. Every append here goes through
   :func:`app.core.turn_recorder.record_drained_event`, the sanctioned UNFENCED
   path (``run_id=None`` skips the fence check entirely). Seq allocation stays
   with ``session_event_counters``, so parent and worker allocate independently
   and neither can collide with the other.

2. **``record_drained_event`` silently drops anything outside
   ``subagent_*``/``task_*``/``system``** (``app/core/turn_recorder.py:1017``).
   Every event this module emits is deliberately a ``subagent_*`` pump shape
   (or the ``system``/``task_updated`` settle frame), so nothing is filtered
   out, and translation to the contract rows is the SAME
   ``_translate_shared`` the in-turn recorder uses — no second translator.

3. **The worker's own log must stay complete.** We only ever READ it
   (``session_events.get_events`` from a cursor). The worker session IS the
   persisted full report (plan §2 C.5); this module duplicates a bounded
   summary stream to the parent, it does not divert the worker's stream.

4. **Observation is a poll, not a hook.** There is no in-process stream from
   another session's turn — the worker runs under its own runtime, possibly on
   a different runner. Tailing the durable log from a cursor is the only
   observation point that works in every placement, needs no new plumbing, and
   cannot perturb the thing it watches.

Volume is bounded on three axes (a chatty worker must not flood the parent's
log): total frames, total forwarded text, and per-frame size. When a cap is
reached a single "progress capped" note is forwarded and the tap goes quiet —
the full detail is always in the worker session.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Bounds (plan §2 C.4: fan-out is UNCAPPED, so per-worker volume must not be)
# ---------------------------------------------------------------------------

#: How often the tap re-reads the worker's event log. The worker's turn runs
#: for seconds-to-minutes, so this is a progress cadence, not a latency budget.
POLL_INTERVAL_SECONDS = 1.5

#: Rows pulled per ``get_events`` call, and how many such calls one drain may
#: make. Together they cap a single tick at 2000 rows so a worker that dumps a
#: huge batch cannot monopolise the loop.
DRAIN_BATCH = 500
MAX_DRAIN_BATCHES = 4

#: Hard ceiling on ``subagent.progress`` frames forwarded for ONE worker.
MAX_PROGRESS_FRAMES = 200

#: Hard ceiling on total assistant text forwarded for ONE worker.
MAX_PROGRESS_TEXT_CHARS = 20_000

#: Per-frame ceilings.
MAX_CHUNK_CHARS = 2_000
MAX_TOOL_INPUT_CHARS = 800
MAX_TOOL_RESULT_CHARS = 2_000

#: Ceiling on the ``result_preview`` carried by ``subagent.completed``.
MAX_RESULT_PREVIEW_CHARS = 2_000

_CAP_NOTE = (
    "[Live progress capped — the worker is still being recorded in full in its "
    "own session.]"
)

#: Worker event types the tap forwards. Everything else in the worker's log
#: (thinking, usage, turn lifecycle, its own nested subagent rows, system
#: notes) is deliberately NOT mirrored: the parent card is a summary surface.
_TEXT_TYPES = ("assistant.text_delta", "message.assistant")
_TOOL_USE_TYPE = "tool.use"
_TOOL_RESULT_TYPE = "tool.result"


def _clip(text: Optional[str], limit: int) -> str:
    """Clip ``text`` to ``limit`` characters, marking the cut."""
    body = text or ""
    if len(body) <= limit:
        return body
    return body[: limit - 1] + "…"


def _bound_tool_input(value: Any) -> Any:
    """Recursively clip long string leaves out of a tool input dict.

    The worker's own log keeps the full input; the parent's card only needs
    enough to identify the call (the card renders ``file_path``/``command``/
    ``pattern``-style summaries).
    """
    if isinstance(value, str):
        return _clip(value, MAX_TOOL_INPUT_CHARS)
    if isinstance(value, dict):
        return {k: _bound_tool_input(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_bound_tool_input(v) for v in value[:20]]
    return value


class WorkerFeed:
    """Forwards ONE worker session's progress into its parent's event feed.

    ``task_id`` is the join key for the whole surface and defaults to the
    worker's session id — which is also the task registry's task id
    (``task_registry.start_task(parent, worker_session_id, "agent", ...)``).
    The registry entry's ``tool_use_id`` is set to the SAME value by
    :func:`app.core.provider_agent.dispatch_worker`, because that is what the
    frontend actually joins on: the ``/live`` spinner overlay matches
    ``activeTasks[].toolUseId`` against the card's ``toolId``
    (``SubagentMessage.svelte:37-44`` → ``utils/subagent-status.ts``), and the
    terminal ``task.updated`` settle joins the same way
    (``reducer.ts:1203``). One id everywhere, no orphan cards.

    Every method is best-effort and never raises: a forwarding failure must
    degrade the parent's card, never the worker or the parent's turn.
    """

    def __init__(
        self,
        *,
        parent_session_id: str,
        worker_session_id: str,
        task_id: Optional[str] = None,
        label: Optional[str] = None,
        provider_id: Optional[str] = None,
        model_id: Optional[str] = None,
        prompt: Optional[str] = None,
    ) -> None:
        self.parent_session_id = parent_session_id
        self.worker_session_id = worker_session_id
        self.task_id = task_id or worker_session_id
        self.label = label
        self.provider_id = provider_id
        self.model_id = model_id
        self.prompt = prompt

        self._cursor = 0
        self._frames = 0
        self._text_chars = 0
        self._capped = False
        self._started = False
        self._completed = False
        self._follow_task: Optional[asyncio.Task] = None

    # -- properties used by tests / diagnostics ---------------------------

    @property
    def frames_forwarded(self) -> int:
        return self._frames

    @property
    def capped(self) -> bool:
        return self._capped

    # -- emit -------------------------------------------------------------

    async def _emit(self, event: Dict[str, Any]) -> None:
        """Append ONE pump-shaped event to the PARENT, unfenced.

        ``record_drained_event`` is the only append path used here: it is
        unfenced by construction (``run_id=None``), it reuses the in-turn
        translator, and its type filter accepts every shape we emit.
        """
        from app.core.turn_recorder import record_drained_event

        try:
            await record_drained_event(self.parent_session_id, event)
        except Exception:  # noqa: BLE001 — never break the worker over a card
            logger.warning(
                "ProviderAgent feed: emit failed for parent=%s task=%s",
                self.parent_session_id, self.task_id, exc_info=True,
            )

    async def started(self) -> None:
        """Emit ``subagent.started`` — this is what CREATES the card.

        Idempotent: a second call is a no-op (the reducer is idempotent too,
        but a duplicate row would still cost a write).
        """
        if self._started:
            return
        self._started = True
        description = self.label or " · ".join(
            p for p in (self.provider_id, self.model_id) if p
        ) or "ProviderAgent worker"
        await self._emit({
            "type": "subagent_start",
            # ``_translate_shared`` reads ``tool_id or agent_id`` for the
            # contract's task_id; both are set so either lookup lands on the
            # same join key.
            "tool_id": self.task_id,
            "agent_id": self.task_id,
            # Unknown agent types render as their raw string badge on the card
            # (SubagentMessage.getModelBadge), so the model id is the useful
            # label here.
            "agent_type": self.model_id or "provider-agent",
            "description": description,
            "prompt": self.prompt or "",
        })

    async def completed(
        self,
        status: str,
        *,
        result_preview: Optional[str] = None,
        summary: Optional[str] = None,
    ) -> None:
        """Emit the terminal frames for this worker.

        Two frames, deliberately:

        * ``subagent.completed`` settles the card on the live feed and on any
          replay of the parent's log;
        * a terminal ``task.updated`` settle frame (via
          :func:`app.core.turn_recorder.record_task_settle_frame`) is the belt.
          It joins the card through ``tool_use_id`` (``reducer.ts:1203``) and,
          because ``deriveActiveTasks`` keeps only ``running`` summaries, a
          TERMINAL-only summary adds no chip to the active-task strip.

        ``status`` uses the TASK REGISTRY's vocabulary —
        ``completed``/``error``/``killed`` — because it is forwarded verbatim as
        the settle frame's state, and the reducer's terminal test is exactly
        that set (``reducer.ts:1202``); a "failed" there would never settle.
        The ``subagent.completed`` contract event only distinguishes error from
        success, so both failure statuses render the card's error state.

        Idempotent.
        """
        if self._completed:
            return
        self._completed = True
        is_error = status != "completed"
        await self._emit({
            "type": "subagent_done",
            "tool_id": self.task_id,
            "agent_id": self.task_id,
            "is_error": is_error,
            "result": _clip(result_preview, MAX_RESULT_PREVIEW_CHARS),
        })
        try:
            from app.core.turn_recorder import record_task_settle_frame

            await record_task_settle_frame(
                self.parent_session_id,
                self.task_id,
                status,
                summary or self.label,
                tool_use_id=self.task_id,
            )
        except Exception:  # noqa: BLE001
            logger.warning(
                "ProviderAgent feed: settle frame failed for parent=%s task=%s",
                self.parent_session_id, self.task_id, exc_info=True,
            )

    # -- observation ------------------------------------------------------

    async def _read_new_rows(self) -> List[Dict[str, Any]]:
        """Read worker rows past the cursor (bounded). Never raises."""
        from app.db import session_events as session_events_db

        rows: List[Dict[str, Any]] = []
        try:
            for _ in range(MAX_DRAIN_BATCHES):
                batch = await asyncio.to_thread(
                    session_events_db.get_events,
                    self.worker_session_id, self._cursor, DRAIN_BATCH,
                )
                if not batch:
                    break
                rows.extend(batch)
                self._cursor = batch[-1].get("seq") or self._cursor
                if len(batch) < DRAIN_BATCH:
                    break
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            logger.debug(
                "ProviderAgent feed: worker log read failed for %s",
                self.worker_session_id, exc_info=True,
            )
        return rows

    async def drain(self) -> int:
        """Forward everything new in the worker's log. Returns frames emitted.

        Assistant text is COALESCED: all consecutive text rows collapse into one
        ``subagent_chunk`` frame, flushed when a tool row interrupts them or at
        the end of the drain. That is what keeps a token-streaming worker from
        writing one parent row per delta.
        """
        if self._capped or self._completed:
            return 0
        rows = await self._read_new_rows()
        if not rows:
            return 0

        emitted = 0
        pending_text: List[str] = []

        async def _flush_text() -> int:
            nonlocal pending_text
            if not pending_text:
                return 0
            text = "".join(pending_text).strip()
            pending_text = []
            if not text:
                return 0
            if self._text_chars >= MAX_PROGRESS_TEXT_CHARS:
                await self._cap()
                return 0
            remaining = MAX_PROGRESS_TEXT_CHARS - self._text_chars
            text = _clip(text, min(MAX_CHUNK_CHARS, remaining))
            self._text_chars += len(text)
            await self._emit({
                "type": "subagent_chunk",
                "agent_id": self.task_id,
                "content": text,
            })
            self._frames += 1
            return 1

        for row in rows:
            if self._frames >= MAX_PROGRESS_FRAMES:
                pending_text = []
                await self._cap()
                break
            etype = row.get("event_type")
            payload = row.get("payload") or {}

            if etype in _TEXT_TYPES:
                if etype == "message.assistant":
                    if payload.get("kind") not in (None, "text"):
                        continue
                    text = payload.get("content")
                else:
                    text = payload.get("text")
                if isinstance(text, str) and text:
                    pending_text.append(text)
                continue

            if etype == _TOOL_USE_TYPE:
                emitted += await _flush_text()
                if self._capped or self._frames >= MAX_PROGRESS_FRAMES:
                    await self._cap()
                    break
                await self._emit({
                    "type": "subagent_tool_use",
                    "agent_id": self.task_id,
                    "id": payload.get("tool_use_id") or "",
                    "name": payload.get("name") or "",
                    "input": _bound_tool_input(payload.get("input") or {}),
                })
                self._frames += 1
                emitted += 1
                continue

            if etype == _TOOL_RESULT_TYPE:
                emitted += await _flush_text()
                if self._capped or self._frames >= MAX_PROGRESS_FRAMES:
                    await self._cap()
                    break
                await self._emit({
                    "type": "subagent_tool_result",
                    "agent_id": self.task_id,
                    "tool_use_id": payload.get("tool_use_id") or "",
                    "is_error": bool(payload.get("is_error")),
                    "output": _clip(
                        payload.get("content_preview"), MAX_TOOL_RESULT_CHARS
                    ),
                })
                self._frames += 1
                emitted += 1
                continue

        emitted += await _flush_text()
        return emitted

    async def _cap(self) -> None:
        """Latch the cap and forward exactly ONE explanatory note."""
        if self._capped:
            return
        self._capped = True
        await self._emit({
            "type": "subagent_chunk",
            "agent_id": self.task_id,
            "content": _CAP_NOTE,
        })

    # -- follow lifecycle -------------------------------------------------

    def start_following(self) -> None:
        """Spawn the background poll task (idempotent, never raises).

        Safe to call with no running loop (returns quietly) so a caller in a
        synchronous context degrades to "no live progress" rather than failing.
        """
        if self._follow_task is not None:
            return
        try:
            self._follow_task = asyncio.get_running_loop().create_task(
                self._follow_loop()
            )
        except RuntimeError:
            logger.debug("ProviderAgent feed: no running loop; not following")

    async def _follow_loop(self) -> None:
        while True:
            await asyncio.sleep(POLL_INTERVAL_SECONDS)
            try:
                await self.drain()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                logger.debug(
                    "ProviderAgent feed: drain tick failed for %s",
                    self.worker_session_id, exc_info=True,
                )

    async def stop_following(self, *, final_drain: bool = True) -> None:
        """Cancel the poll task and (optionally) drain once more.

        The final drain is what makes the card show the worker's LAST tool call
        and closing text: the worker typically finishes between two ticks.
        Never raises.
        """
        task, self._follow_task = self._follow_task, None
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        if final_drain:
            try:
                await self.drain()
            except Exception:  # noqa: BLE001
                logger.debug(
                    "ProviderAgent feed: final drain failed for %s",
                    self.worker_session_id, exc_info=True,
                )


async def emit_shielded(coro: Any) -> None:
    """Await ``coro`` under a shield, swallowing our own cancellation.

    The teardown hazard this exists for: the ProviderAgent MCP handler runs as
    a tracked child of the parent's SDK client and is CANCELLED when that
    client closes (eviction / interrupt). The terminal frames are emitted from
    a ``finally``, i.e. while the task is already cancelling — and the reducer
    only speculatively settles running cards on ``turn.completed``, NOT on
    ``turn.aborted`` (``reducer.ts:609``), so a card whose
    ``subagent.completed`` is lost to an interrupt would spin forever on
    replay.

    ``asyncio.shield`` promotes the append to an independent task that runs to
    completion regardless; we swallow the ``CancelledError`` raised at OUR await
    so the caller's own ``raise`` still propagates.
    """
    inner = asyncio.ensure_future(coro)
    try:
        await asyncio.shield(inner)
    except asyncio.CancelledError:
        # The shielded append continues on its own — do not cancel it, and do
        # not re-raise here: the caller owns the cancellation semantics.
        pass
    except Exception:  # noqa: BLE001
        logger.debug("ProviderAgent feed: shielded emit failed", exc_info=True)
