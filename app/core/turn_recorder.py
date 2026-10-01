"""TurnRecorder — typed event-log appender tapped into the stream pump.

This module translates every pump event (``stream_to_websocket`` yields, plus
between-turns re-dispatches) into typed contract events
(`app.contract.events`) appended to the append-only ``session_events`` log
(`app.db.session_events`) under a fenced run lease (`app.db.runs`).

Invariants (program rules):
- NEVER imports ``sync_engine`` or any WebSocket-layer module.
- A recorder bug must never break streaming: every public method swallows and
  logs its own exceptions; all DB appends run off-loop via ``asyncio.to_thread``.
- Recording never blocks a turn: if the run lease cannot be claimed, events are
  appended UNFENCED (``run_id=None``) with a logged warning.

Delta batching: ``stream_delta`` text/thinking/tool-input payloads are
buffered in memory and flushed as ONE coalesced contract event per kind when
150ms have elapsed since the first buffered delta, OR the kind's buffer reaches
2048 chars, OR any non-delta event arrives (flushed BEFORE it so log order
always reflects pump order), OR on close. ``turn.usage`` is coalesced to at
most one append per 2s (latest pending value flushed before terminals/close).
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional

from app.contract import events as contract
from app.core import artifact_ingest, artifact_store
from app.core.config import settings
from app.core.session_title import derive_session_title
from app.db import runs as runs_db
from app.db import session_events as session_events_db

logger = logging.getLogger(__name__)

# Tunables — module-level so tests can monkeypatch deterministic values.
FLUSH_INTERVAL_SECONDS = 0.15  # flush deltas this long after the first buffered
FLUSH_MAX_BUFFER_CHARS = 2048  # ...or once a kind's buffer reaches this size
USAGE_COALESCE_SECONDS = 2.0   # at most one turn.usage append per window
# turn.provider_alive: the first of a turn is always recorded; a later one only
# after this long with nothing appended at all. A turn that streams content
# therefore records one, and a silent reasoning stream one per window.
PROVIDER_ALIVE_MIN_GAP_SECONDS = 15.0
RESULT_TRUNCATE_CHARS = 65536  # tool.result content / subagent result previews
RUN_LEASE_TTL_SECONDS = 60
RUN_LEASE_RENEW_SECONDS = 20

# Pump ``system`` subtypes recorded as system.compaction_boundary (passthrough).
_COMPACTION_SUBTYPES = ("compact_boundary", "microcompact_boundary")

# Envelope fields of EventBase — everything else on a model is its payload.
_ENVELOPE_FIELDS = {
    "id", "session_id", "seq", "run_id", "turn_id", "command_id",
    "created_at", "type",
}


def _notify_run_change(session_id: str, *, force: bool = False) -> None:
    """Run-lifecycle hook for the board spinner (streaming_status re-check).

    Lazy import + never raises: a status/broadcast bug must never break
    recording or the turn. ``force=True`` at CLAIM time: the previous turn's
    stale busy entry in ``_last_broadcast`` would otherwise suppress the ON
    frame the frontend already consumed via its done-frame cleanup.
    """
    try:
        from app.core import streaming_status

        streaming_status.notify(session_id, force=force)
    except Exception:
        logger.debug(
            "[TurnRecorder] streaming_status notify failed for %s",
            session_id, exc_info=True,
        )


def _truncate(value: Any, limit: int = RESULT_TRUNCATE_CHARS) -> Optional[str]:
    """Stringify + hard-truncate a result/preview payload. None stays None."""
    if value is None:
        return None
    text = value if isinstance(value, str) else str(value)
    if len(text) > limit:
        return text[:limit]
    return text


def _as_dict(value: Any) -> Dict[str, Any]:
    """Coerce a pump payload field into the dict the contract models require."""
    if isinstance(value, dict):
        return value
    if value is None:
        return {}
    return {"content": value}


# Pump event types whose ``screenshot`` data URI is content-addressed into the
# artifact store. ``tool_result`` renders on the main-lane tool card;
# ``subagent_tool_result`` on the subagent child card.
_SCREENSHOT_EVENT_TYPES = ("tool_result", "subagent_tool_result")


def _stamp_screenshot(event: Dict[str, Any]) -> Optional[tuple]:
    """Decode a pump event's ``screenshot`` data URI and stamp its content hash.

    On a screenshot-bearing event (see ``_SCREENSHOT_EVENT_TYPES``), decode +
    hash the bytes and append the digest to ``event["artifact_hashes"]`` so the
    translated contract event references it. Returns ``(data, mime, filename,
    digest)`` for the caller to store, or ``None`` when the event carries no
    well-formed screenshot. Pure/synchronous; never raises on malformed input
    (the parse simply misses).
    """
    if event.get("type") not in _SCREENSHOT_EVENT_TYPES:
        return None
    parsed = artifact_ingest.parse_screenshot_data_uri(event.get("screenshot"))
    if parsed is None:
        return None
    data, mime = parsed
    digest = artifact_ingest.content_hash(data)
    existing = event.get("artifact_hashes")
    hashes = list(existing) if isinstance(existing, list) else []
    if digest not in hashes:
        hashes.append(digest)
    event["artifact_hashes"] = hashes
    return data, mime, artifact_ingest.filename_for(digest, mime), digest


async def _ingest_screenshot_unfenced(
    session_id: str, event: Dict[str, Any]
) -> List[Dict[str, Any]]:
    """Unfenced-path screenshot ingest (between-turns re-dispatch).

    Mirrors ``TurnRecorder._maybe_ingest_screenshot`` for callers with no live
    recorder: stamp ``artifact_hashes`` onto the pump event, store the bytes
    (awaited — these paths are not latency-critical and have no close() drain to
    guarantee durability), and return the ``artifact.created`` row to append
    before the translated event. Returns ``[]`` when there is no screenshot;
    never raises.
    """
    try:
        stamped = _stamp_screenshot(event)
        if stamped is None:
            return []
        data, mime, filename, digest = stamped
        try:
            await asyncio.to_thread(
                artifact_store.store_bytes,
                data,
                mime=mime,
                filename=filename,
                created_by_session=session_id,
                runner_id=settings.effective_instance_id,
            )
        except Exception:
            logger.warning(
                "[TurnRecorder] unfenced artifact store failed for %s hash %s",
                session_id, digest, exc_info=True,
            )
        return [_build_event(
            contract.ArtifactCreated, session_id,
            hash=digest, mime=mime, filename=filename, size=len(data),
        )]
    except Exception:
        logger.warning(
            "[TurnRecorder] unfenced screenshot ingest failed for %s",
            session_id, exc_info=True,
        )
        return []


def _build_event(
    model_cls: type,
    session_id: str,
    *,
    turn_id: Optional[str] = None,
    command_id: Optional[str] = None,
    extra_payload: Optional[Dict[str, Any]] = None,
    exclude_none: bool = False,
    **fields: Any,
) -> Dict[str, Any]:
    """Construct a contract event model and reduce it to append_events shape.

    ``id``/``seq``/``created_at`` are server-assigned by the log layer, so the
    model is built with placeholders purely to validate the typed payload
    fields; only ``{"type", "payload", "turn_id", "command_id"}`` is appended.
    ``extra_payload`` carries payload fields the frozen contract models have
    no field for (e.g. turn.aborted ``message``) — payload is a free-form JSON
    column and the reducer ignores unknown keys. ``exclude_none`` drops unset
    optional fields from the payload instead of writing them as nulls, for
    events with an optional field that is usually absent (turn.usage's
    ``context_usage``).
    """
    model = model_cls(
        id="",
        session_id=session_id,
        seq=0,
        created_at=datetime.now(timezone.utc),
        turn_id=turn_id,
        command_id=command_id,
        **fields,
    )
    payload = model.model_dump(
        mode="json", exclude=_ENVELOPE_FIELDS, exclude_none=exclude_none
    )
    if extra_payload:
        payload.update(extra_payload)
    return {
        "type": model.type,
        "payload": payload,
        "turn_id": turn_id,
        "command_id": command_id,
    }


# Terminal statuses a claimed completion frame may carry. Anything else (an
# unknown string, or no status at all) settles the card as "completed" — a
# finished task must never be left running on the strip.
_TERMINAL_FRAME_STATES = ("completed", "failed", "cancelled")


def _registry_kind(session_id: str, task_id: Any) -> Dict[str, Any]:
    """Registry kind ("agent" | "shell") for a TaskUpdated frame, best-effort.

    The wire frames carry no kind of their own; without this stamp the FE
    reducer defaults every task to 'agent' and the tail line badges a shell as
    an agent. Returns {} when unknown (older logs / unregistered ids) so the
    additive field is simply absent.
    """
    if not task_id:
        return {}
    try:
        from app.core.task_registry import task_registry
        record = task_registry.get_task(session_id, str(task_id))
        kind = (record or {}).get("kind")
        return {"kind": kind} if kind else {}
    except Exception:  # noqa: BLE001 — a badge must never break recording
        return {}


def _registry_tool_use_id(session_id: str, task_id: Any) -> Optional[str]:
    """The launch ``tool_use_id`` the registry bound to ``task_id``, if any.

    Host Monitor shells learn their launching tool_use_id from the spawner, not
    from the frame that announces them, so the start/terminal rows have to read
    it back off the registry or the frontend card join has nothing to key on.
    Best-effort: unknown ids and a raising registry return ``None``.
    """
    if not task_id:
        return None
    try:
        from app.core.task_registry import task_registry
        return task_registry.get_task_tool_use_id(session_id, str(task_id))
    except Exception:  # noqa: BLE001 — a join key must never break recording
        return None


def _latch_start_chip(session_id: str, task_id: str) -> bool:
    """``True`` the FIRST time a start row is recorded for ``task_id``.

    Thin wrapper over ``task_registry.mark_chipped(..., "start")`` so a registry
    failure degrades to "record it" (a duplicate running row folds onto the same
    summary in the reducer; a dropped one would leave a running task invisible).
    """
    try:
        from app.core.task_registry import task_registry
        return task_registry.mark_chipped(session_id, task_id, "start")
    except Exception:  # noqa: BLE001
        return True


def _translate_shared(
    event: Dict[str, Any],
    session_id: str,
    *,
    turn_id: Optional[str] = None,
    command_id: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """Translation for the stateless pump event types.

    Shared by the in-turn recorder (fenced) and ``record_drained_event``
    (unfenced). Returns ``[]`` for anything unmapped (the not-recorded row).
    Delta batching, usage coalescing and terminals are stateful and handled by
    :class:`TurnRecorder` itself.
    """
    etype = event.get("type")
    kw: Dict[str, Any] = {"turn_id": turn_id, "command_id": command_id}

    if etype == "chunk":
        # Non-partial full text block (include_partial_messages disabled).
        return [_build_event(
            contract.AssistantTextDelta, session_id,
            text=event.get("content") or "", **kw,
        )]

    if etype == "tool_use":
        return [_build_event(
            contract.ToolUse, session_id,
            tool_use_id=event.get("id") or "",
            name=event.get("name") or "",
            input=event.get("input") if isinstance(event.get("input"), dict) else {},
            **kw,
        )]

    if etype == "tool_result":
        raw_hashes = event.get("artifact_hashes")
        artifact_hashes = (
            [h for h in raw_hashes if isinstance(h, str)]
            if isinstance(raw_hashes, list) else []
        )
        return [_build_event(
            contract.ToolResult, session_id,
            tool_use_id=event.get("tool_use_id") or "",
            is_error=bool(event.get("is_error")),
            content_preview=_truncate(event.get("output")),
            artifact_hashes=artifact_hashes,
            **kw,
        )]

    if etype == "subagent_start":
        return [_build_event(
            contract.SubagentStarted, session_id,
            task_id=event.get("tool_id") or event.get("agent_id") or "",
            agent_id=event.get("agent_id"),
            worker_session_id=event.get("worker_session_id") or None,
            agent_type=event.get("agent_type"),
            agent_name=event.get("agent_name") or None,
            description=event.get("description"),
            prompt_preview=_truncate(event.get("prompt")),
            launch_tool_use_id=event.get("launch_tool_use_id") or None,
            **kw,
        )]

    if etype in ("subagent_chunk", "subagent_tool_use", "subagent_tool_result"):
        # ``screenshot`` (a raw base64 data URI) is excluded: the ingest step
        # content-addresses it into the artifact store and stamps
        # ``artifact_hashes`` (kept in the payload) — the log never carries the
        # bytes themselves.
        payload = {
            k: v for k, v in event.items()
            if k not in ("type", "agent_id", "session_id", "screenshot")
        }
        return [_build_event(
            contract.SubagentProgress, session_id,
            task_id=event.get("agent_id") or "",
            kind=etype.removeprefix("subagent_"),
            payload=payload,
            **kw,
        )]

    if etype == "subagent_done":
        return [_build_event(
            contract.SubagentCompleted, session_id,
            task_id=event.get("tool_id") or event.get("agent_id") or "",
            status="failed" if event.get("is_error") else "completed",
            result_preview=_truncate(event.get("result")),
            usage=event.get("usage") if isinstance(event.get("usage"), dict) else None,
            **kw,
        )]

    if etype in ("task_progress", "task_started", "task_notification"):
        state = event.get("status") or {
            "task_started": "started",
            "task_progress": "running",
            "task_notification": "notified",
        }[etype]
        task_id = event.get("task_id") or ""
        tool_use_id = event.get("tool_use_id")
        if etype == "task_started":
            # ONE durable start row per task, whoever emits first (item 4). Both
            # the shell spawner (app/mcp/builtin_tools._spawn_background) and the
            # in-stream TaskStarted mirror produce this frame for the SAME host
            # Monitor shell; the shared ``mark_chipped(..., "start")`` latch —
            # the one the terminal surface already sets — makes the second a
            # no-op. Cleared on a genuine resume (task_registry.start_task) so a
            # revived agent paints a running chip again.
            if task_id and not _latch_start_chip(session_id, task_id):
                return []
            # Host Monitor shells carry no tool_use_id on their SubagentEvent
            # (fluid's event has no such field), so the terminal chip could not
            # join the launching card. The registry knows it — the spawner binds
            # it at start_task — so enrich here, exactly as the system/
            # task_updated branch below already does.
            if not tool_use_id:
                tool_use_id = _registry_tool_use_id(session_id, task_id)
        return [_build_event(
            contract.TaskUpdated, session_id,
            task_id=task_id,
            state=str(state),
            description=event.get("description"),
            summary=event.get("summary"),
            tool_use_id=tool_use_id,
            **_registry_kind(session_id, task_id),
            **kw,
        )]

    if etype == "system":
        subtype = event.get("subtype") or ""
        if subtype == "task_updated":
            # Background-task status chip (a TaskUpdatedMessage from the
            # stream). Recording it as a generic
            # system.note loses the terminal state: a replay then shows the
            # task stuck on its last in-turn "running" update forever.
            data = _as_dict(event.get("data"))
            task_id = data.get("task_id")
            if task_id:
                patch = data.get("patch")
                patch = patch if isinstance(patch, dict) else {}
                state = patch.get("status") or data.get("status") or "updated"
                # A TaskUpdatedMessage never carries tool_use_id; enrich
                # from the registry so terminal chips can settle the subagent card
                # (whose toolId is the launch tool_use_id). Function-local import
                # keeps this off the hot path / import graph.
                tool_use_id = data.get("tool_use_id")
                if not tool_use_id:
                    from app.core.task_registry import task_registry
                    tool_use_id = task_registry.get_task_tool_use_id(
                        session_id, str(task_id)
                    )
                return [_build_event(
                    contract.TaskUpdated, session_id,
                    task_id=str(task_id),
                    state=str(state),
                    description=data.get("description"),
                    summary=data.get("summary"),
                    tool_use_id=tool_use_id,
                    **_registry_kind(session_id, str(task_id)),
                    **kw,
                )]
        if subtype in _COMPACTION_SUBTYPES:
            # Contract model carries no fields; pass the raw frame through in
            # the payload.
            return [_build_event(
                contract.SystemCompactionBoundary, session_id,
                extra_payload={"subtype": subtype, "data": event.get("data")},
                **kw,
            )]
        return [_build_event(
            contract.SystemNote, session_id,
            subtype=subtype,
            data=_as_dict(event.get("data")),
            **kw,
        )]

    if etype == "hook_event":
        # Hook decisions render as system cards. Mirror the jsonl replay shape
        # (jsonl_parser.py hook_event mapping) so live + reload look identical.
        return [_build_event(
            contract.SystemNote, session_id,
            subtype="hook_event",
            data={
                "hook_event_name": event.get("hook_event_name"),
                "matcher": event.get("matcher"),
                "decision": event.get("decision"),
                "reason": event.get("reason"),
                "system_message": event.get("system_message"),
                "tool_name": event.get("tool_name"),
                "session_id": event.get("session_id"),
                "timestamp": event.get("timestamp"),
            },
            **kw,
        )]

    # Not recorded: stream_block_start/stop, stream_message_delta
    # (non-usage), rate_limit, plugin_message, heartbeat, api_error — and
    # anything unknown (forward compatibility).
    return []


class _Superseded(Exception):
    """Internal: unwinds ``close()`` past the append it must not attempt."""


class TurnRecorder:
    """Per-turn recorder: one instance per ``stream_to_websocket`` turn."""

    def __init__(
        self,
        session_id: str,
        command_id: Optional[str],
        run_id: Optional[str],
        fence_token: Optional[int],
        runner_id: str,
    ) -> None:
        self.session_id = session_id
        self.command_id = command_id
        self.run_id = run_id
        self.fence_token = fence_token
        self.runner_id = runner_id
        self.turn_id = str(uuid.uuid4())
        # True when THIS recorder owns the run lease (the ``maybe_create``
        # self-claim path): ``close()`` then releases the lease. The
        # ``create_for_run`` injection path sets this False — the SessionRuntime
        # owns the lease + renew loop and releases it itself, so ``close()`` must
        # NOT release out from under the runtime.
        self._owns_lease = True

        # Delta batching state. Insertion-ordered: kind -> list[str] chunks.
        self._delta_buffers: Dict[str, List[str]] = {}
        self._tool_input_tool_id: str = ""
        self._tool_input_tool_name: str = ""
        self._open_tool_use_id: str = ""  # from the last stream_block_start
        self._open_tool_use_name: str = ""
        self._flush_task: Optional[asyncio.Task] = None

        # turn.usage coalescing.
        self._last_usage_at: float = float("-inf")
        self._pending_usage: Optional[Dict[str, Any]] = None
        # Latest per-call context breakdown seen this turn. Held separately
        # from _pending_usage because coalescing drops whole usage records:
        # a snapshot must survive being coalesced over, so the flushed event
        # still describes the most recent model call.
        self._pending_context_usage: Optional[Dict[str, Any]] = None

        # turn.provider_alive bounding (PROVIDER_ALIVE_MIN_GAP_SECONDS).
        self._provider_alive_recorded = False
        self._last_append_at: float = float("-inf")

        self._terminal_recorded = False
        self._closed = False
        # Fence state. The first FenceViolation from append_events means the
        # run lease moved to another runner: every later append from this
        # recorder would be rejected too, so the recorder goes silent (drops
        # and counts) instead of logging one WARNING per batch for the rest
        # of the turn. ``on_superseded`` (set by the owning SessionRuntime)
        # fires once so the owner can stop the pump it is still driving.
        self._superseded = False
        self._superseded_dropped = 0
        self.on_superseded: Optional[Callable[[], None]] = None
        self._recording_failed = False
        self._recording_failed_dropped = 0
        self.on_recording_failed: Optional[Callable[[], None]] = None
        self._renew_task: Optional[asyncio.Task] = None
        # Serializes buffer mutation + appends (on_event vs. the flush timer)
        # so log order always reflects pump order.
        self._lock = asyncio.Lock()
        # In-flight fire-and-forget screenshot-artifact stores. Held so the
        # loop keeps a strong ref (asyncio only weak-refs tasks) and ``close``
        # can drain them, making the bytes durable before teardown.
        self._artifact_tasks: set[asyncio.Task] = set()

    # ------------------------------------------------------------------
    # lifecycle
    # ------------------------------------------------------------------

    @classmethod
    async def maybe_create(
        cls, session_id: str, command_id: Optional[str] = None
    ) -> Optional["TurnRecorder"]:
        """Return a recorder for this turn — every session records on the V2 path.

        Never raises and never blocks the turn: any failure (including a lost
        run-lease claim) degrades to ``None`` / unfenced recording.
        """
        # Seed the event log from pre-event-log history BEFORE this turn's first
        # append, so turn.started never lands on an unseeded log. Lazy import:
        # history_backfill imports this module's builders.
        from app.core.history_backfill import ensure_backfilled
        await ensure_backfilled(session_id)

        runner_id = settings.effective_instance_id
        run_id: Optional[str] = None
        fence_token: Optional[int] = None
        try:
            run = await asyncio.to_thread(
                runs_db.claim_run, session_id, runner_id, RUN_LEASE_TTL_SECONDS
            )
        except Exception:
            run = None
            logger.warning(
                "[TurnRecorder] claim_run raised for %s — recording UNFENCED",
                session_id, exc_info=True,
            )
        if run:
            run_id = run["id"]
            fence_token = run["fence_token"]
            # Run went active — force the durable busy view to broadcast ON
            # even if a stale previous-turn busy entry is still last-broadcast.
            _notify_run_change(session_id, force=True)
        else:
            logger.warning(
                "[TurnRecorder] could not claim run lease for %s "
                "(another active run?) — recording UNFENCED", session_id,
            )

        recorder = cls(session_id, command_id, run_id, fence_token, runner_id)
        if run_id is not None:
            recorder._renew_task = asyncio.create_task(recorder._renew_loop())
        return recorder

    @classmethod
    async def create_for_run(
        cls,
        session_id: str,
        *,
        run_id: str,
        fence_token: int,
        turn_id: str,
        command_id: Optional[str] = None,
        runner_id: Optional[str] = None,
    ) -> "TurnRecorder":
        """Build a recorder bound to a run lease the CALLER already claimed
        (the injection path).

        :class:`app.core.session_runtime.SessionRuntime` claims the fenced run
        lease, starts the ``turns`` row, and owns the renew loop, then hands the
        recorder the injected ``(run_id, fence_token, turn_id)`` instead of
        letting it self-claim (the :meth:`maybe_create` path). Consequences vs.
        ``maybe_create``:

        * the recorder does NOT claim a run and does NOT start its own renew loop
          (``_renew_task`` stays ``None`` — the runtime heartbeats the lease);
        * ``close()`` does NOT release the run (``_owns_lease`` is False — the
          runtime releases it in its ``finally``);
        * the recorder's ``turn_id`` is the injected ``turns`` row id, so every
          appended event ties to the durable turn machine.

        All other recording internals (delta batching, usage coalescing,
        terminal translation) are identical. Like ``maybe_create`` this seeds the
        history backfill before the turn's first append and never raises
        for the backfill.
        """
        from app.core.history_backfill import ensure_backfilled
        await ensure_backfilled(session_id)

        recorder = cls(
            session_id,
            command_id,
            run_id,
            fence_token,
            runner_id or settings.effective_instance_id,
        )
        recorder.turn_id = turn_id
        recorder._owns_lease = False
        return recorder

    async def start(
        self,
        user_message: str,
        profile_id: str | None = None,
        record_user: bool = True,
        source: str | None = None,
    ) -> None:
        """Append ``turn.started`` then ``message.user``.

        ``record_user=False`` appends only ``turn.started`` — a resumed turn
        re-runs from the head node and adds no new user message. ``source``
        is what started the turn, in fluid's vocabulary; None leaves it off.
        """
        try:
            started: Dict[str, Any] = {"profile_id": profile_id}
            if source is not None:
                started["source"] = source
            events = [self._event(contract.TurnStarted, **started)]
            if record_user:
                events.append(self._event(
                    contract.MessageUser,
                    content=user_message or "",
                    profile_id=profile_id,
                ))
            await self._append(events)
        except Exception:
            logger.warning(
                "[TurnRecorder] start failed for %s", self.session_id,
                exc_info=True,
            )

    async def record_steered_user_message(
        self,
        content: str,
        *,
        profile_id: str | None = None,
        command_id: str | None = None,
    ) -> bool:
        """Append a ``message.user`` for a message steered into the LIVE turn.

        A queued prompt handed to the running engine mid-stream belongs to THIS
        turn from the moment fluid receipts it, and it belongs at the position
        the model saw it — right after the tool result that carried it. So it is
        appended here, mid-stream, rather than by the ``start`` of a turn the
        row will now never have.

        ``command_id`` overrides the recorder's own (the queued row's handle,
        not the live turn's) so the frontend collapses its pending bubble onto
        this message instead of rendering a second copy. Buffered deltas drain
        first, exactly as any non-delta pump event would, so log order still
        mirrors stream order. These messages have no standalone transcript
        checkpoint, so explicitly disable their independent rewind capability.
        Returns True when the append was made.
        """
        if self._closed or self._superseded or self._recording_failed:
            return False
        try:
            async with self._lock:
                events = self._drain_buffered_events()
                events.append(_build_event(
                    contract.MessageUser, self.session_id,
                    turn_id=self.turn_id,
                    command_id=command_id or self.command_id,
                    content=content or "",
                    profile_id=profile_id,
                    checkpoint_backed=False,
                ))
                await self._append(events)
            return not (self._recording_failed or self._superseded)
        except Exception:
            logger.warning(
                "[TurnRecorder] record_steered_user_message failed for %s",
                self.session_id, exc_info=True,
            )
            return False

    async def record_task_terminal(self, frames: List[Dict[str, Any]]) -> int:
        """Append one ``task.updated`` per claimed background-task completion.

        A background task that finishes BETWEEN turns is delivered by the wake
        turn the scheduler enqueues for it (``payload.pending_frames``). That
        wake IS the delivery point for those frames, so it is also where the
        event log — and with it the frontend card — has to learn the task is
        done: nothing in the wake turn's own stream repeats a completion that
        happened before it started, and the frontend does not force-settle
        background shells at turn end. Without this append the card runs
        forever, its elapsed timer counting past the real completion.

        Mirrors the in-stream ``task_updated`` translation: registry ``kind`` so
        the badge survives, registry ``tool_use_id`` so a subagent card can
        settle. Best-effort like every other append — never raises. Returns the
        number of events appended.
        """
        if self._closed or self._superseded or self._recording_failed or not frames:
            return 0
        try:
            events: List[Dict[str, Any]] = []
            for frame in frames:
                if not isinstance(frame, dict):
                    continue
                task_id = frame.get("task_id")
                if not task_id:
                    continue
                task_id = str(task_id)
                status = frame.get("status")
                state = (
                    str(status) if status in _TERMINAL_FRAME_STATES else "completed"
                )
                kind = _registry_kind(self.session_id, task_id)
                if not kind and frame.get("kind"):
                    kind = {"kind": str(frame["kind"])}
                tool_use_id = frame.get("tool_use_id")
                if not tool_use_id:
                    from app.core.task_registry import task_registry
                    tool_use_id = task_registry.get_task_tool_use_id(
                        self.session_id, task_id
                    )
                events.append(self._event(
                    contract.TaskUpdated,
                    task_id=task_id,
                    state=state,
                    description=frame.get("description"),
                    summary=frame.get("summary"),
                    tool_use_id=tool_use_id,
                    **kind,
                ))
            if not events:
                return 0
            async with self._lock:
                await self._append(self._drain_buffered_events() + events)
            return 0 if self._recording_failed or self._superseded else len(events)
        except Exception:
            logger.warning(
                "[TurnRecorder] record_task_terminal failed for %s (%d frame(s))",
                self.session_id, len(frames), exc_info=True,
            )
            return 0

    async def on_event(self, event: Dict[str, Any]) -> None:
        """Translate one yielded pump event. Never raises."""
        try:
            if not isinstance(event, dict):
                return
            if self._superseded:
                self._superseded_dropped += 1
                return
            async with self._lock:
                await self._on_event_locked(event)
        except Exception:
            logger.warning(
                "[TurnRecorder] on_event failed for %s (event type=%r)",
                self.session_id, event.get("type") if isinstance(event, dict) else None,
                exc_info=True,
            )

    async def close(self, outcome: Optional[str] = None) -> None:
        """Flush, append crash-path terminal if needed, release lease.

        Idempotent — a second close appends nothing and releases nothing.
        """
        if self._closed:
            return
        self._closed = True
        if self._superseded:
            # Nothing here can be appended (the fence rejects it). Account for
            # what was still buffered, then say so ONCE.
            self._superseded_dropped += len(self._drain_buffered_events())
            if self._pending_usage is not None:
                self._superseded_dropped += 1
                self._pending_usage = None
            logger.warning(
                "[TurnRecorder] fence violation for %s run %s — superseded "
                "runner; %d event(s) dropped for the rest of the turn",
                self.session_id, self.run_id, self._superseded_dropped,
            )
        try:
            async with self._lock:
                if self._superseded:
                    raise _Superseded()
                events = self._drain_buffered_events()
                if self._pending_usage is not None:
                    events.append(self._usage_event(self._pending_usage))
                    self._pending_usage = None
                if not self._terminal_recorded:
                    # Crash path: the pump ended without done/interrupted/error.
                    events.append(
                        self._event(
                            contract.TurnAborted, reason="error",
                            extra_payload=(
                                {"outcome": outcome} if outcome else None
                            ),
                        )
                    )
                    self._terminal_recorded = True
                await self._append(events)
        except _Superseded:
            pass
        except Exception:
            logger.warning(
                "[TurnRecorder] close failed for %s", self.session_id,
                exc_info=True,
            )
        finally:
            # Drain in-flight screenshot stores so the bytes are durable before
            # teardown. Errors are already swallowed inside each task.
            if self._artifact_tasks:
                await asyncio.gather(
                    *list(self._artifact_tasks), return_exceptions=True
                )
            self._cancel_task(self._flush_task)
            self._flush_task = None
            self._cancel_task(self._renew_task)
            self._renew_task = None
            if (
                self._owns_lease
                and self.run_id is not None
                and self.fence_token is not None
            ):
                try:
                    await asyncio.to_thread(
                        runs_db.release_run, self.run_id, self.fence_token
                    )
                except Exception:
                    logger.warning(
                        "[TurnRecorder] release_run failed for %s run %s",
                        self.session_id, self.run_id, exc_info=True,
                    )
                # Run left active (released — or expired/transferred if the
                # release lost). Either way, re-check + broadcast the diff.
                _notify_run_change(self.session_id)

    # ------------------------------------------------------------------
    # translation core (lock held)
    # ------------------------------------------------------------------

    async def _on_event_locked(self, event: Dict[str, Any]) -> None:
        if self._recording_failed:
            self._count_recording_drops(1)
            return
        etype = event.get("type")

        if etype == "stream_delta":
            if self._buffer_delta(event):
                # Size threshold (≥ FLUSH_MAX_BUFFER_CHARS) — flush now.
                await self._append(self._drain_buffered_events())
            return

        # Any non-delta pump event flushes buffered deltas FIRST so the log
        # order always reflects pump order — including not-recorded
        # events like stream_block_start (a block boundary).
        events = self._drain_buffered_events()

        if etype == "stream_block_start":
            # Not recorded, but tracks the open tool_use block so buffered
            # tool-input deltas can carry their tool_use_id.
            content_block = event.get("content_block") or {}
            if content_block.get("type") == "tool_use" and content_block.get("id"):
                self._open_tool_use_id = content_block["id"]
                self._open_tool_use_name = str(content_block.get("name") or "")
        elif etype == "provider_alive":
            # The provider accepted a model request and its stream is open.
            # Recorded for the Task worker's first-frame watchdog, which reads
            # only this log: it proves the request started, not that
            # the model is doing useful work. Bounded, since fluid sends one
            # per model call even when content follows at once.
            silent = not events and (
                time.monotonic() - self._last_append_at
                >= PROVIDER_ALIVE_MIN_GAP_SECONDS
            )
            if not self._provider_alive_recorded or silent:
                self._provider_alive_recorded = True
                signal = event.get("signal")
                events.append(self._event(
                    contract.TurnProviderAlive,
                    signal=str(signal) if signal else None,
                ))
        elif etype == "turn_usage":
            events.extend(self._coalesce_usage(_as_dict(event.get("usage"))))
        elif etype == "stream_start":
            usage = (event.get("message") or {}).get("usage")
            if usage:
                events.extend(self._coalesce_usage(_as_dict(usage)))
        elif etype == "stream_message_delta":
            usage = event.get("usage")
            if usage:
                events.extend(self._coalesce_usage(_as_dict(usage)))
        elif etype == "done":
            events.extend(self._flush_pending_usage())
            metadata = event.get("metadata") or {}
            usage_summary = {
                k: metadata[k]
                for k in (
                    "tokens_in", "tokens_out", "cache_creation_tokens",
                    "cache_read_tokens", "total_cost_usd", "duration_ms",
                    "num_turns", "usage_estimated",
                )
                if k in metadata
            }
            # usage is an open dict in the contract, so per-model usage rides here to stay durable in session_events under postgres_only.
            if metadata.get("model_usage"):
                usage_summary["model_usage"] = metadata["model_usage"]
            # Carry the pump's API error status into the terminal payload — a
            # usage-limit 429 turn
            # otherwise settles as a normal completion with api_error_status
            # living only in in-memory pump metadata, leaving the scheduler's
            # failover policy nothing durable to key on. ``extra_payload``
            # because the frozen contract model has no field for it (same
            # mechanism as turn.aborted ``message``). ``ResultMessage`` exposes
            # only ``api_error_status`` — no message field is ever populated.
            api_error: Optional[Dict[str, Any]] = None
            if metadata.get("api_error_status") is not None:
                api_error = {"api_error_status": metadata["api_error_status"]}
            # A provider can report a failed ResultMessage without HTTP status.
            # Preserve these gates for post-settlement consumers as well.
            for key in ("is_error", "errors"):
                if metadata.get(key):
                    api_error = {**(api_error or {}), key: metadata[key]}
            events.append(self._event(
                contract.TurnCompleted,
                stop_reason=metadata.get("stop_reason"),
                terminal_reason=metadata.get("terminal_reason"),
                usage=usage_summary,
                extra_payload=api_error,
            ))
            self._terminal_recorded = True
        elif etype == "interrupted":
            events.extend(self._flush_pending_usage())
            interrupt_meta = event.get("metadata") or {}
            events.append(self._event(
                contract.TurnAborted,
                reason="interrupt",
                terminal_reason=interrupt_meta.get("terminal_reason"),
            ))
            self._terminal_recorded = True
        elif etype == "error":
            events.extend(self._flush_pending_usage())
            events.append(self._event(
                contract.TurnAborted, reason="error",
                extra_payload={"message": event.get("message")},
            ))
            self._terminal_recorded = True
        else:
            # Content-address a tool_result screenshot BEFORE translation so
            # the ToolResult carries its artifact_hashes; append artifact.created
            # first (no-op for every non-screenshot event / flag OFF).
            events.extend(self._maybe_ingest_screenshot(event))
            events.extend(_translate_shared(
                event, self.session_id,
                turn_id=self.turn_id, command_id=self.command_id,
            ))

        await self._append(events)

    # ------------------------------------------------------------------
    # screenshot artifact ingest
    # ------------------------------------------------------------------

    def _maybe_ingest_screenshot(
        self, event: Dict[str, Any]
    ) -> List[Dict[str, Any]]:
        """Content-address a screenshot result (fire-and-forget).

        On a ``tool_result`` / ``subagent_tool_result`` pump event carrying a
        decoded screenshot data URI (browser / computer-use image results —
        surfaced at the ``query_engine`` pump sites), decode + hash the bytes
        synchronously, stamp ``artifact_hashes`` onto the pump event (so
        ``_translate_shared`` carries the reference into the recorded
        ``tool.result`` / ``subagent.progress``), schedule a fire-and-forget
        disk/DB store, and return an ``artifact.created`` event to append in
        the same batch.

        Returns ``[]`` (a no-op) for every other event, or when the screenshot
        is not a well-formed base64 data URI. Never raises: a decode/scheduling
        failure degrades to no ingest, never breaking the pump (the recorder
        isolation invariant).
        """
        try:
            # Stamp the content address onto the pump event so the translated
            # ToolResult / SubagentProgress append references it. Idempotent +
            # dedup-friendly.
            stamped = _stamp_screenshot(event)
            if stamped is None:
                return []
            data, mime, filename, digest = stamped
            self._spawn_artifact_store(data, mime, filename, digest)
            return [self._event(
                contract.ArtifactCreated,
                hash=digest, mime=mime, filename=filename, size=len(data),
            )]
        except Exception:
            logger.warning(
                "[TurnRecorder] screenshot ingest failed for %s",
                self.session_id, exc_info=True,
            )
            return []

    def _spawn_artifact_store(
        self, data: bytes, mime: str, filename: str, digest: str
    ) -> None:
        """Fire-and-forget the blocking disk/DB store off the pump loop."""
        async def _run() -> None:
            try:
                await asyncio.to_thread(
                    artifact_store.store_bytes,
                    data,
                    mime=mime,
                    filename=filename,
                    created_by_session=self.session_id,
                    runner_id=self.runner_id,
                )
            except Exception:
                logger.warning(
                    "[TurnRecorder] artifact store failed for %s hash %s",
                    self.session_id, digest, exc_info=True,
                )

        task = asyncio.create_task(_run())
        self._artifact_tasks.add(task)
        task.add_done_callback(self._artifact_tasks.discard)

    # ------------------------------------------------------------------
    # delta batching
    # ------------------------------------------------------------------

    def _buffer_delta(self, event: Dict[str, Any]) -> bool:
        """Buffer one stream_delta. Returns True when a size-flush is due."""
        delta_type = event.get("delta_type")
        content = event.get("content") or ""
        if delta_type not in ("text", "thinking", "tool_input") or not content:
            return False
        if delta_type == "thinking" and event.get("subtype"):
            # The provider's summary and its reasoning text are different
            # voices: buffered under their own key so they never coalesce into
            # one delta, and emitted with the subtype the UI labels on.
            delta_type = f"thinking/{event['subtype']}"
        if delta_type == "tool_input" and "tool_input" not in self._delta_buffers:
            # Stamp the open block's id at first buffer; a new tool block's
            # stream_block_start flushes this buffer before re-stamping.
            self._tool_input_tool_id = self._open_tool_use_id
            # The name rides the delta on the native leg and comes from the
            # open block on the delegated one; either way the first fragment
            # is what a card needs to label itself.
            self._tool_input_tool_name = (
                str(event.get("tool_name") or "") or self._open_tool_use_name
            )
        buf = self._delta_buffers.setdefault(delta_type, [])
        buf.append(content)
        if sum(len(c) for c in buf) >= FLUSH_MAX_BUFFER_CHARS:
            # Size threshold — the (locked) caller flushes immediately.
            return True
        if self._flush_task is None or self._flush_task.done():
            self._flush_task = asyncio.create_task(self._delayed_flush())
        return False

    async def _delayed_flush(self) -> None:
        """Flush buffered deltas ~FLUSH_INTERVAL_SECONDS after first buffered."""
        try:
            await asyncio.sleep(FLUSH_INTERVAL_SECONDS)
            async with self._lock:
                await self._append(self._drain_buffered_events())
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.warning(
                "[TurnRecorder] delayed flush failed for %s", self.session_id,
                exc_info=True,
            )

    def _drain_buffered_events(self) -> List[Dict[str, Any]]:
        """Coalesce each kind's buffer into ONE contract event (pump order)."""
        events: List[Dict[str, Any]] = []
        for kind, chunks in self._delta_buffers.items():
            text = "".join(chunks)
            if not text:
                continue
            if kind == "text":
                events.append(self._event(contract.AssistantTextDelta, text=text))
            elif kind.startswith("thinking"):
                subtype = kind.partition("/")[2] or None
                events.append(
                    self._event(
                        contract.AssistantThinkingDelta, text=text,
                        **({"subtype": subtype} if subtype else {}),
                    )
                )
            elif kind == "tool_input":
                events.append(self._event(
                    contract.ToolInputDelta,
                    tool_use_id=self._tool_input_tool_id,
                    partial_json=text,
                    name=self._tool_input_tool_name or None,
                ))
        self._delta_buffers.clear()
        self._cancel_task(self._flush_task)
        self._flush_task = None
        return events

    # ------------------------------------------------------------------
    # usage coalescing
    # ------------------------------------------------------------------

    def _usage_event(self, usage: Dict[str, Any]) -> Dict[str, Any]:
        """A turn.usage event carrying the newest context breakdown available.

        Coalescing discards whole usage records, so the snapshot is tracked
        apart from the record it arrived on and re-attached here: a breakdown
        is never lost just because a later usage record won the window.
        """
        return self._event(
            contract.TurnUsage,
            usage=usage,
            context_usage=self._pending_context_usage,
            exclude_none=True,
        )

    def _coalesce_usage(self, usage: Dict[str, Any]) -> List[Dict[str, Any]]:
        # A snapshot is the most recent truth about the context regardless of
        # which branch below claims this record, so latch it first.
        context_usage = usage.get("context_usage")
        if context_usage:
            self._pending_context_usage = context_usage
        # Native fluid emits one terminal snapshot per logical model call with
        # a stable call index. Those records are accounting/audit evidence, not
        # a rapidly-changing gauge, so never coalesce one call over another.
        if usage.get("model_call_index") is not None:
            self._pending_usage = None
            self._last_usage_at = time.monotonic()
            return [self._usage_event(usage)]
        now = time.monotonic()
        if now - self._last_usage_at >= USAGE_COALESCE_SECONDS:
            self._last_usage_at = now
            self._pending_usage = None
            return [self._usage_event(usage)]
        # Within the window — hold the LATEST value; flushed before terminals
        # and on close so the final gauge value is never lost.
        self._pending_usage = usage
        return []

    def _flush_pending_usage(self) -> List[Dict[str, Any]]:
        if self._pending_usage is None:
            return []
        usage = self._pending_usage
        self._pending_usage = None
        self._last_usage_at = time.monotonic()
        return [self._usage_event(usage)]

    # ------------------------------------------------------------------
    # plumbing
    # ------------------------------------------------------------------

    def _event(self, model_cls: type, **fields: Any) -> Dict[str, Any]:
        return _build_event(
            model_cls, self.session_id,
            turn_id=self.turn_id, command_id=self.command_id, **fields,
        )

    async def _append(self, events: List[Dict[str, Any]]) -> None:
        """Append a batch under the fence (or unfenced). Never raises."""
        if not events:
            return
        if self._recording_failed:
            self._count_recording_drops(len(events))
            return
        if self._superseded:
            self._superseded_dropped += len(events)
            return
        try:
            if self.run_id is not None and self.fence_token is not None:
                await asyncio.to_thread(
                    session_events_db.append_events,
                    self.session_id, events,
                    run_id=self.run_id, fence_token=self.fence_token,
                )
            else:
                await asyncio.to_thread(
                    session_events_db.append_events, self.session_id, events
                )
            self._last_append_at = time.monotonic()
        except session_events_db.FenceViolation:
            self._mark_superseded(len(events))
        except Exception:
            # The commit outcome is unknown. Never retry or retain this batch.
            self._recording_failed = True
            self._cancel_task(self._renew_task)
            self._renew_task = None
            self._count_recording_drops(len(events) + len(self._delta_buffers)
                                        + int(self._pending_usage is not None))
            self._delta_buffers.clear()
            self._pending_usage = None
            self._pending_context_usage = None
            self._cancel_task(self._flush_task)
            self._flush_task = None
            logger.warning(
                "[TurnRecorder] recording failed for %s run %s turn %s "
                "(%d event(s) dropped; commit outcome unknown)",
                self.session_id, self.run_id, self.turn_id, len(events), exc_info=True,
            )
            if self.on_recording_failed is not None:
                try:
                    self.on_recording_failed()
                except Exception:
                    logger.warning("[TurnRecorder] on_recording_failed hook failed for %s",
                                   self.session_id, exc_info=True)

    def _count_recording_drops(self, count: int) -> None:
        # Saturation bounds diagnostics even for a long-lived direct recorder.
        self._recording_failed_dropped = min(2**31 - 1, self._recording_failed_dropped + count)

    @property
    def recording_failed(self) -> bool:
        return self._recording_failed

    @property
    def recording_failed_dropped(self) -> int:
        return self._recording_failed_dropped

    def _mark_superseded(self, dropped: int) -> None:
        """First fence violation: go silent, stop the heartbeat, tell the owner.

        Logging is deferred to ``close()`` so the whole episode produces one
        WARNING carrying the total drop count. The heartbeat is cancelled
        because the lease is no longer ours to renew. ``on_superseded`` runs
        exactly once, synchronously on the loop; it must not raise.
        """
        self._superseded_dropped += dropped
        if self._superseded:
            return
        self._superseded = True
        self._cancel_task(self._renew_task)
        self._renew_task = None
        callback = self.on_superseded
        if callback is None:
            return
        try:
            callback()
        except Exception:  # noqa: BLE001 — owner hook is best-effort
            logger.warning(
                "[TurnRecorder] on_superseded hook failed for %s run %s",
                self.session_id, self.run_id, exc_info=True,
            )

    @property
    def superseded(self) -> bool:
        return self._superseded

    @property
    def superseded_dropped(self) -> int:
        return self._superseded_dropped

    async def _renew_loop(self) -> None:
        """Heartbeat the run lease every RUN_LEASE_RENEW_SECONDS."""
        try:
            while True:
                await asyncio.sleep(RUN_LEASE_RENEW_SECONDS)
                try:
                    ok = await asyncio.to_thread(
                        runs_db.renew_run,
                        self.run_id, self.runner_id, self.fence_token,
                        RUN_LEASE_TTL_SECONDS,
                    )
                except Exception:
                    logger.warning(
                        "[TurnRecorder] renew_run raised for %s run %s",
                        self.session_id, self.run_id, exc_info=True,
                    )
                    continue
                if not ok:
                    logger.warning(
                        "[TurnRecorder] lease renewal lost for %s run %s — "
                        "stopping heartbeat (appends will be fence-rejected)",
                        self.session_id, self.run_id,
                    )
                    return
        except asyncio.CancelledError:
            raise

    @staticmethod
    def _cancel_task(task: Optional[asyncio.Task]) -> None:
        if task is not None and not task.done() and task is not asyncio.current_task():
            task.cancel()


