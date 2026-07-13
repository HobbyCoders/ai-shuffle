"""Lazy, persisted read-time backfill of legacy history into ``session_events``.

A session created before the V2 event log existed (or PATCHed to ``v2_stream``
mid-life) has its transcript only in the legacy stores (``session_messages`` /
JSONL / the Postgres session store). When such a session is first read through a
V2 path (the SSE tail, a recorded turn, or a rewind), :func:`ensure_backfilled`
converts that legacy transcript into typed contract events
(:mod:`app.contract.events`) and appends them ONCE, so the event log carries the
full history before any live event lands on top.

Design (``docs/V2_CORE_REARCHITECTURE.md`` Part 1):

- Fast path: ``session_events.get_max_seq(session_id) > 0`` → already populated,
  return immediately. A 60s per-process memo suppresses repeated legacy loads
  for a genuinely-empty session.
- Legacy history is loaded via the SAME persistent chain the ``/live`` read path
  uses (``db_message_loader`` → ``jsonl``/store parser → legacy rescue),
  off-loop in a thread.
- Every produced event is stamped ``turn_id='backfill'`` and ``run_id=NULL``.
- The append goes through :func:`app.db.session_events.append_backfill_events`,
  whose exclusive-create counter gate guarantees exactly one writer wins the
  race against a concurrent recorder or a second reader.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Dict, List, Optional

from app.contract import events as contract
from app.core.system_message_filter import is_hidden_system_subtype
from app.core.turn_recorder import _build_event, _truncate
from app.db import session_events as session_events_db
from app.db import sessions as sessions_db

logger = logging.getLogger(__name__)

BACKFILL_TURN_ID = "backfill"

# session_id -> monotonic time we last saw an EMPTY legacy transcript. Suppresses
# a repeated (expensive) legacy load on every read of a session that genuinely
# has no history yet. Only empties are memoed — once the log is populated the
# get_max_seq fast path short-circuits.
_EMPTY_MEMO_TTL_SECONDS = 60.0
_empty_memo: Dict[str, float] = {}


def _reset_memo() -> None:
    """Test helper — drop the per-process empty-session memo."""
    _empty_memo.clear()


# ----------------------------------------------------------------------
# public entry point
# ----------------------------------------------------------------------

async def ensure_backfilled(session_id: str) -> None:
    """Backfill a session's legacy history into the event log exactly once.

    Never raises and never blocks a turn: any failure degrades to a no-op (the
    reader simply sees whatever is already in the log). Idempotent — the
    get_max_seq fast path and the counter gate both prevent a second backfill.
    """
    try:
        max_seq = await asyncio.to_thread(session_events_db.get_max_seq, session_id)
    except Exception:
        logger.warning(
            "[history_backfill] get_max_seq failed for %s — skipping backfill",
            session_id, exc_info=True,
        )
        return
    if max_seq > 0:
        return

    now = time.monotonic()
    memoed = _empty_memo.get(session_id)
    if memoed is not None and now - memoed < _EMPTY_MEMO_TTL_SECONDS:
        return

    try:
        messages = await asyncio.to_thread(_load_legacy_history, session_id)
    except Exception:
        logger.warning(
            "[history_backfill] legacy load failed for %s — skipping backfill",
            session_id, exc_info=True,
        )
        return

    if not messages:
        _empty_memo[session_id] = now
        return

    events = convert_messages_to_events(session_id, messages)
    if not events:
        _empty_memo[session_id] = now
        return

    try:
        result = await asyncio.to_thread(
            session_events_db.append_backfill_events, session_id, events
        )
    except Exception:
        logger.warning(
            "[history_backfill] append_backfill_events failed for %s",
            session_id, exc_info=True,
        )
        return

    if result is None:
        # Lost the exclusive-create race — another writer populated the log
        # first. Nothing to do; their events are authoritative.
        logger.debug(
            "[history_backfill] lost backfill race for %s (log already seeded)",
            session_id,
        )
        return

    first_seq, last_seq = result
    logger.info(
        "[history_backfill] backfilled %d legacy message(s) for %s as events "
        "[%d..%d]", len(messages), session_id, first_seq, last_seq,
    )


# ----------------------------------------------------------------------
# legacy history loading (persistent chain)
# ----------------------------------------------------------------------

def _load_legacy_history(session_id: str) -> List[Dict[str, Any]]:
    """Load a session's legacy transcript in frontend message format.

    Mirrors the ``/live`` persistent fallback chain (``app.api.sessions``
    get_live_session):

      1. legacy ``session_messages`` via ``db_message_loader`` (non postgres_only)
      2. JSONL parse — or the store-backed parser under postgres_only
      3. postgres_only legacy rescue: empty store → legacy ``session_messages``
    """
    session = sessions_db.get_session(session_id)
    if not session:
        return []

    from app.core.config import settings
    postgres_only = settings.session_store_mode == "postgres_only"

    if not postgres_only:
        try:
            from app.core.db_message_loader import load_messages_from_db
            msgs, has_db = load_messages_from_db(session_id)
            if has_db:
                return msgs
        except Exception:
            logger.warning(
                "[history_backfill] db_message_loader failed for %s", session_id,
                exc_info=True,
            )

    sdk_session_id = session.get("sdk_session_id")
    if sdk_session_id:
        try:
            from app.core.message_resolver import resolve_working_dir
            working_dir = resolve_working_dir(dict(session))
            if postgres_only:
                from app.api.sessions_helpers import parse_session_history_via_store
                msgs, _usage = parse_session_history_via_store(
                    sdk_session_id, working_dir
                )
            else:
                from app.core.jsonl_parser import parse_session_history
                msgs, _usage = parse_session_history(sdk_session_id, working_dir)
            if msgs:
                return msgs
        except Exception:
            logger.warning(
                "[history_backfill] transcript parse failed for %s", session_id,
                exc_info=True,
            )

    if postgres_only:
        # Store yielded nothing — pre-cutover session whose transcript lives only
        # in the legacy shared session_messages table. Mirror /live's rescue.
        try:
            from app.core.db_message_loader import load_messages_from_db
            msgs, has_db = load_messages_from_db(session_id)
            if has_db:
                return msgs
        except Exception:
            logger.warning(
                "[history_backfill] legacy rescue failed for %s", session_id,
                exc_info=True,
            )

    return []


# ----------------------------------------------------------------------
# frontend message -> contract event conversion (mapping table, Part 1)
# ----------------------------------------------------------------------

def convert_messages_to_events(
    session_id: str, messages: List[Dict[str, Any]]
) -> List[Dict[str, Any]]:
    """Convert frontend-format legacy messages to append-ready contract events."""
    events: List[Dict[str, Any]] = []
    for msg in messages:
        if not isinstance(msg, dict):
            continue
        try:
            events.extend(_convert_one(session_id, msg))
        except Exception:
            logger.warning(
                "[history_backfill] failed to convert message %r (type=%r)",
                msg.get("id"), msg.get("type"), exc_info=True,
            )
    return events


def _event(model_cls: type, session_id: str, *, extra_payload=None, **fields):
    """Build a backfill event (turn_id='backfill', unfenced)."""
    return _build_event(
        model_cls, session_id,
        turn_id=BACKFILL_TURN_ID, extra_payload=extra_payload, **fields,
    )


def _timestamp(msg: Dict[str, Any]) -> Optional[Any]:
    meta = msg.get("metadata")
    if isinstance(meta, dict) and meta.get("timestamp"):
        return meta.get("timestamp")
    return msg.get("timestamp")


def _model(msg: Dict[str, Any]) -> Optional[str]:
    meta = msg.get("metadata")
    if isinstance(meta, dict):
        return meta.get("model")
    return None


def _as_input(value: Any) -> Dict[str, Any]:
    return value if isinstance(value, dict) else {}


_CHILD_KINDS = {"tool_use": "tool_use", "tool_result": "tool_result"}


def _child_kind(raw: Any) -> str:
    """Map a subagent/team child ``type`` to a progress ``kind``."""
    return _CHILD_KINDS.get(raw, "chunk")


def _child_payload(kind: str, child: Dict[str, Any]) -> Dict[str, Any]:
    """Translate a v1 camelCase child into the v2 progress payload shape.

    Legacy children carry v1 message keys (toolName/toolId/toolInput/
    toolResult/toolStatus), but the v2 reducer reads snake_case progress
    payloads. Golden shape = the live progress events built by
    ``team_transcript_watcher`` (tool_use: id/name/input; tool_result:
    tool_use_id/output/is_error; chunk: content).
    """
    if kind == "tool_use":
        return {
            "id": child.get("toolId") or "",
            "name": child.get("toolName") or "",
            "input": _as_input(child.get("toolInput")),
        }
    if kind == "tool_result":
        output = child.get("toolResult")
        if output is None:
            output = child.get("content")
        return {
            "tool_use_id": child.get("toolId") or "",
            "output": "" if output is None else str(output),
            "is_error": child.get("toolStatus") == "error",
        }
    return {"content": child.get("content")}


def _terminal_status(raw: Any) -> str:
    """Map a persisted agent/teammate status to the completed-event enum."""
    if raw in ("error", "failed"):
        return "failed"
    if raw in ("killed", "cancelled", "canceled"):
        return "killed"
    return "completed"


def _convert_one(session_id: str, msg: Dict[str, Any]) -> List[Dict[str, Any]]:
    role = msg.get("role")
    mtype = msg.get("type")
    ts = _timestamp(msg)
    uuid_ = msg.get("uuid")
    profile_id = msg.get("profile_id")
    content = msg.get("content") or ""

    # --- skipped noise: task-notification user rows, queued/rate_limit ---
    if role == "user" and isinstance(content, str) and "<task-notification>" in content:
        return []
    if mtype in ("rate_limit", "queued"):
        return []

    # --- user plain / command ---
    if role == "user" and mtype in (None, "text", "user"):
        return [_event(
            contract.MessageUser, session_id,
            content=content, kind="user",
            profile_id=profile_id, uuid=uuid_, timestamp=ts,
        )]
    if role == "user" and mtype == "command":
        return [_event(
            contract.MessageUser, session_id,
            content=content, kind="command",
            command_name=msg.get("commandName"),
            command_args=msg.get("commandArgs"),
            profile_id=profile_id, uuid=uuid_, timestamp=ts,
        )]

    # --- assistant text / thinking ---
    if mtype == "text" and role == "assistant":
        return [_event(
            contract.MessageAssistant, session_id,
            content=content, kind="text", model=_model(msg),
            profile_id=profile_id, uuid=uuid_, timestamp=ts,
        )]
    if mtype == "thinking":
        return [_event(
            contract.MessageAssistant, session_id,
            content=content, kind="thinking", model=_model(msg),
            profile_id=profile_id, uuid=uuid_, timestamp=ts,
        )]

    # --- tool_use (+ merged result) ---
    if mtype == "tool_use":
        out = [_event(
            contract.ToolUse, session_id,
            tool_use_id=msg.get("toolId") or "",
            name=msg.get("toolName") or "",
            input=_as_input(msg.get("toolInput")),
            screenshot=msg.get("toolScreenshot"),
            timestamp=ts,
        )]
        tool_status = msg.get("toolStatus")
        tool_result = msg.get("toolResult")
        if tool_result is not None or tool_status in ("complete", "completed", "error"):
            out.append(_event(
                contract.ToolResult, session_id,
                tool_use_id=msg.get("toolId") or "",
                is_error=(tool_status == "error"),
                content_preview=_truncate(tool_result),
                timestamp=ts,
            ))
        return out

    # --- standalone tool_result ---
    if mtype == "tool_result":
        return [_event(
            contract.ToolResult, session_id,
            tool_use_id=msg.get("toolId") or "",
            is_error=(msg.get("toolStatus") == "error"),
            content_preview=_truncate(content or msg.get("toolResult")),
            timestamp=ts,
        )]

    # --- subagent ---
    if mtype == "subagent":
        return _convert_subagent(session_id, msg)

    # --- team member ---
    if mtype == "team_member":
        return _convert_team_member(session_id, msg)

    # --- system ---
    if mtype == "system":
        systype = msg.get("systemSubtype") or msg.get("subtype") or ""
        if is_hidden_system_subtype(systype):
            return []
        if systype in ("compact_boundary", "microcompact_boundary"):
            return [_event(
                contract.SystemCompactionBoundary, session_id,
                extra_payload={"subtype": systype, "data": msg.get("systemData")},
            )]
        data = dict(msg.get("systemData") or {})
        if content:
            data["content"] = content
        return [_event(
            contract.SystemNote, session_id,
            subtype=systype, data=data, timestamp=ts,
        )]

    # Unknown / unmapped — skip (forward-compatible).
    return []


def _convert_subagent(session_id: str, msg: Dict[str, Any]) -> List[Dict[str, Any]]:
    task_id = msg.get("toolId") or msg.get("agentId") or ""
    out = [_event(
        contract.SubagentStarted, session_id,
        task_id=task_id,
        agent_id=msg.get("agentId"),
        agent_type=msg.get("agentType"),
        description=msg.get("agentDescription"),
        prompt_preview=_truncate(msg.get("agentPrompt")),
    )]
    for child in msg.get("agentChildren") or []:
        if not isinstance(child, dict):
            continue
        kind = _child_kind(child.get("type"))
        out.append(_event(
            contract.SubagentProgress, session_id,
            task_id=task_id, kind=kind, payload=_child_payload(kind, child),
        ))
    out.append(_event(
        contract.SubagentCompleted, session_id,
        task_id=task_id,
        status=_terminal_status(msg.get("agentStatus")),
        result_preview=_truncate(msg.get("content")),
        usage=msg.get("agentUsage"),
    ))
    return out


def _convert_team_member(session_id: str, msg: Dict[str, Any]) -> List[Dict[str, Any]]:
    team_name = msg.get("teamName") or ""
    teammate_name = msg.get("teammateName") or ""
    out = [_event(
        contract.TeamMemberStarted, session_id,
        team_name=team_name, teammate_name=teammate_name,
        description=None, prompt=_truncate(msg.get("agentPrompt")),
    )]
    for child in msg.get("agentChildren") or []:
        if not isinstance(child, dict):
            continue
        kind = _child_kind(child.get("type"))
        out.append(_event(
            contract.TeamMemberProgress, session_id,
            team_name=team_name, teammate_name=teammate_name,
            kind=kind, payload=_child_payload(kind, child),
        ))
    out.append(_event(
        contract.TeamMemberCompleted, session_id,
        team_name=team_name, teammate_name=teammate_name,
        status=_terminal_status(msg.get("teammateStatus")),
        result_preview=_truncate(msg.get("content")),
        usage=msg.get("agentUsage"),
    ))
    return out
