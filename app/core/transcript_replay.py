"""Pure canonical replay helpers shared by SSE, recording and shadow preparation.

No application, settings, database or provider imports: the optional private
CPU child must not reconstruct the serving application to fold a transcript.
"""
from __future__ import annotations

import os
from typing import Any, Dict, Iterable, Iterator, List, Optional, Sequence, Tuple

from app.core.harness_reminders import strip_harness_reminders

# A context-usage snapshot is 20-45 KB, mostly breakdown detail (gridRows,
# mcpTools, skills, memoryFiles, ...) that only the context panel's on-demand
# fetch (GET /sessions/{id}/context-usage) shows. The bar a turn.usage feeds
# reads the totals and the category rows, so a turn.usage carries just those.
_COMPACT_CONTEXT_KEYS = (
    "categories", "totalTokens", "maxTokens", "rawMaxTokens", "percentage",
    "model", "measured", "isAutoCompactEnabled",
)
_COMPACT_CATEGORY_KEYS = ("name", "tokens", "color", "isDeferred")


def compact_context_usage(snapshot: Any) -> Any:
    """The part of a context-usage snapshot the usage bar reads.

    Anything that is not a dict is returned unchanged.
    """
    if not isinstance(snapshot, dict):
        return snapshot
    compact = {k: snapshot[k] for k in _COMPACT_CONTEXT_KEYS if k in snapshot}
    categories = compact.get("categories")
    if isinstance(categories, list):
        compact["categories"] = [
            {k: c[k] for k in _COMPACT_CATEGORY_KEYS if k in c}
            if isinstance(c, dict) else c
            for c in categories
        ]
    return compact


def slim_turn_usage_payload(payload: Dict[str, Any]) -> Dict[str, Any]:
    """A turn.usage payload with one compact snapshot.

    Rows written before the snapshot was compacted carry it twice: in full at
    the top level and again inside ``usage``. The nested copy is dropped (no
    reader uses it) and the top-level one compacted. Returns a new dict; the
    argument is not changed.
    """
    usage = payload.get("usage")
    slim = dict(payload)
    if isinstance(usage, dict) and "context_usage" in usage:
        slim["usage"] = {k: v for k, v in usage.items() if k != "context_usage"}
    if isinstance(payload.get("context_usage"), dict):
        slim["context_usage"] = compact_context_usage(payload["context_usage"])
    return slim


_COALESCE_TYPES = ("assistant.thinking_delta", "assistant.text_delta")

# Tool-argument deltas get the same treatment during backfill, one step
# further: a block that later got its ``tool.use`` row has its whole input
# there, so its deltas are dropped. Deltas with no later ``tool.use`` (a
# background subagent launch, an interrupted block) are the only record of
# that input and replay as one merged frame per run.
_TOOL_INPUT_DELTA = "tool.input_delta"

# Backfill frames reach the ASGI stack in chunks of about this size rather than
# one per frame: every chunk crosses each BaseHTTPMiddleware layer, and that
# per-frame cost dominated long replays.
_BACKFILL_CHUNK_BYTES = 64 * 1024

# Subagent text streams the same way: one subagent.progress row per text delta
# of the subagent, which the client only appends to the card's trailing text
# child. A run of consecutive chunk-kind rows for one task replays as one frame.
# Only CONSECUTIVE rows merge: a frame's seq is the client's resume cursor, so
# every row at or below it must already be in that frame or an earlier one.
_SUBAGENT_PROGRESS = "subagent.progress"
_PROGRESS_STEP_KINDS = ("tool_use", "tool_result")

# Server-side kill switches for the replay slimming above. On unless set to 0;
# read per call so a worker can be switched without a code change.
_SLIM_USAGE_ENV = "AI_SHUFFLE_PERF_SLIM_USAGE_REPLAY"
_COALESCE_PROGRESS_ENV = "AI_SHUFFLE_PERF_COALESCE_SUBAGENT_PROGRESS"


