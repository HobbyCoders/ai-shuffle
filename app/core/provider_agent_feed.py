"""Cross-session event forwarding for Task-dispatched workers.

A worker (:mod:`app.core.provider_agent`) runs its turn on its OWN session, with
its own :class:`~app.core.turn_recorder.TurnRecorder` and ``session_events``
log. This module is the tap that renders it as the EXISTING subagent card by
emitting, on the PARENT session's log, the frames the v2 ``reducer.ts`` builds
that card from:

* ``subagent.started`` — when the worker turn is dispatched;
* ``task.updated`` (state ``running``) — alongside ``subagent.started``; the
  background-tasks strip, spinner overlay and StopButton read ``activeTasks``
  and so see only ``task.updated`` rows (``feed-controller.ts``
  ``deriveActiveTasks``);
* ``subagent.progress`` — assistant text / tool calls / tool results, bounded;
* ``subagent.completed`` — the terminal status, bounded report and preview,
  paired with the terminal ``task.updated`` that clears the running chip.

Four load-bearing constraints:

1. **The parent has a FENCED run in flight.** Appending with a fence token we
   do not own raises ``session_events.FenceViolation``, and a ``TurnRecorder``
   is bound to one session for life. Every append here goes through
   :func:`app.core.turn_recorder.record_drained_event`, the sanctioned UNFENCED
   path (``run_id=None`` skips the fence check). Seq allocation stays with
   ``session_event_counters``, so parent and worker never collide.
2. **``record_drained_event`` drops anything outside
   ``subagent_*``/``task_*``/``system``.** Every event emitted here is one of
   those shapes, translated by the same ``_translate_shared`` the in-turn
   recorder uses.
3. **The worker's own log is read-only here** (``session_events.get_events``
   from a cursor). The worker session is the full report; this module mirrors
   a bounded summary to the parent.
4. **Observation is a poll, not a hook.** The worker may run on another runner;
   tailing its durable log is the only observation point that works in every
   placement and cannot perturb what it watches.

The poll is ONE loop per PARENT (:class:`WorkerFeedGroup`), batching all of that
parent's workers into one :func:`app.db.session_events.get_events_multi` call
per interval, so a wide fan-out does not multiply DB reads (and pool pressure).

Volume is bounded on three axes: total frames, total forwarded text and
per-frame size. A normal worker turn never hits them. They degrade in two
stages: exhausting the TEXT budget forwards one note and stops text only (tool
activity keeps the card live); exhausting the FRAME budget forwards a final
note and silences the tap. The full detail is always in the worker session.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Bounds (fan-out can be wide, so per-worker volume must be capped)
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
#: Sized as a backstop, not a budget: a substantive worker turn runs hundreds
#: of tool calls (2 frames each), and the native subagent leg is uncapped, so
#: a ceiling a real workload can hit silences exactly the runs the user most
#: wants to watch (the original 200 capped nearly every long worker).
MAX_PROGRESS_FRAMES = 2_000

#: Ceiling on total assistant text forwarded for ONE worker. Reaching it stops
#: TEXT only (one note); tool frames keep streaming under the frame ceiling.
MAX_PROGRESS_TEXT_CHARS = 100_000

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

_TEXT_CAP_NOTE = (
    "[Live text capped — tool activity continues below; the worker's full "
    "output is recorded in its own session.]"
)

#: Worker event types the tap forwards. Everything else in the worker's log
#: (thinking, usage, turn lifecycle, its own nested subagent rows, system
#: notes) is deliberately NOT mirrored: the parent card is a summary surface.
_TEXT_TYPES = ("assistant.text_delta", "message.assistant")
_TOOL_USE_TYPE = "tool.use"
_TOOL_RESULT_TYPE = "tool.result"

#: Worker rows that prove its provider request is streaming or has answered,
#: though the tap does not mirror them: reasoning deltas, tool-argument deltas
#: and a finished model call's usage. A reasoning model can spend minutes on
#: these before its first text or tool call. ``turn.provider_alive``
#: is the weakest of them: the provider accepted the request and its stream was
#: open, nothing more. It is the only row an encrypted-only reasoning model
#: produces before its reasoning item ends. ``turn.started`` is not one: the
#: recorder writes it before any provider request is made.
_PROVIDER_PROGRESS_TYPES = (
    "assistant.thinking_delta", "tool.input_delta", "turn.usage",
    "turn.provider_alive",
)


def _is_provider_progress(row: Dict[str, Any]) -> bool:
    etype = row.get("event_type")
    if etype in _PROVIDER_PROGRESS_TYPES:
        return True
    # A completed non-text block (reasoning) recorded without deltas.
    return etype == "message.assistant" and (
        (row.get("payload") or {}).get("kind") not in (None, "text")
    )


def _clip(text: Optional[str], limit: int) -> str:
    """Clip ``text`` to ``limit`` characters, marking the cut."""
    body = text or ""
    if len(body) <= limit:
        return body
    return body[: limit - 1] + "…"


async def record_continuation_report(
    parent_session_id: str, worker_session_id: str, completion_id: str,
    status: str, result: str,
) -> None:
    """Persist a resumed turn independently of the original feed's done latch.

    Stable event ids deduplicate retries, including after restart. This does
    not settle the task: another continuation may already be running.
    """
    from app.core.turn_recorder import record_drained_event

    await record_drained_event(parent_session_id, {
        "type": "subagent_done", "tool_id": worker_session_id,
        "completion_id": completion_id, "is_error": status != "completed",
        "killed": status == "killed", "result": result,
        "result_preview": _clip(result, MAX_RESULT_PREVIEW_CHARS),
    })


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
    (``SubagentMessage.svelte`` → ``utils/subagent-status.ts``), and the
    terminal ``task.updated`` settle joins the same way (``reducer.ts``).
    One id everywhere, no orphan cards.

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
        launch_tool_use_id: Optional[str] = None,
        agent_name: Optional[str] = None,
    ) -> None:
        self.parent_session_id = parent_session_id
        self.worker_session_id = worker_session_id
        self.task_id = task_id or worker_session_id
        self.label = label
        self.provider_id = provider_id
        self.model_id = model_id
        # The dispatched agent's DISPLAY NAME ("Frontend Designer"). Rides
        # ``subagent.started`` as the card's identity; the model slug stays on
        # ``agent_type`` as a secondary detail.
        self.agent_name = agent_name
        self.prompt = prompt
        # The model's Task tool_use_id (fluid native leg), when known. Rides the
        # subagent.started event so the reducer can absorb the launch tool_use
        # placeholder into the card; NOT a join key for settles (those stay on
        # task_id — the worker session id).
        self.launch_tool_use_id = launch_tool_use_id

        self._cursor = 0
        self._frames = 0
        self._provider_progress = 0
        self._text_chars = 0
        self._capped = False
        self._text_capped = False
        self._started = False
        self._completed = False
        self._group: Optional["WorkerFeedGroup"] = None

    # -- properties used by tests / diagnostics ---------------------------

    @property
    def frames_forwarded(self) -> int:
        return self._frames

    @property
    def provider_progress(self) -> int:
        """Worker rows seen that prove provider activity but are not mirrored.

        Liveness evidence for the first-frame watchdog, never parent output.
        """
        return self._provider_progress

    @property
    def capped(self) -> bool:
        return self._capped

    @property
    def cursor(self) -> int:
        """Highest worker ``seq`` already forwarded (the tail cursor)."""
        return self._cursor

    @property
    def wants_rows(self) -> bool:
        """False once this feed is capped or settled — nothing more to forward."""
        return not self._capped and not self._completed

    # -- emit -------------------------------------------------------------

    async def _emit(self, event: Dict[str, Any]) -> None:
        """Append ONE ``subagent_*``/``task_*``/``system`` event to the PARENT.

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
        """Emit ``subagent.started`` plus a RUNNING ``task.updated``.

        Two frames, deliberately:

        * ``subagent.started`` CREATES the card;
        * a ``task.updated`` with state ``running`` puts the worker on the v2
          active-task surfaces. ``deriveActiveTasks`` is built solely from
          ``task.updated`` summaries, so without this frame the background-tasks
          strip, the spinner overlay and the StopButton would never see the
          worker as running — a terminal summary adds no chip. The frame is
          the ``task_started`` system shape the harness emits for a CLI
          background task, so ``_translate_shared`` maps it with no new code and
          ``record_drained_event`` admits it on the ``task_`` prefix. ``status``
          is explicit ``running`` rather than the per-type ``started`` default,
          because ``deriveActiveTasks`` keeps only ``running``.

        Emitted for EVERY worker, background or synchronous, so every registry
        task shows on the strip.

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
            "worker_session_id": self.worker_session_id,
            "launch_tool_use_id": self.launch_tool_use_id,
            # The card's IDENTITY: the agent's own name. Absent (a bare worker
            # with no agent behind it) the card falls back to the model badge
            # below.
            "agent_name": self.agent_name or "",
            # KEPT for compatibility: every existing consumer (and every card
            # already in a durable log) reads the model id off ``agent_type``,
            # which the card now renders as a secondary chip rather than as the
            # worker's identity.
            "agent_type": self.model_id or "provider-agent",
            "description": description,
            "prompt": self.prompt or "",
        })
        await self._emit({
            "type": "task_started",
            "task_id": self.task_id,
            "status": "running",
            "description": description,
            "summary": self.label,
            "tool_use_id": self.task_id,
        })

    async def completed(
        self,
        status: str,
        *,
        result_preview: Optional[str] = None,
        summary: Optional[str] = None,
        usage: Optional[Dict[str, Any]] = None,
        result: Optional[str] = None,
        result_truncated: bool = False,
    ) -> None:
        """Emit the terminal frames for this worker.

        Two frames, deliberately:

        * ``subagent.completed`` settles the card on the live feed and on any
          replay of the parent's log;
        * a terminal ``task.updated`` settle frame (via
          :func:`app.core.turn_recorder.record_task_settle_frame`) is the belt.
          It joins the card through ``tool_use_id`` (``reducer.ts``) and,
          because ``deriveActiveTasks`` keeps only ``running`` summaries, it is
          also what CLEARS the active-task chip that :meth:`started`'s running
          frame created.

        ``status`` uses the TASK REGISTRY's vocabulary —
        ``completed``/``error``/``killed`` — because it is forwarded verbatim as
        the settle frame's state, and the reducer's terminal test is exactly
        that set (``reducer.ts``); a "failed" there would never settle.
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
            # A stopped worker is not a failure: the card reads "Stopped" and
            # keeps its own row (#1806).
            "killed": status == "killed",
            # The shared recorder bounds the full report for storage AND replay.
            # Keep the historical preview separate for existing consumers.
            "result": result if result is not None else result_preview,
            "result_truncated": result_truncated,
            "result_preview": _clip(result_preview, MAX_RESULT_PREVIEW_CHARS),
            # The worker's authoritative token counts. Without them the card's
            # usage row renders nothing for workers.
            "usage": usage if isinstance(usage, dict) and usage else None,
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
        """Read worker rows past the cursor (bounded). Never raises.

        Pages with a LOCAL cursor and never advances ``self._cursor`` —
        :meth:`forward` is the sole cursor writer, so a read that fails halfway
        through paging cannot leave the cursor past rows that were never
        forwarded, and a concurrent group tick cannot lose rows to a cursor this
        method moved.
        """
        from app.db import session_events as session_events_db

        rows: List[Dict[str, Any]] = []
        cursor = self._cursor
        try:
            for _ in range(MAX_DRAIN_BATCHES):
                batch = await asyncio.to_thread(
                    session_events_db.get_events,
                    self.worker_session_id, cursor, DRAIN_BATCH,
                )
                if not batch:
                    break
                rows.extend(batch)
                cursor = batch[-1].get("seq") or cursor
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
        """Read this worker's new rows and forward them. Returns frames emitted.

        The self-reading path: used for the final drain and by any caller
        without a group. While a :class:`WorkerFeedGroup` is following, the
        group does the reading and calls :meth:`forward` directly.
        """
        if not self.wants_rows:
            return 0
        return await self.forward(await self._read_new_rows())

    async def forward(self, rows: List[Dict[str, Any]]) -> int:
        """Forward already-read worker rows to the parent. Returns frames emitted.

        Assistant text is COALESCED: all consecutive text rows collapse into one
        ``subagent_chunk`` frame, flushed when a tool row interrupts them or at
        the end of the batch. That is what keeps a token-streaming worker from
        writing one parent row per delta.

        Rows at or below the cursor are dropped BEFORE any await, and the cursor
        advances in the same synchronous step, so a row can never be forwarded
        twice even if a group tick and a self-drain overlap.
        """
        if not self.wants_rows:
            return 0
        rows = [r for r in (rows or []) if (r.get("seq") or 0) > self._cursor]
        if not rows:
            return 0
        self._cursor = max(
            (r.get("seq") or 0) for r in rows
        ) or self._cursor

        self._capture_usage(rows)
        self._provider_progress += sum(1 for r in rows if _is_provider_progress(r))

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
            # Emit the coalesced text as consecutive MAX_CHUNK_CHARS frames
            # rather than clipping to one frame: a fast model streams more
            # than one frame's worth between two poll ticks, and anything
            # clipped here was silently lost from the live card. The reducer
            # merges consecutive text children, so N frames render as one run.
            sent = 0
            while text:
                if self._text_capped or self._text_chars >= MAX_PROGRESS_TEXT_CHARS:
                    await self._cap_text()
                    break
                if self._frames >= MAX_PROGRESS_FRAMES:
                    await self._cap()
                    break
                take = min(MAX_CHUNK_CHARS, MAX_PROGRESS_TEXT_CHARS - self._text_chars)
                piece, text = text[:take], text[take:]
                self._text_chars += len(piece)
                await self._emit({
                    "type": "subagent_chunk",
                    "agent_id": self.task_id,
                    "content": piece,
                })
                self._frames += 1
                sent += 1
            return sent

        for row in rows:
            if self._frames >= MAX_PROGRESS_FRAMES:
                pending_text = []
                await self._cap()
                break
            etype = row.get("event_type")
            payload = row.get("payload") or {}

            if etype in _TEXT_TYPES:
                if self._text_capped:
                    continue
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

    def _capture_usage(self, rows: List[Dict[str, Any]]) -> None:
        """Mirror a worker's ``turn.completed`` usage onto its supervisor record.

        This is what makes per-turn worker spend visible WHILE the parent turn
        runs rather than only once the tool call returns.
        ``dispatch_worker`` still records the authoritative final numbers, and
        the supervisor merges rather than accumulates, so seeing the same
        snapshot twice cannot double-count. Purely additive: never raises, and a
        miss only costs live precision.
        """
        try:
            from app.core import provider_agent_supervisor

            for row in rows:
                if row.get("event_type") != "turn.completed":
                    continue
                usage = (row.get("payload") or {}).get("usage")
                if isinstance(usage, dict) and usage:
                    provider_agent_supervisor.record_usage(
                        self.parent_session_id, self.worker_session_id, usage
                    )
        except Exception:  # noqa: BLE001
            logger.debug("ProviderAgent feed: usage capture failed", exc_info=True)

    async def _cap(self) -> None:
        """Latch the FRAME cap and forward exactly ONE explanatory note.

        This is the hard stage: the whole tap goes quiet (``wants_rows``
        turns False).
        """
        if self._capped:
            return
        self._capped = True
        await self._emit({
            "type": "subagent_chunk",
            "agent_id": self.task_id,
            "content": _CAP_NOTE,
        })

    async def _cap_text(self) -> None:
        """Latch the TEXT cap and forward exactly ONE explanatory note.

        The soft stage: further assistant text is dropped, but tool frames
        keep streaming so the card stays live up to the frame ceiling.
        """
        if self._text_capped:
            return
        self._text_capped = True
        await self._emit({
            "type": "subagent_chunk",
            "agent_id": self.task_id,
            "content": _TEXT_CAP_NOTE,
        })

    # -- follow lifecycle -------------------------------------------------

    def resume_at(self, cursor: int) -> None:
        """Re-attach to a card this worker already started, past ``cursor``.

        For a feed rebuilt after a restart (a Task child on another worker
        outlives this process): its card exists in the parent's log, so
        :meth:`started` is not repeated, and rows at or below ``cursor`` were
        either forwarded before the restart or are skipped rather than
        duplicated. The worker session keeps the full transcript.
        """
        self._started = True
        self._cursor = max(self._cursor, int(cursor or 0))

    def start_following(self) -> None:
        """Join this parent's SHARED poll loop (idempotent, never raises).

        There is one loop per PARENT session, not per worker — see the module
        docstring. Safe to call with no running loop (returns quietly) so a
        caller in a synchronous context degrades to "no live progress" rather
        than failing.
        """
        if self._group is not None:
            return
        self._group = join_feed_group(self.parent_session_id, self)

    async def stop_following(self, *, final_drain: bool = True) -> None:
        """Leave the shared poll loop and (optionally) drain once more.

        The final drain is what makes the card show the worker's LAST tool call
        and closing text: the worker typically finishes between two ticks. It is
        a SELF read — the group may already have stopped — and any rows the
        group's last tick already forwarded are filtered by the cursor.
        Never raises.
        """
        group, self._group = self._group, None
        if group is not None:
            await group.remove(self)
        if final_drain:
            try:
                await self.drain()
            except Exception:  # noqa: BLE001
                logger.debug(
                    "ProviderAgent feed: final drain failed for %s",
                    self.worker_session_id, exc_info=True,
                )