# ----------------------------------------------------------------------
# Module-level taps (between turns / stop / park) — all unfenced appends.
# ----------------------------------------------------------------------

async def _append_unfenced(session_id: str, events: List[Dict[str, Any]]) -> None:
    if not events:
        return
    await asyncio.to_thread(session_events_db.append_events, session_id, events)


async def record_drained_event(session_id: str, event: Dict[str, Any]) -> None:
    """Record a between-turns event: subagent_*/task_*/system only.

    Unfenced append (run_id=None, turn_id=None) — callers run between turns with
    no owning run. Never raises.
    """
    try:
        if not isinstance(event, dict):
            return
        etype = event.get("type") or ""
        if not (
            etype.startswith("subagent_")
            or etype.startswith("task_")
            or etype == "system"
        ):
            # Deliberately narrow (other drain callers depend on it). Log the
            # dropped type so a silently-unrendered drain event is diagnosable.
            logger.debug(
                "[TurnRecorder] record_drained_event dropped type=%r for %s",
                etype, session_id,
            )
            return
        # Screenshot ingest BEFORE translation (mirrors the fenced record()
        # order): a drained subagent_tool_result's image is content-addressed
        # and its hash carried in the payload — translation strips the raw URI.
        rows = await _ingest_screenshot_unfenced(session_id, event)
        rows.extend(_translate_shared(event, session_id))
        await _append_unfenced(session_id, rows)
    except Exception:
        logger.warning(
            "[TurnRecorder] record_drained_event failed for %s", session_id,
            exc_info=True,
        )


