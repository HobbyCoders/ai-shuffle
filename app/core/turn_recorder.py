"""V2 TurnRecorder — typed event-log appender tapped into the SDK pump (Phase 1).

For sessions flagged ``sessions.v2_stream`` this module translates every legacy
pump event (``stream_to_websocket`` yields, plus the between-turns drain pump's
re-dispatches) into typed contract events (`app.contract.events`) appended to
the append-only ``session_events`` log (`app.db.session_events`) under a fenced
run lease (`app.db.runs`). Design: ``docs/V2_PHASE1_DESIGN.md`` §2–§3.

Invariants (program rules):
- NEVER imports ``live_session_store``, ``ws_event_handlers`` or ``sync_engine``.
- A recorder bug must never break streaming: every public method swallows and
  logs its own exceptions; all DB appends run off-loop via ``asyncio.to_thread``.
- Recording never blocks a turn: if the run lease cannot be claimed, events are
  appended UNFENCED (``run_id=None``) with a logged warning.

Delta batching (§2): ``stream_delta`` text/thinking/tool-input payloads are
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
from typing import Any, Dict, List, Optional, Tuple

from app.contract import events as contract
from app.core.config import settings
from app.db import runs as runs_db
from app.db import session_events as session_events_db
from app.db import sessions as sessions_db

logger = logging.getLogger(__name__)

# Tunables — module-level so tests can monkeypatch deterministic values.
FLUSH_INTERVAL_SECONDS = 0.15  # flush deltas this long after the first buffered
FLUSH_MAX_BUFFER_CHARS = 2048  # ...or once a kind's buffer reaches this size
USAGE_COALESCE_SECONDS = 2.0   # at most one turn.usage append per window
RESULT_TRUNCATE_CHARS = 65536  # tool.result content / subagent result previews
RUN_LEASE_TTL_SECONDS = 60
RUN_LEASE_RENEW_SECONDS = 20
V2_FLAG_CACHE_TTL_SECONDS = 60.0  # per-session v2_stream cache for drained events

# Pump ``system`` subtypes recorded as system.compaction_boundary (passthrough).
_COMPACTION_SUBTYPES = ("compact_boundary", "microcompact_boundary")

# Envelope fields of EventBase — everything else on a model is its payload.
_ENVELOPE_FIELDS = {
    "id", "session_id", "seq", "run_id", "turn_id", "command_id",
    "created_at", "type",
}


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


def _build_event(
    model_cls: type,
    session_id: str,
    *,
    turn_id: Optional[str] = None,
    command_id: Optional[str] = None,
    extra_payload: Optional[Dict[str, Any]] = None,
    **fields: Any,
) -> Dict[str, Any]:
    """Construct a contract event model and reduce it to append_events shape.

    ``id``/``seq``/``created_at`` are server-assigned by the log layer, so the
    model is built with placeholders purely to validate the typed payload
    fields; only ``{"type", "payload", "turn_id", "command_id"}`` is appended.
    ``extra_payload`` carries §3 payload notes the frozen contract models have
    no field for (e.g. turn.aborted ``message``) — payload is a free-form JSON
    column and the reducer ignores unknown keys.
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
    payload = model.model_dump(mode="json", exclude=_ENVELOPE_FIELDS)
    if extra_payload:
        payload.update(extra_payload)
    return {
        "type": model.type,
        "payload": payload,
        "turn_id": turn_id,
        "command_id": command_id,
    }