# ---------------------------------------------------------------------------
# One poller per PARENT TURN
# ---------------------------------------------------------------------------

#: parent_session_id -> the single live group for that parent.
_GROUPS: Dict[str, "WorkerFeedGroup"] = {}


class WorkerFeedGroup:
    """The shared poll loop for every worker of ONE parent.

    Each tick issues ONE :func:`app.db.session_events.get_events_multi`
    covering every member's cursor, then hands each member its own rows
    through :meth:`WorkerFeed.forward`. Per-worker bounds still apply: a member
    takes at most ``DRAIN_BATCH`` rows per inner batch and at most
    ``MAX_DRAIN_BATCHES`` batches per tick, and its frame/text caps and text
    coalescing live in ``forward``.

    The group starts when its first member joins and stops when its last member
    leaves, so an idle parent runs no loop at all.
    """

    def __init__(self, parent_session_id: str) -> None:
        self.parent_session_id = parent_session_id
        self._feeds: "Dict[str, WorkerFeed]" = {}
        self._task: Optional[asyncio.Task] = None

    @property
    def size(self) -> int:
        return len(self._feeds)

    def add(self, feed: WorkerFeed) -> None:
        """Add a member, starting the loop if this is the first one."""
        self._feeds[feed.worker_session_id] = feed
        # A loop that ended (its event loop closed under it) is restarted, or
        # the new member would never be read.
        if self._task is None or self._task.done():
            try:
                self._task = asyncio.get_running_loop().create_task(self._loop())
            except RuntimeError:
                logger.debug("ProviderAgent feed: no running loop; not following")

    async def remove(self, feed: WorkerFeed) -> None:
        """Drop a member, stopping (and awaiting) the loop when the last leaves."""
        self._feeds.pop(feed.worker_session_id, None)
        if self._feeds:
            return
        _GROUPS.pop(self.parent_session_id, None)
        task, self._task = self._task, None
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass

    async def _loop(self) -> None:
        while True:
            await asyncio.sleep(POLL_INTERVAL_SECONDS)
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                logger.debug(
                    "ProviderAgent feed: group tick failed for %s",
                    self.parent_session_id, exc_info=True,
                )

    async def _read_batches(
        self, feeds: "List[WorkerFeed]"
    ) -> Dict[str, List[Dict[str, Any]]]:
        """Read new rows for every member. Returns ``{session_id: rows}``.

        Loops at most ``MAX_DRAIN_BATCHES`` times, re-querying ONLY the sessions
        whose previous slice came back full — so the common case (nothing much
        happened) is exactly ONE query for the whole parent, and the worst case
        matches what a single worker cost before.
        """
        from app.db import session_events as session_events_db

        pending: Dict[str, int] = {f.worker_session_id: f.cursor for f in feeds}
        buffers: Dict[str, List[Dict[str, Any]]] = {}
        try:
            for _ in range(MAX_DRAIN_BATCHES):
                if not pending:
                    break
                rows = await asyncio.to_thread(
                    session_events_db.get_events_multi,
                    dict(pending), DRAIN_BATCH * len(pending),
                )
                if not rows:
                    break
                grouped: Dict[str, List[Dict[str, Any]]] = {}
                for row in rows:
                    sid = row.get("session_id")
                    if sid in pending:
                        grouped.setdefault(sid, []).append(row)
                pending = {}
                for sid, session_rows in grouped.items():
                    # Per-worker slice cap — the same DRAIN_BATCH the per-worker
                    # poller applied. A full slice means more may be waiting, so
                    # that session (and only it) is re-queried next round.
                    session_rows = session_rows[:DRAIN_BATCH]
                    buffers.setdefault(sid, []).extend(session_rows)
                    if len(session_rows) == DRAIN_BATCH:
                        last = session_rows[-1].get("seq")
                        if last:
                            pending[sid] = last
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            logger.debug(
                "ProviderAgent feed: batched worker log read failed for %s",
                self.parent_session_id, exc_info=True,
            )
        return buffers

    async def tick(self) -> int:
        """One batched read + forward for every live member. Returns frames emitted."""
        feeds = [f for f in list(self._feeds.values()) if f.wants_rows]
        if not feeds:
            return 0
        buffers = await self._read_batches(feeds)
        if not buffers:
            return 0
        emitted = 0
        for feed in feeds:
            rows = buffers.get(feed.worker_session_id)
            if not rows:
                continue
            try:
                emitted += await feed.forward(rows)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                logger.debug(
                    "ProviderAgent feed: forward failed for %s",
                    feed.worker_session_id, exc_info=True,
                )
        return emitted