# ----------------------------------------------------------------------
# Voice exchanges — synthetic turns, no ``turns`` row.
# ----------------------------------------------------------------------

# Placeholder title stamped by ``POST /api/v1/voice/sessions``; the first
# exchange replaces it with something derived from what the user actually said.
VOICE_PLACEHOLDER_TITLE = "Voice session"

# Same shape the chat WS handler derives titles with (100 chars, word-boundary
# ellipsis) so a voice session's board entry reads like any other.
_TITLE_MAX_CHARS = 100


def _derive_title(text: str) -> str:
    return derive_session_title(text, max_chars=_TITLE_MAX_CHARS)


#: Metadata stamped on the ``session_messages`` row for a backend-authored note.
VOICE_SYSTEM_NOTE_SOURCE = "voice_system_note"


def _voice_exchange_side_effects(
    session_id: str,
    user_text: Optional[str],
    assistant_text: Optional[str],
    system_text: Optional[str] = None,
) -> Optional[str]:
    """Search rows + board bookkeeping for one voice exchange (sync).

    Mirrors what a normal turn writes ALONGSIDE the event log:
    ``session_messages`` rows (FTS/search parity) and the session counters. On
    the first exchange of a still-unnamed voice session the title is backfilled
    from the user's first utterance; the derived title is returned so the caller
    can log it (``None`` when nothing was renamed).

    ``system_text`` is a note the BRIDGE injected (``[call started]``,
    ``[agent update]``, ``[approval needed]``). It persists as a ``system`` row,
    not a ``user`` one: it is not the user's words, it must not name the
    session, and the voice context seed — which reads user/assistant rows — must
    not replay it back into the next call as if the user had said it.
    """
    from app.db import database

    if system_text:
        database.add_session_message(
            session_id, "system", system_text,
            metadata={"source": VOICE_SYSTEM_NOTE_SOURCE},
        )
    if user_text:
        database.add_session_message(session_id, "user", user_text)
    if assistant_text:
        database.add_session_message(session_id, "assistant", assistant_text)

    new_title: Optional[str] = None
    if user_text and not _is_voice_system_note(user_text):
        row = database.get_session(session_id)
        current = (row or {}).get("title") or ""
        if not current.strip() or current.strip() == VOICE_PLACEHOLDER_TITLE:
            new_title = _derive_title(user_text) or None

    database.update_session(
        session_id=session_id, turn_increment=1, title=new_title,
    )
    return new_title