def _translate_shared(
    event: Dict[str, Any],
    session_id: str,
    *,
    turn_id: Optional[str] = None,
    command_id: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """§3 translation for the stateless pump event types.

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
        return [_build_event(
            contract.ToolResult, session_id,
            tool_use_id=event.get("tool_use_id") or "",
            is_error=bool(event.get("is_error")),
            content_preview=_truncate(event.get("output")),
            **kw,
        )]

    if etype == "subagent_start":
        return [_build_event(
            contract.SubagentStarted, session_id,
            task_id=event.get("tool_id") or event.get("agent_id") or "",
            agent_id=event.get("agent_id"),
            agent_type=event.get("agent_type"),
            description=event.get("description"),
            prompt_preview=_truncate(event.get("prompt")),
            **kw,
        )]

    if etype in ("subagent_chunk", "subagent_tool_use", "subagent_tool_result"):
        payload = {
            k: v for k, v in event.items()
            if k not in ("type", "agent_id", "session_id")
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
            **kw,
        )]

    if etype in ("task_progress", "task_started", "task_notification"):
        state = event.get("status") or {
            "task_started": "started",
            "task_progress": "running",
            "task_notification": "notified",
        }[etype]
        return [_build_event(
            contract.TaskUpdated, session_id,
            task_id=event.get("task_id") or "",
            state=str(state),
            description=event.get("description"),
            summary=event.get("summary"),
            tool_use_id=event.get("tool_use_id"),
            **kw,
        )]

    if etype == "system":
        subtype = event.get("subtype") or ""
        if subtype == "task_updated":
            # Background-task status chip (TaskUpdatedMessage in-stream, or the
            # drain pump's mirrored/synthesized shape — see
            # _task_status_events_from_message). Recording it as a generic
            # system.note loses the terminal state: a replay then shows the
            # task stuck on its last in-turn "running" update forever.
            data = _as_dict(event.get("data"))
            task_id = data.get("task_id")
            if task_id:
                patch = data.get("patch")
                patch = patch if isinstance(patch, dict) else {}
                state = patch.get("status") or data.get("status") or "updated"
                return [_build_event(
                    contract.TaskUpdated, session_id,
                    task_id=str(task_id),
                    state=str(state),
                    description=data.get("description"),
                    summary=data.get("summary"),
                    tool_use_id=data.get("tool_use_id"),
                    **kw,
                )]
        if subtype in _COMPACTION_SUBTYPES:
            # Contract model carries no fields; pass the raw frame through in
            # the payload per the §3 "passthrough" note.
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

    # Not-recorded row (§3): stream_block_start/stop, stream_message_delta
    # (non-usage), rate_limit, hook_event, plugin_message, heartbeat,
    # api_error — and anything unknown (forward compatibility).
    return []


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

        # Delta batching state. Insertion-ordered: kind -> list[str] chunks.
        self._delta_buffers: Dict[str, List[str]] = {}
        self._tool_input_tool_id: str = ""
        self._open_tool_use_id: str = ""  # from the last stream_block_start
        self._flush_task: Optional[asyncio.Task] = None

        # turn.usage coalescing.
        self._last_usage_at: float = float("-inf")
        self._pending_usage: Optional[Dict[str, Any]] = None

        self._terminal_recorded = False
        self._closed = False
        self._renew_task: Optional[asyncio.Task] = None
        # Serializes buffer mutation + appends (on_event vs. the flush timer)
        # so log order always reflects pump order.
        self._lock = asyncio.Lock()

    # ------------------------------------------------------------------
    # lifecycle
    # ------------------------------------------------------------------

    @classmethod
    async def maybe_create(
        cls, session_id: str, command_id: Optional[str] = None
    ) -> Optional["TurnRecorder"]:
        """Return a recorder iff the session row has ``v2_stream`` true.

        Never raises and never blocks the turn: any failure (including a lost
        run-lease claim) degrades to ``None`` / unfenced recording.
        """
        try:
            row = await asyncio.to_thread(sessions_db.get_session, session_id)
        except Exception:
            logger.warning(
                "[TurnRecorder] v2_stream flag read failed for %s — not recording",
                session_id, exc_info=True,
            )
            return None
        if not row or not bool(row.get("v2_stream")):
            return None

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
        else:
            logger.warning(
                "[TurnRecorder] could not claim run lease for %s "
                "(another active run?) — recording UNFENCED", session_id,
            )

        recorder = cls(session_id, command_id, run_id, fence_token, runner_id)
        if run_id is not None:
            recorder._renew_task = asyncio.create_task(recorder._renew_loop())
        return recorder

    async def start(self, user_message: str) -> None:
        """Append ``turn.started`` then ``message.user``."""
        try:
            await self._append([
                self._event(contract.TurnStarted),
                self._event(contract.MessageUser, content=user_message or ""),
            ])
        except Exception:
            logger.warning(
                "[TurnRecorder] start failed for %s", self.session_id,
                exc_info=True,
            )

    async def on_event(self, event: Dict[str, Any]) -> None:
        """Translate one yielded pump event per §3. Never raises."""
        try:
            if not isinstance(event, dict):
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
        try:
            async with self._lock:
                events = self._drain_buffered_events()
                if self._pending_usage is not None:
                    events.append(
                        self._event(contract.TurnUsage, usage=self._pending_usage)
                    )
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
        except Exception:
            logger.warning(
                "[TurnRecorder] close failed for %s", self.session_id,
                exc_info=True,
            )
        finally:
            self._cancel_task(self._flush_task)
            self._flush_task = None
            self._cancel_task(self._renew_task)
            self._renew_task = None
            if self.run_id is not None and self.fence_token is not None:
                try:
                    await asyncio.to_thread(
                        runs_db.release_run, self.run_id, self.fence_token
                    )
                except Exception:
                    logger.warning(
                        "[TurnRecorder] release_run failed for %s run %s",
                        self.session_id, self.run_id, exc_info=True,
                    )

    # ------------------------------------------------------------------
    # translation core (lock held)
    # ------------------------------------------------------------------

    async def _on_event_locked(self, event: Dict[str, Any]) -> None:
        etype = event.get("type")

        if etype == "stream_delta":
            if self._buffer_delta(event):
                # Size threshold (≥ FLUSH_MAX_BUFFER_CHARS) — flush now.
                await self._append(self._drain_buffered_events())
            return

        # Any non-delta pump event flushes buffered deltas FIRST so the log
        # order always reflects pump order (§2) — including not-recorded
        # events like stream_block_start (a block boundary).
        events = self._drain_buffered_events()

        if etype == "stream_block_start":
            # Not recorded, but tracks the open tool_use block so buffered
            # tool-input deltas can carry their tool_use_id.
            content_block = event.get("content_block") or {}
            if content_block.get("type") == "tool_use" and content_block.get("id"):
                self._open_tool_use_id = content_block["id"]
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
                    "num_turns",
                )
                if k in metadata
            }
            events.append(self._event(
                contract.TurnCompleted,
                stop_reason=metadata.get("stop_reason"),
                usage=usage_summary,
            ))
            self._terminal_recorded = True
        elif etype == "interrupted":
            events.extend(self._flush_pending_usage())
            events.append(self._event(contract.TurnAborted, reason="interrupt"))
            self._terminal_recorded = True
        elif etype == "error":
            events.extend(self._flush_pending_usage())
            events.append(self._event(
                contract.TurnAborted, reason="error",
                extra_payload={"message": event.get("message")},
            ))
            self._terminal_recorded = True
        else:
            events.extend(_translate_shared(
                event, self.session_id,
                turn_id=self.turn_id, command_id=self.command_id,
            ))

        await self._append(events)

    # ------------------------------------------------------------------
    # delta batching
    # ------------------------------------------------------------------

    def _buffer_delta(self, event: Dict[str, Any]) -> bool:
        """Buffer one stream_delta. Returns True when a size-flush is due."""
        delta_type = event.get("delta_type")
        content = event.get("content") or ""
        if delta_type not in ("text", "thinking", "tool_input") or not content:
            return False
        if delta_type == "tool_input" and "tool_input" not in self._delta_buffers:
            # Stamp the open block's id at first buffer; a new tool block's
            # stream_block_start flushes this buffer before re-stamping.
            self._tool_input_tool_id = self._open_tool_use_id
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
            elif kind == "thinking":
                events.append(
                    self._event(contract.AssistantThinkingDelta, text=text)
                )
            elif kind == "tool_input":
                events.append(self._event(
                    contract.ToolInputDelta,
                    tool_use_id=self._tool_input_tool_id,
                    partial_json=text,
                ))
        self._delta_buffers.clear()
        self._cancel_task(self._flush_task)
        self._flush_task = None
        return events

    # ------------------------------------------------------------------
    # usage coalescing
    # ------------------------------------------------------------------

    def _coalesce_usage(self, usage: Dict[str, Any]) -> List[Dict[str, Any]]:
        now = time.monotonic()
        if now - self._last_usage_at >= USAGE_COALESCE_SECONDS:
            self._last_usage_at = now
            self._pending_usage = None
            return [self._event(contract.TurnUsage, usage=usage)]
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
        return [self._event(contract.TurnUsage, usage=usage)]

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
        except session_events_db.FenceViolation:
            logger.warning(
                "[TurnRecorder] fence violation for %s run %s — %d event(s) "
                "dropped (superseded runner)",
                self.session_id, self.run_id, len(events),
            )
        except Exception:
            logger.warning(
                "[TurnRecorder] append failed for %s (%d event(s) dropped)",
                self.session_id, len(events), exc_info=True,
            )

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
# Module-level taps (drain pump / stop / park) — all unfenced appends.
# ----------------------------------------------------------------------

# session_id -> (checked_at_monotonic, flag). 60s TTL so the drain pump does
# not pay a DB read per drained event.
_v2_flag_cache: Dict[str, Tuple[float, bool]] = {}


def _reset_flag_cache() -> None:
    """Test helper — drop the per-session v2_stream cache."""
    _v2_flag_cache.clear()


async def _session_is_v2(session_id: str) -> bool:
    now = time.monotonic()
    cached = _v2_flag_cache.get(session_id)
    if cached is not None and now - cached[0] < V2_FLAG_CACHE_TTL_SECONDS:
        return cached[1]
    try:
        row = await asyncio.to_thread(sessions_db.get_session, session_id)
        flag = bool(row and row.get("v2_stream"))
    except Exception:
        logger.warning(
            "[TurnRecorder] v2_stream flag read failed for %s — treating as off",
            session_id, exc_info=True,
        )
        flag = False
    _v2_flag_cache[session_id] = (now, flag)
    return flag


async def _append_unfenced(session_id: str, events: List[Dict[str, Any]]) -> None:
    if not events:
        return
    await asyncio.to_thread(session_events_db.append_events, session_id, events)


async def record_drained_event(session_id: str, event: Dict[str, Any]) -> None:
    """Record a drain-pump re-dispatch (§2): subagent_*/task_*/system only.

    Unfenced append (run_id=None, turn_id=None) — the drain pump runs between
    turns with no owning run. Never raises.
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
            return
        if not await _session_is_v2(session_id):
            return
        await _append_unfenced(session_id, _translate_shared(event, session_id))
    except Exception:
        logger.warning(
            "[TurnRecorder] record_drained_event failed for %s", session_id,
            exc_info=True,
        )


async def record_parked(session_id: str, task_ids: List[str]) -> None:
    """Append ``turn.parked`` when the drain pump starts over active bg work."""
    try:
        if not await _session_is_v2(session_id):
            return
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
        if not await _session_is_v2(session_id):
            return
        await _append_unfenced(session_id, [_build_event(
            contract.TurnInterrupting, session_id, mode=mode,
        )])
    except Exception:
        logger.warning(
            "[TurnRecorder] record_interrupting failed for %s", session_id,
            exc_info=True,
        )