def _perf_switch_on(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() not in ("0", "false", "off")


def _delta_key(row: Dict[str, Any]) -> tuple:
    """What makes two delta rows part of the same run."""
    key = (row.get("event_type"), row.get("turn_id"), row.get("run_id"))
    if row.get("event_type") == _TOOL_INPUT_DELTA:
        return key + ((row.get("payload") or {}).get("tool_use_id"),)
    return key


def _delta_field(row: Dict[str, Any]) -> str:
    """The payload field a delta row streams."""
    return "partial_json" if row.get("event_type") == _TOOL_INPUT_DELTA else "text"


def _merged_row(run: Dict[str, Any]) -> Dict[str, Any]:
    """The run's single frame row: first envelope, joined text, LAST seq.

    The seq must be the last row's so the client's reconnect cursor (the SSE
    ``id:`` line) does not rewind into the middle of a merged run.
    """
    first = run["first"]
    row = dict(first)
    payload = dict(first.get("payload") or {})
    payload[run["field"]] = "".join(run["texts"])
    if run.get("name") and not payload.get("name"):
        # A tool block's name can arrive after its first fragment.
        payload["name"] = run["name"]
    row["payload"] = payload
    row["seq"] = run["last_seq"]
    return row


def _open_run(row: Dict[str, Any]) -> Dict[str, Any]:
    field = _delta_field(row)
    payload = row.get("payload") or {}
    run = {
        "key": _delta_key(row),
        "field": field,
        "first": row,
        "texts": [payload.get(field) or ""],
        "last_seq": row["seq"],
    }
    if row.get("event_type") == _TOOL_INPUT_DELTA:
        run["name"] = payload.get("name")
    return run


def _superseded_tool_input(
    row: Dict[str, Any], tool_uses: Optional[Dict[Tuple[Any, str], int]]
) -> bool:
    """True for a tool.input_delta whose full input a LATER tool.use repeats."""
    if not tool_uses:
        return False
    tool_use_id = (row.get("payload") or {}).get("tool_use_id")
    if not tool_use_id:
        return False
    return row["seq"] < tool_uses.get((row.get("turn_id"), str(tool_use_id)), 0)


def _coalesce_deltas(
    rows: Sequence[Dict[str, Any]],
    pending: List[Optional[Dict[str, Any]]],
    tool_uses: Optional[Dict[Tuple[Any, str], int]] = None,
) -> Iterator[Dict[str, Any]]:
    """Yield frame-ready rows, merging consecutive streaming deltas.

    ``pending`` is a one-slot list holding the run still open from an earlier
    page (``[None]`` to start), so a run split across a page boundary still
    merges; the caller drains what is left with :func:`_flush_pending` once
    paging ends. Any non-delta row closes the open run before it is emitted.

    ``tool_uses`` maps ``(turn_id, tool_use_id)`` to the seq of that block's
    ``tool.use`` row (``get_tool_use_seqs``). A tool.input_delta before it is
    dropped. It still closes the open run, so the rows on either side merge
    exactly as they did when the delta was sent.
    """
    for row in rows:
        run = pending[0]
        event_type = row.get("event_type")
        if event_type == _TOOL_INPUT_DELTA and _superseded_tool_input(row, tool_uses):
            if run is not None:
                yield _merged_row(run)
                pending[0] = None
            continue
        if event_type in _COALESCE_TYPES or event_type == _TOOL_INPUT_DELTA:
            if run is not None and run["key"] == _delta_key(row):
                payload = row.get("payload") or {}
                run["texts"].append(payload.get(run["field"]) or "")
                if "name" in run and not run["name"]:
                    run["name"] = payload.get("name")
                run["last_seq"] = row["seq"]
                continue
            if run is not None:
                yield _merged_row(run)
            pending[0] = _open_run(row)
            continue
        if run is not None:
            yield _merged_row(run)
            pending[0] = None
        yield row


def _flush_pending(
    pending: List[Optional[Dict[str, Any]]],
) -> Iterator[Dict[str, Any]]:
    """Emit the run still open at the end of the backfill, if any."""
    run = pending[0]
    if run is not None:
        pending[0] = None
        yield _merged_row(run)


def _progress_text(row: Dict[str, Any]) -> Optional[str]:
    """The text a chunk-kind subagent.progress row adds, or None for any other row.

    Mirrors the client reducer: ``note``, else ``payload.text``, else
    ``payload.content``, counted only when it is a string.
    """
    if row.get("event_type") != _SUBAGENT_PROGRESS:
        return None
    payload = row.get("payload") or {}
    if payload.get("kind") in _PROGRESS_STEP_KINDS:
        return None
    value = payload.get("note")
    if value is None:
        inner = payload.get("payload")
        inner = inner if isinstance(inner, dict) else {}
        value = inner.get("text")
        if value is None:
            value = inner.get("content")
    return value if isinstance(value, str) else ""


def _merged_progress_row(run: Dict[str, Any]) -> Dict[str, Any]:
    """One frame for a run of chunk rows: first texted envelope, joined text, LAST seq."""
    first = run["first"]
    if run["count"] == 1:
        return first
    row = dict(first)
    payload = dict(first.get("payload") or {})
    inner = payload.get("payload")
    inner = {k: v for k, v in inner.items() if k != "text"} if isinstance(inner, dict) else {}
    inner["content"] = "".join(run["texts"])
    payload["payload"] = inner
    payload["note"] = None
    row["payload"] = payload
    row["seq"] = run["last_seq"]
    return row


def _coalesce_progress(
    rows: Iterable[Dict[str, Any]],
    pending: List[Optional[Dict[str, Any]]],
) -> Iterator[Dict[str, Any]]:
    """Merge consecutive chunk-kind subagent.progress rows for the same task.

    ``pending`` is a one-slot list holding the run still open from an earlier
    page, drained with :func:`_flush_pending_progress` once paging ends. The
    merged row keeps the envelope of the run's first row that adds text (the
    client names a new text child after it) and the last row's seq.
    """
    for row in rows:
        run = pending[0]
        text = _progress_text(row)
        if text is None:
            if run is not None:
                pending[0] = None
                yield _merged_progress_row(run)
            yield row
            continue
        task_id = (row.get("payload") or {}).get("task_id")
        if run is not None and run["task_id"] == task_id:
            if text and not run["texted"]:
                run["first"], run["texted"] = row, True
            run["texts"].append(text)
            run["count"] += 1
            run["last_seq"] = row["seq"]
            continue
        if run is not None:
            yield _merged_progress_row(run)
        pending[0] = {
            "task_id": task_id,
            "first": row,
            "texted": bool(text),
            "texts": [text],
            "count": 1,
            "last_seq": row["seq"],
        }


def _flush_pending_progress(
    pending: List[Optional[Dict[str, Any]]],
) -> Iterator[Dict[str, Any]]:
    run = pending[0]
    if run is not None:
        pending[0] = None
        yield _merged_progress_row(run)


def _wire(row: Dict[str, Any]) -> Dict[str, Any]:
    """Merge a DB row's payload with its envelope (envelope keys win)."""
    payload = row.get("payload") or {}
    if row.get("event_type") == "turn.usage" and _perf_switch_on(_SLIM_USAGE_ENV):
        # Old rows (and rows mirrored from older workers) carry the full
        # context snapshot twice; send one compact copy.
        payload = slim_turn_usage_payload(payload)
    if row.get("event_type") == "message.user" and isinstance(payload.get("content"), str):
        # Rows backfilled from a transcript can carry harness reminders.
        content = strip_harness_reminders(payload["content"])
        if content is not payload["content"]:
            payload = {**payload, "content": content}
    created_at = row.get("created_at")
    if hasattr(created_at, "isoformat"):
        created_at = created_at.isoformat()
    return {
        **payload,
        "id": row.get("id"),
        "session_id": row.get("session_id"),
        "seq": row.get("seq"),
        "run_id": row.get("run_id"),
        "turn_id": row.get("turn_id"),
        "command_id": row.get("command_id"),
        "type": row.get("event_type"),
        "created_at": created_at,
    }