def _is_voice_system_note(text: str) -> bool:
    """Whether ``text`` is bridge boilerplate rather than something spoken.

    Belt-and-braces behind the bridge's own classification: the greeting
    preamble reaching this function as ``user_text`` would title the session
    "[call started] The user just joined the call…".
    """
    try:
        from app.core.voice_bridge import is_backend_note

        return is_backend_note(text)
    except Exception:  # noqa: BLE001 — a title guard never blocks a write
        return False


async def record_voice_exchange(
    session_id: str,
    user_text: Optional[str],
    assistant_text: Optional[str],
    *,
    model: Optional[str] = None,
    usage: Optional[Dict[str, Any]] = None,
    system_text: Optional[str] = None,
) -> Optional[str]:
    """Persist ONE completed voice exchange as a synthetic turn.

    Appends ``turn.started`` / ``message.user`` / ``message.assistant`` /
    ``turn.completed`` in a SINGLE unfenced batch (so the four rows get
    contiguous seqs) under a ``voice-<uuid>`` turn id with NO ``turns``-table
    row — exactly the CLI-wake synthetic-turn shape, which the FE reducer
    already renders as ordinary history and the turn scheduler ignores.

    ``user_text=None`` is legitimate — a model-initiated response, or an input
    transcription that never arrived — and simply omits the ``message.user``
    event. ``system_text`` is a note the bridge injected into the call, kept
    apart from the user's words throughout (see
    :func:`_voice_exchange_side_effects`). Returns the synthetic turn id, or
    ``None`` when nothing was written. Never raises: a persistence bug must not
    kill the call.
    """
    try:
        user_text = (user_text or "").strip() or None
        assistant_text = (assistant_text or "").strip() or None
        system_text = (system_text or "").strip() or None
        if not user_text and not assistant_text and not system_text:
            return None

        turn_id = f"voice-{uuid.uuid4()}"
        rows: List[Dict[str, Any]] = [
            _build_event(contract.TurnStarted, session_id, turn_id=turn_id),
        ]
        if system_text:
            # Tagged, and first: it is what PROMPTED the reply that follows.
            rows.append(_build_event(
                contract.MessageUser, session_id, turn_id=turn_id,
                content=system_text, kind="text",
                extra_payload={"source": VOICE_SYSTEM_NOTE_SOURCE},
            ))
        if user_text:
            rows.append(_build_event(
                contract.MessageUser, session_id, turn_id=turn_id,
                content=user_text, kind="text",
                extra_payload={"source": "voice"},
            ))
        rows.append(_build_event(
            contract.MessageAssistant, session_id, turn_id=turn_id,
            content=assistant_text or "", kind="text", model=model,
        ))
        rows.append(_build_event(
            contract.TurnCompleted, session_id, turn_id=turn_id,
            stop_reason="end_turn", usage=usage or {},
        ))
        await _append_unfenced(session_id, rows)

        try:
            new_title = await asyncio.to_thread(
                _voice_exchange_side_effects,
                session_id, user_text, assistant_text, system_text,
            )
            if new_title:
                logger.info(
                    "[TurnRecorder] voice session %s titled %r", session_id, new_title,
                )
        except Exception:
            # The event log is the transcript authority — search rows and board
            # counters failing must not lose the exchange that was just written.
            logger.warning(
                "[TurnRecorder] voice exchange side effects failed for %s",
                session_id, exc_info=True,
            )
        return turn_id
    except Exception:
        logger.warning(
            "[TurnRecorder] record_voice_exchange failed for %s", session_id,
            exc_info=True,
        )
        return None


