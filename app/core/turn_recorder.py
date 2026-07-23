"""V2 TurnRecorder — typed event-log appender tapped into the SDK pump (Phase 1).

Every session streams on the V2 path, so this module translates every legacy
pump event (``stream_to_websocket`` yields, plus the between-turns drain pump's
re-dispatches) into typed contract events (`app.contract.events`) appended to
the append-only ``session_events`` log (`app.db.session_events`) under a fenced
run lease (`app.db.runs`). Design: ``docs/V2_PHASE1_DESIGN.md`` §2–§3.

Invariants (program rules):
- NEVER imports ``sync_engine`` or any WebSocket-layer module.
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
from typing import Any, Dict, List, Optional

from app.contract import events as contract
from app.core import artifact_ingest, artifact_store
from app.core.config import settings
from app.db import runs as runs_db
from app.db import session_events as session_events_db

logger = logging.getLogger(__name__)

# Tunables — module-level so tests can monkeypatch deterministic values.
FLUSH_INTERVAL_SECONDS = 0.15  # flush deltas this long after the first buffered
FLUSH_MAX_BUFFER_CHARS = 2048  # ...or once a kind's buffer reaches this size
USAGE_COALESCE_SECONDS = 2.0   # at most one turn.usage append per window
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


def _notify_run_change(session_id: str) -> None:
    """Run-lifecycle hook for the board spinner (streaming_status re-check).

    Lazy import + never raises: a status/broadcast bug must never break
    recording or the turn.
    """
    try:
        from app.core import streaming_status

        streaming_status.notify(session_id)
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
# artifact store (Q5). ``tool_result`` renders on the main-lane tool card;
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
    session_id: str, event: Dict[str, Any], *, turn_id: Optional[str] = None
) -> List[Dict[str, Any]]:
    """Unfenced-path screenshot ingest (drain pump / CLI wake turns).

    Mirrors ``TurnRecorder._maybe_ingest_screenshot`` for callers with no live
    recorder: stamp ``artifact_hashes`` onto the pump event, store the bytes
    (awaited — these paths are not latency-critical and have no close() drain to
    guarantee durability), and return the ``artifact.created`` row to append
    before the translated event. Returns ``[]`` on flag-off / no screenshot;
    never raises.
    """
    try:
        if not settings.v2_placement:
            return []
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
            contract.ArtifactCreated, session_id, turn_id=turn_id,
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
            agent_type=event.get("agent_type"),
            description=event.get("description"),
            prompt_preview=_truncate(event.get("prompt")),
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
                # The SDK's TaskUpdatedMessage never carries tool_use_id; enrich
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

    # Not-recorded row (§3): stream_block_start/stop, stream_message_delta
    # (non-usage), rate_limit, plugin_message, heartbeat, api_error — and
    # anything unknown (forward compatibility).
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
        # True when THIS recorder owns the run lease (the legacy ``maybe_create``
        # self-claim path): ``close()`` then releases the lease. The Phase-2
        # ``create_for_run`` injection path sets this False — the SessionRuntime
        # owns the lease + renew loop and releases it itself, so ``close()`` must
        # NOT release out from under the runtime.
        self._owns_lease = True

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
        # seq of this turn's FIRST appended event — the truncation point for
        # an empty-turn discard (record_empty_turn_discard, wired in a later
        # commit).
        self._first_seq: Optional[int] = None
        self._renew_task: Optional[asyncio.Task] = None
        # Serializes buffer mutation + appends (on_event vs. the flush timer)
        # so log order always reflects pump order.
        self._lock = asyncio.Lock()
        # In-flight fire-and-forget screenshot-artifact stores (Q5). Held so the
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
        # Seed the event log from legacy history BEFORE this turn's first
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
            # Run went active — let the durable busy view re-check + broadcast.
            _notify_run_change(session_id)
        else:
            logger.warning(
                "[TurnRecorder] could not claim run lease for %s "
                "(another active run?) — recording UNFENCED", session_id,
            )

        recorder = cls(session_id, command_id, run_id, fence_token, runner_id)
        if run_id is not None:
            recorder._renew_task = asyncio.create_task(recorder._renew_loop())
        # Registry of the LAST recorder per session so the post-turn
        # empty-turn-discard path (``discard_empty_turn``) can reach it after
        # the pump has closed it — the instance itself never escapes
        # ``stream_to_websocket``.
        _last_turn_recorders[session_id] = recorder
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
        (Phase 2 injection path).

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
        legacy history backfill before the turn's first append and never raises
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
        _last_turn_recorders[session_id] = recorder
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
            # Drain in-flight screenshot stores so the bytes are durable before
            # teardown (Q5). Errors are already swallowed inside each task.
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

    async def record_empty_turn_discard(self) -> None:
        """Append ``transcript.rewound`` erasing this turn's events.

        For turns whose user message is discarded end-to-end (stopped before
        any output — see the interrupted-user-message poller). Truncates the
        reduced transcript from this turn's FIRST appended event. Unfenced —
        the discard decision lands after the turn's run lease is released.
        Reached post-turn via module-level ``discard_empty_turn``. Never raises.
        """
        if self._first_seq is None:
            return
        try:
            await _append_unfenced(self.session_id, [_build_event(
                contract.TranscriptRewound, self.session_id,
                from_seq=self._first_seq,
            )])
        except Exception:
            logger.warning(
                "[TurnRecorder] record_empty_turn_discard failed for %s",
                self.session_id, exc_info=True,
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
            # usage is an open dict in the contract, so per-model usage rides here to stay durable in session_events under postgres_only.
            if metadata.get("model_usage"):
                usage_summary["model_usage"] = metadata["model_usage"]
            # Phase 2 A5 amendment (plan §1.6(1)): carry the pump's API error
            # status into the terminal payload — a usage-limit 429 turn
            # otherwise settles as a normal completion with api_error_status
            # living only in in-memory pump metadata, leaving the scheduler's
            # failover policy nothing durable to key on. ``extra_payload``
            # because the frozen contract model has no field for it (same
            # mechanism as turn.aborted ``message``). ``ResultMessage`` exposes
            # only ``api_error_status`` — no message field is ever populated.
            api_error: Optional[Dict[str, Any]] = None
            if metadata.get("api_error_status") is not None:
                api_error = {"api_error_status": metadata["api_error_status"]}
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
            # Q5: content-address a tool_result screenshot BEFORE translation so
            # the ToolResult carries its artifact_hashes; append artifact.created
            # first (no-op for every non-screenshot event / flag OFF).
            events.extend(self._maybe_ingest_screenshot(event))
            events.extend(_translate_shared(
                event, self.session_id,
                turn_id=self.turn_id, command_id=self.command_id,
            ))

        await self._append(events)

    # ------------------------------------------------------------------
    # screenshot artifact ingest (Q5)
    # ------------------------------------------------------------------

    def _maybe_ingest_screenshot(
        self, event: Dict[str, Any]
    ) -> List[Dict[str, Any]]:
        """Content-address a screenshot result (flag-gated, fire-and-forget).

        On a ``tool_result`` / ``subagent_tool_result`` pump event carrying a
        decoded screenshot data URI (browser / computer-use image results —
        surfaced at the ``query_engine`` pump sites), and only when
        ``settings.v2_placement`` is on, decode + hash the bytes synchronously,
        stamp ``artifact_hashes`` onto the pump event (so ``_translate_shared``
        carries the reference into the recorded ``tool.result`` /
        ``subagent.progress``), schedule a fire-and-forget disk/DB store, and
        return an ``artifact.created`` event to append in the same batch.

        Returns ``[]`` (a no-op) for every other event, when the flag is off, or
        when the screenshot is not a well-formed base64 data URI. Never raises:
        a decode/scheduling failure degrades to no ingest, never breaking the
        pump (the recorder isolation invariant).
        """
        try:
            if not settings.v2_placement:
                return []
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
                seqs = await asyncio.to_thread(
                    session_events_db.append_events,
                    self.session_id, events,
                    run_id=self.run_id, fence_token=self.fence_token,
                )
            else:
                seqs = await asyncio.to_thread(
                    session_events_db.append_events, self.session_id, events
                )
            if self._first_seq is None and seqs:
                self._first_seq = seqs[0]
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

# Last recorder per session (set by ``TurnRecorder.maybe_create``). Lets the
# post-turn empty-turn-discard path reach the just-closed recorder — the
# instance itself is encapsulated inside ``stream_to_websocket``. Overwritten
# every turn; entries are tiny (buffers are drained on close).
_last_turn_recorders: Dict[str, "TurnRecorder"] = {}


async def discard_empty_turn(session_id: str) -> None:
    """Append ``transcript.rewound`` erasing the LAST recorded turn's events.

    Caller: the #420 empty-turn discard in ``chat_ws_handler.run_query`` —
    a turn stopped before ANY output whose user message is discarded
    end-to-end. Delegates to the last recorder's
    ``record_empty_turn_discard`` (no-op if the turn appended nothing or no
    recorder exists). Never raises.
    """
    recorder = _last_turn_recorders.get(session_id)
    if recorder is None:
        return
    await recorder.record_empty_turn_discard()


def pop_last_turn_recorder(session_id: str) -> None:
    """Drop the session's last-recorder entry (session teardown).

    ``_last_turn_recorders`` is overwritten per turn but never emptied when a
    session goes away, so a long-lived server leaks one (tiny) entry per dead
    session. Called from the session teardown paths alongside
    ``task_registry.clear_session``. Never raises.
    """
    _last_turn_recorders.pop(session_id, None)


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
            # Deliberately narrow (other drain callers depend on it). Log the
            # dropped type so a silently-unrendered drain event is diagnosable —
            # the CLI-wake render surface (chunk/tool_use/tool_result) is served
            # by record_cli_wake_event, NOT by widening this filter.
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


async def record_cli_wake_event(
    session_id: str, event: Dict[str, Any], *, turn_id: Optional[str] = None
) -> int:
    """Record ONE forwarded CLI-owned wake-turn event to the session log.

    The between-turns CLI wake turn (``idle_reader``) renders assistant TEXT and
    top-level tool activity that :func:`record_drained_event` deliberately drops
    (its filter accepts only ``subagent_*``/``task_*``/``system``). This entry
    point widens the accepted set to the full render surface — ``chunk`` /
    ``tool_use`` / ``tool_result`` PLUS everything ``record_drained_event``
    accepts — and stamps the synthesized wake ``turn_id`` so each row ties to the
    ``turn.started``/``turn.completed`` the reader brackets around the wake.

    Unfenced append (run_id=None): the CLI wake runs between turns under the idle
    reader's reader token, so no fenced ``TurnRecorder`` can be live for the same
    session concurrently. Returns the number of contract rows appended (``0`` =
    filtered out or untranslatable). Never raises.
    """
    try:
        if not isinstance(event, dict):
            return 0
        etype = event.get("type") or ""
        if not (
            etype in ("chunk", "tool_use", "tool_result")
            or etype.startswith("subagent_")
            or etype.startswith("task_")
            or etype == "system"
        ):
            return 0
        # Screenshot ingest BEFORE translation (mirrors the fenced record()
        # order) so a wake-turn browser/computer-use screenshot renders too.
        rows = await _ingest_screenshot_unfenced(session_id, event, turn_id=turn_id)
        rows.extend(_translate_shared(event, session_id, turn_id=turn_id))
        await _append_unfenced(session_id, rows)
        return len(rows)
    except Exception:
        logger.warning(
            "[TurnRecorder] record_cli_wake_event failed for %s", session_id,
            exc_info=True,
        )
        return 0


async def record_cli_wake_turn_started(session_id: str, turn_id: str) -> None:
    """Append a synthetic ``turn.started`` opening a CLI-owned wake turn.

    Brackets the between-turns wake as a first-class turn so the FE reducer flips
    its streaming indicator (``turn.started`` → ``isStreaming``) and — on the
    paired ``turn.completed`` — settles any speculative subagent card. ``turn_id``
    is a synthesized id with NO ``turns``-table row; the scheduler keys its
    terminal reads by turns-table ids, so a synthetic wake turn is invisible to
    the parked-turn state machine. Unfenced. Never raises.
    """
    try:
        await _append_unfenced(session_id, [_build_event(
            contract.TurnStarted, session_id, turn_id=turn_id,
        )])
    except Exception:
        logger.warning(
            "[TurnRecorder] record_cli_wake_turn_started failed for %s",
            session_id, exc_info=True,
        )


async def record_cli_wake_turn_completed(
    session_id: str,
    turn_id: str,
    *,
    usage: Optional[Dict[str, Any]] = None,
    stop_reason: Optional[str] = None,
) -> None:
    """Append a synthetic ``turn.completed`` closing a CLI-owned wake turn.

    Mirrors the fenced recorder's ``done`` terminal shape (``stop_reason`` +
    ``usage`` summary) so the FE reducer treats the wake like a normal turn:
    ``isStreaming`` off + ``settleStaleBackgroundWork``. Paired with
    :func:`record_cli_wake_turn_started` (same ``turn_id``). Unfenced. Never
    raises.
    """
    try:
        await _append_unfenced(session_id, [_build_event(
            contract.TurnCompleted, session_id, turn_id=turn_id,
            stop_reason=stop_reason, usage=usage or {},
        )])
    except Exception:
        logger.warning(
            "[TurnRecorder] record_cli_wake_turn_completed failed for %s",
            session_id, exc_info=True,
        )


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
    """Append ``turn.parked`` when the drain pump starts over active bg work."""
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