def join_feed_group(parent_session_id: str, feed: WorkerFeed) -> WorkerFeedGroup:
    """Add ``feed`` to its parent's group, creating the group on first use."""
    group = _GROUPS.get(parent_session_id)
    if group is None:
        group = WorkerFeedGroup(parent_session_id)
        _GROUPS[parent_session_id] = group
    group.add(feed)
    return group


def get_feed_group(parent_session_id: str) -> Optional[WorkerFeedGroup]:
    """The live group for a parent, or ``None`` (tests / diagnostics)."""
    return _GROUPS.get(parent_session_id)


def reset_feed_groups() -> None:
    """Drop every group without awaiting its loop (tests only)."""
    for group in list(_GROUPS.values()):
        task = group._task
        if task is not None and not task.done():
            task.cancel()
    _GROUPS.clear()


async def emit_shielded(coro: Any) -> None:
    """Await ``coro`` under a shield, swallowing our own cancellation.

    The teardown hazard this exists for: the Task tool handler runs as a
    tracked child of the parent's pooled adapter and is CANCELLED when that
    adapter closes (eviction / interrupt). The terminal frames are emitted from
    a ``finally``, i.e. while the task is already cancelling — and the reducer
    only speculatively settles running cards on ``turn.completed``, NOT on
    ``turn.aborted`` (``reducer.ts``), so a card whose
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