async def record_voice_tool_exchange(
    session_id: str,
    call_id: str,
    name: str,
    tool_input: Dict[str, Any],
    result: str,
    *,
    is_error: bool = False,
) -> Optional[str]:
    """Persist a voice research result using the existing tool-card contract."""
    try:
        turn_id = f"voice-tool-{uuid.uuid4()}"
        rows = [
            _build_event(contract.TurnStarted, session_id, turn_id=turn_id),
            _build_event(
                contract.ToolUse, session_id, turn_id=turn_id,
                tool_use_id=call_id, name=name, input=tool_input,
            ),
            _build_event(
                contract.ToolResult, session_id, turn_id=turn_id,
                tool_use_id=call_id, is_error=is_error,
                content_preview=_truncate(result),
            ),
            _build_event(
                contract.TurnCompleted, session_id, turn_id=turn_id,
                stop_reason="tool_result",
            ),
        ]
        await _append_unfenced(session_id, rows)
        return turn_id
    except Exception:
        logger.warning(
            "[TurnRecorder] voice tool persistence failed for %s",
            session_id, exc_info=True,
        )
        return None


async def record_task_start_frame(
    session_id: str,
    task_id: Optional[str],
    *,
    description: Optional[str] = None,
    tool_use_id: Optional[str] = None,
    summary: Optional[str] = None,
) -> None:
    """Emit the durable RUNNING task frame that puts a task on the chip strip.

    The mirror of :func:`record_task_settle_frame` for the START of a task's
    life, and the reason a long-lived background shell is visible after a
    reload or a session switch: ``deriveActiveTasks`` (feed-controller.ts) is
    built purely from ``task.updated`` summaries, so without this frame the only
    surface a running Monitor shell has is the LIVE feed of the turn that
    spawned it — nothing durable, nothing to replay.

    ``status`` is an explicit ``running`` (rather than the per-type ``started``
    default) to match the ProviderAgent worker's start frame; the reducer keeps
    only ``running`` summaries on the strip.

    Deduped downstream by the shared ``mark_chipped(..., "start")`` latch in
    :func:`_translate_shared`, so a task whose in-stream ``TaskStarted`` also
    lands records exactly ONE row. Never raises.
    """
    await record_drained_event(session_id, {
        "type": "task_started",
        "task_id": task_id,
        "status": "running",
        "description": description,
        "summary": summary,
        "tool_use_id": tool_use_id,
    })


async def record_task_settle_frame(
    session_id: str,
    task_id: Optional[str],
    status: str,
    summary: Optional[str],
    *,
    reason: Optional[str] = None,
    tool_use_id: Optional[str] = None,
) -> None:
    """Emit the terminal task-settle frame the FE reducer consumes.

    Single source of truth for the ``system``/``task_updated`` frame that
    settles a background-task chip/subagent card to a terminal state — every
    producer (boot reconcile, the durable force-settle fallback, the
    terminal-surface guarantee) MUST route through here so the frame's shape
    stays identical across paths. Builds the ``data`` payload
    (``task_id``/``status``/``summary`` + a ``patch`` the reducer prefers),
    including ``reason``/``tool_use_id`` only when truthy, and hands it to
    :func:`record_drained_event` (unfenced — legal between turns / at boot).
    """
    data: Dict[str, Any] = {
        "task_id": task_id,
        "status": status,
        "summary": summary,
        "patch": {"status": status},
    }
    if reason:
        data["reason"] = reason
    if tool_use_id:
        data["tool_use_id"] = tool_use_id
    await record_drained_event(session_id, {
        "type": "system",
        "subtype": "task_updated",
        "content": summary,
        "data": data,
    })


async def record_parked(session_id: str, task_ids: List[str]) -> None:
    """Append ``turn.parked`` when a turn parks over active bg work."""
    try:
        await _append_unfenced(session_id, [_build_event(
            contract.TurnParked, session_id,
            task_ids=[t for t in (task_ids or []) if t],
        )])
    except Exception:
        logger.warning(
            "[TurnRecorder] record_parked failed for %s", session_id,
            exc_info=True,
        )


async def record_interrupting(session_id: str, mode: str) -> None:
    """Append ``turn.interrupting`` on a user stop / send-now intent."""
    try:
        await _append_unfenced(session_id, [_build_event(
            contract.TurnInterrupting, session_id, mode=mode,
        )])
    except Exception:
        logger.warning(
            "[TurnRecorder] record_interrupting failed for %s", session_id,
            exc_info=True,
        )
