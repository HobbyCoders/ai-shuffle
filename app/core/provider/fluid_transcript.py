"""Transcript / checkpoint facade for fluid-harness sessions.

Background
----------
With a database configured (every real deployment) a session's transcript
is NOT the legacy pair (CLI JSONL under ``~/.claude/projects`` + the
``session_store_entries`` mirror). It is fluid's parent-linked node DAG:
``fluid_sessions.head_node_id`` points at the leaf, and ``fluid_session_nodes``
holds ``(node_id, parent_id, ts, message)`` rows. ``sessions.sdk_session_id``
then carries the fluid session id (``ses_<hex12>``), not a CLI UUID.

Every app feature that reads "the transcript" (rewind checkpoint resolution,
fork, the ``/live`` reload path) was written against the legacy sources and
therefore sees an EMPTY transcript for a fluid session. This module is the
one place that knows how to read the fluid store for those features.

The central trick: fluid nodes are projected into **JSONL-shaped transcript
entries** — the same ``{"uuid", "type", "message": {"role", "content": [...]}}``
dicts the JSONL parser, the session-store parser, the rewind slicer
(:func:`app.core.jsonl_rewind.compute_rewind_keep_count`), the checkpoint
builder (:func:`app.core.jsonl_rewind.checkpoints_from_entries`) and the DB
anchor matcher (``rewind_db_sync``) already consume. Nothing about slicing,
checkpoint selection or message rendering is re-implemented for fluid; only the
*source* changes, so fluid sessions inherit the exact semantics the legacy path
has today.

Projection is 1:1 and order-preserving: ``entries[i]`` is always
``load_fluid_path()[i]``. Rewind depends on that (a keep-count over entries maps
straight back to the node that must become the new head).

Legacy-session safety: nothing here runs for a pre-fluid session. Every public entry
point is gated on :func:`looks_like_fluid_session_id` (a cheap ``ses_`` prefix
test that a CLI UUID can never satisfy) before any DB work, and the module is
imported lazily at each call site.

One thing here writes OUTSIDE the fluid store: the delegated-CLI re-seed (ruling
R5). The ``claude`` CLI keeps its own conversation and only reconnects to it by
uuid, so a head-move alone would leave it replaying the rewound-away turns —
the module therefore also materializes fluid's post-rewind path as a CLI session
file and repoints the session's ``--resume`` handle at it. See the "Delegated-CLI
re-seed" section.

Reads go straight to SQL rather than through
:class:`~app.core.provider.fluid_pg_store.PgFluidStore` on purpose: these callers
are synchronous request-path helpers, they must work when ``fluid`` itself is not
importable, and the store's per-instance memory cache would just be cold anyway.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
import time
import uuid as _uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from app.core.jsonl_rewind import (
    Checkpoint,
    RewindResult,
    checkpoints_from_entries,
    compute_rewind_keep_count,
)

logger = logging.getLogger(__name__)

# fluid's ``new_id("ses")`` (fluid.store.base) — the only id shape a session
# created by the harness can have. A Claude CLI session id is a UUID, so this
# prefix is an exact discriminator between the two transcript worlds.
FLUID_SESSION_ID_PREFIX = "ses_"


# ─── Session identity ────────────────────────────────────────────────────────

def looks_like_fluid_session_id(sdk_session_id: Optional[str]) -> bool:
    """Cheap, DB-free "could this be a fluid session?" test."""
    return bool(sdk_session_id) and str(sdk_session_id).startswith(
        FLUID_SESSION_ID_PREFIX
    )


def new_fluid_session_id() -> str:
    """Mint an id in fluid's own format (``fluid.store.base.new_id('ses')``)."""
    return f"{FLUID_SESSION_ID_PREFIX}{_uuid.uuid4().hex[:12]}"


def is_fluid_session(sdk_session_id: Optional[str]) -> bool:
    """True when *sdk_session_id* names a real row in ``fluid_sessions``.

    Prefix-gated so a legacy session never pays for a query, then CONFIRMED
    against the store. The confirmation matters: the id shape alone would make
    every caller treat a typo'd/deleted id as fluid and serve an empty
    transcript, whereas an unconfirmed id falls through to the legacy readers
    exactly as before — sessions recorded before the fluid cutover keep
    resolving through the JSONL/session-store path.
    """
    if not looks_like_fluid_session_id(sdk_session_id):
        return False
    try:
        return _session_row(str(sdk_session_id)) is not None
    except Exception as exc:  # a DB hiccup must not break the legacy fallback
        logger.debug("fluid session probe failed for %s: %s", sdk_session_id, exc)
        return False


# ─── Raw store reads ─────────────────────────────────────────────────────────

def _session_row(session_id: str) -> Optional[Dict[str, Any]]:
    from app.db.base import get_cursor, get_db
    from app.db.compat import ph

    with get_db() as conn:
        cursor = get_cursor(conn)
        cursor.execute(
            f"SELECT session_id, head_node_id, created_at FROM fluid_sessions "
            f"WHERE session_id = {ph()}",
            (session_id,),
        )
        row = cursor.fetchone()
    return dict(row) if row is not None else None


def _node_rows(session_id: str) -> List[Dict[str, Any]]:
    from app.db.base import get_cursor, get_db
    from app.db.compat import ph

    with get_db() as conn:
        cursor = get_cursor(conn)
        cursor.execute(
            f"SELECT node_id, parent_id, ts, message FROM fluid_session_nodes "
            f"WHERE session_id = {ph()}",
            (session_id,),
        )
        rows = cursor.fetchall()
    return [dict(r) for r in rows]


def _event_rows(session_id: str) -> List[Dict[str, Any]]:
    """The session's durable event log, in emission order.

    ``fluid_session_events`` is the only record of the tool calls a code-mode
    cell makes: ``CodeModeSession._host_call`` emits a ``ToolUse``/``ToolResult``
    pair per in-kernel call, and none of them reach the node tree (the model
    only ever emitted the enclosing ``run_python`` block).
    """
    from app.db.base import get_cursor, get_db
    from app.db.compat import ph

    with get_db() as conn:
        cursor = get_cursor(conn)
        cursor.execute(
            f"SELECT seq, event FROM fluid_session_events "
            f"WHERE session_id = {ph()} ORDER BY seq",
            (session_id,),
        )
        rows = cursor.fetchall()
    return [
        {"seq": dict(r).get("seq"), "event": _as_dict(dict(r).get("event"))}
        for r in rows
    ]


def _as_dict(value: Any) -> Dict[str, Any]:
    """``jsonb`` arrives as a dict from psycopg2 but as a JSON string from the
    test shim's SQLite — normalize (mirrors ``fluid_pg_store._as_dict``)."""
    if isinstance(value, dict):
        return value
    try:
        return json.loads(value)
    except Exception:
        return {}


def load_fluid_path(sdk_session_id: str) -> List[Dict[str, Any]]:
    """The session's on-path nodes, root-first (``head`` walked to the root).

    Mirrors ``SessionStore.load_path``: off-path nodes (siblings left behind by
    an earlier rewind/fork) are excluded — they are history, not transcript.
    Returns ``[]`` for an unknown session or a session whose head is NULL (a
    rewind back past the first prompt leaves exactly that).
    """
    meta = _session_row(sdk_session_id)
    if meta is None:
        return []
    nodes = {r["node_id"]: r for r in _node_rows(sdk_session_id)}
    path: List[Dict[str, Any]] = []
    seen: set = set()
    cursor = meta.get("head_node_id")
    while cursor is not None:
        node = nodes.get(cursor)
        if node is None or cursor in seen:  # dangling/looping parent — stop
            break
        seen.add(cursor)
        path.append(node)
        cursor = node.get("parent_id")
    path.reverse()
    return path


# ─── fluid node → JSONL-shaped transcript entry ───────────────────────────────

def _block_to_transcript(block: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """One fluid content block (``ContentBlock.to_dict``) → an Anthropic-shaped
    transcript block. ``Reasoning`` has no transcript analogue (it is opaque
    provider state, never rendered) and maps to None."""
    kind = block.get("kind")
    if kind == "text":
        return {"type": "text", "text": block.get("text") or ""}
    if kind == "thinking":
        return {"type": "thinking", "thinking": block.get("text") or ""}
    if kind == "tooluseblock":
        return {
            "type": "tool_use",
            "id": block.get("id") or "",
            "name": block.get("name") or "",
            "input": block.get("input") or {},
        }
    if kind == "toolresultblock":
        inner = block.get("content") or []
        text = "".join(
            b.get("text") or ""
            for b in inner
            if isinstance(b, dict) and b.get("kind") == "text"
        )
        return {
            "type": "tool_result",
            "tool_use_id": block.get("tool_use_id") or "",
            "content": text,
            "is_error": bool(block.get("is_error")),
        }
    if kind == "image":
        return {
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": block.get("media_type") or "",
                "data": block.get("data") or "",
            },
        }
    return None


def _iso(ts: Any) -> Optional[str]:
    try:
        return datetime.fromtimestamp(float(ts), tz=timezone.utc).isoformat()
    except Exception:
        return None


def _node_to_entry(node: Dict[str, Any], sdk_session_id: str) -> Dict[str, Any]:
    """Project one node row onto a transcript entry.

    ALWAYS returns an entry, even for an unrecognized role or a message whose
    blocks all dropped out: the entries list must stay index-aligned with the
    node path, or the rewind keep-count could not be mapped back to a node.
    Readers already skip entry types they don't understand.
    """
    message = _as_dict(node.get("message"))
    role = message.get("role") or "assistant"
    content = [
        projected
        for projected in (
            _block_to_transcript(b)
            for b in (message.get("content") or [])
            if isinstance(b, dict)
        )
        if projected is not None
    ]
    inner: Dict[str, Any] = {"role": role, "content": content}
    model = (message.get("provider_meta") or {}).get("model")
    if model:
        inner["model"] = model
    return {
        "uuid": node.get("node_id"),
        "parentUuid": node.get("parent_id"),
        "type": role,
        "timestamp": _iso(node.get("ts")),
        "sessionId": sdk_session_id,
        "message": inner,
    }


# ─── Code-mode inner calls ───────────────────────────────────────────────────
#
# In code mode the model emits ONE ``run_python`` tool_use per cell; the tools
# the cell calls (Grep, Read, Bash, Task…) are dispatched host-side by
# ``CodeModeSession._host_call``, which emits a ``ToolUse``/``ToolResult`` pair
# per call to the event bus. Those events are persisted to
# ``fluid_session_events`` and never appear in the node tree, so a transcript
# read from nodes alone shows a bare ``run_python`` card where the live chat
# showed the individual tool cards. The helpers below recover the inner calls
# from the event log and splice them back in for read-only renderers.

#: Code-mode's cell tool. Only blocks with this name get inner calls spliced.
_CODE_MODE_TOOL = "run_python"


def _inner_calls_by_block(
    events: Sequence[Dict[str, Any]], outer_ids: set
) -> Dict[str, List[Dict[str, Any]]]:
    """Group the event log's in-kernel tool calls by the block that ran them.

    ``outer_ids`` are the tool_use ids that DO appear in the node tree. Any
    ToolUse/ToolResult carrying an id the node tree does not know about was
    dispatched from inside one of those blocks — the question is which one.

    The engine emits every outer ``ToolUse`` while the assistant message is
    still STREAMING (``NativeLoop._stream_call``), before it has dispatched
    anything, and only then runs the blocks one at a time in order. So an
    assistant message with two tool_use blocks logs
    ``ToolUse(a), ToolUse(b), <a's inner calls>, ToolResult(a), <b's inner
    calls>, ToolResult(b)`` — tracking a single "current" outer id would hand
    every one of a's inner calls to b, because b's ToolUse was seen last.

    The walk therefore keeps an ordered list of OPEN outer ids (an outer
    ``ToolUse`` appends, its ``ToolResult`` removes it) and attributes an inner
    event to the OLDEST one still open, which is the block actually being
    dispatched. Inner events with nothing open are ignored — they belong to the
    engine, not to a cell.

    Returns ``{outer_tool_use_id: [{"id", "name", "input", "output",
    "is_error"}, ...]}`` in first-seen order. An inner ``ToolUse`` with no
    matching result still yields a record, with ``output`` None.
    """
    calls: Dict[str, List[Dict[str, Any]]] = {}
    records: Dict[str, Dict[str, Any]] = {}
    open_outer: List[str] = []

    for row in events:
        event = row.get("event") or {}
        cls = event.get("_cls")
        if cls not in ("ToolUse", "ToolResult"):
            continue
        tool_use_id = event.get("tool_use_id") or ""
        if tool_use_id in outer_ids:
            if cls == "ToolUse":
                if tool_use_id not in open_outer:
                    open_outer.append(tool_use_id)
            elif tool_use_id in open_outer:
                open_outer.remove(tool_use_id)
            continue
        if not open_outer:
            continue
        current = open_outer[0]
        if cls == "ToolUse":
            if tool_use_id in records:
                continue
            record = {
                "id": tool_use_id,
                "name": event.get("name") or "",
                "input": event.get("input") or {},
                "output": None,
                "is_error": False,
            }
            records[tool_use_id] = record
            calls.setdefault(current, []).append(record)
        else:
            record = records.get(tool_use_id)
            if record is None:
                continue
            record["output"] = event.get("output")
            record["is_error"] = bool(event.get("is_error"))

    return calls


def _tool_use_blocks(entry: Dict[str, Any]) -> List[Dict[str, Any]]:
    if entry.get("type") != "assistant":
        return []
    content = (entry.get("message") or {}).get("content") or []
    return [
        b for b in content
        if isinstance(b, dict) and b.get("type") == "tool_use" and b.get("id")
    ]


def _synthetic_entry(
    uuid_: str,
    parent_uuid: Optional[str],
    role: str,
    block: Dict[str, Any],
    host: Dict[str, Any],
    model: Optional[str],
) -> Dict[str, Any]:
    """One spliced entry, shaped exactly like :func:`_node_to_entry` output."""
    message: Dict[str, Any] = {"role": role, "content": [block]}
    if model:
        message["model"] = model
    return {
        "uuid": uuid_,
        "parentUuid": parent_uuid,
        "type": role,
        "timestamp": host.get("timestamp"),
        "sessionId": host.get("sessionId"),
        "message": message,
    }


def _splice_inner_calls(
    entries: List[Dict[str, Any]], sdk_session_id: str
) -> List[Dict[str, Any]]:
    """Insert each ``run_python`` block's in-kernel calls after its entry.

    Every inner call becomes the same tool_use/tool_result pair a JSON-surface
    call produces, so the shared transform merges it into an ordinary tool card.
    The pair goes between the ``run_python`` tool_use entry and the tool_result
    entry that closes it, which is the order the live feed showed.
    """
    outer_ids = {
        block["id"] for entry in entries for block in _tool_use_blocks(entry)
    }
    has_cells = any(
        block.get("name") == _CODE_MODE_TOOL
        for entry in entries
        for block in _tool_use_blocks(entry)
    )
    if not has_cells:  # nothing to splice — don't pay for the event query
        return entries

    calls = _inner_calls_by_block(_event_rows(sdk_session_id), outer_ids)
    if not calls:
        return entries

    out: List[Dict[str, Any]] = []
    for entry in entries:
        out.append(entry)
        model = (entry.get("message") or {}).get("model")
        parent_uuid = entry.get("uuid")
        for block in _tool_use_blocks(entry):
            if block.get("name") != _CODE_MODE_TOOL:
                continue
            for record in calls.get(block["id"], []):
                use_uuid = f"evt_{record['id']}"
                out.append(_synthetic_entry(
                    use_uuid, parent_uuid, "assistant",
                    {
                        "type": "tool_use",
                        "id": record["id"],
                        "name": record["name"],
                        "input": record["input"],
                    },
                    entry, model,
                ))
                parent_uuid = use_uuid
                if record["output"] is None:
                    continue
                result_uuid = f"{use_uuid}_result"
                out.append(_synthetic_entry(
                    result_uuid, parent_uuid, "user",
                    {
                        "type": "tool_result",
                        "tool_use_id": record["id"],
                        "content": str(record["output"]),
                        "is_error": bool(record["is_error"]),
                    },
                    entry, model,
                ))
                parent_uuid = result_uuid
    return out


def fluid_transcript_entries(
    sdk_session_id: str, include_inner_calls: bool = False
) -> List[Dict[str, Any]]:
    """The session's transcript as JSONL-shaped entries, root-first.

    This is the facade's core: hand the result to any existing entries-based
    reader (``checkpoints_from_entries``, ``compute_rewind_keep_count``,
    ``jsonl_parser._transform_entries``, ``rewind_db_sync``'s matchers) and it
    behaves exactly as it does for a legacy transcript.

    ``include_inner_calls`` adds the tool calls a code-mode cell made from
    inside the kernel, read from the event log and spliced in as ordinary
    tool_use/tool_result entries. It defaults to False because the default
    result MUST stay index-aligned with :func:`load_fluid_path` — rewind maps a
    keep-count over entries straight back to the node that becomes the new head,
    and fork and the checkpoint list depend on the same 1:1 mapping. Splicing
    breaks that alignment, so only read-only renderers (``/live``, the detail
    read, the export) may pass True.
    """
    if not looks_like_fluid_session_id(sdk_session_id):
        return []
    entries = [
        _node_to_entry(node, sdk_session_id)
        for node in load_fluid_path(sdk_session_id)
    ]
    if not include_inner_calls or not entries:
        return entries
    return _splice_inner_calls(entries, sdk_session_id)


# ─── Checkpoints ─────────────────────────────────────────────────────────────

def get_fluid_checkpoints(sdk_session_id: str) -> List[Checkpoint]:
    """Rewind checkpoints for a fluid session (uuid == fluid ``node_id``).

    Same population and ordering rules as
    :meth:`JSONLRewindService.get_checkpoints` — they share
    :func:`checkpoints_from_entries` — so tool-result-only user nodes are
    excluded and the occurrence ordinals the content guard computes line up
    with the ones the client sees.
    """
    return checkpoints_from_entries(fluid_transcript_entries(sdk_session_id))


# ─── Rewind ──────────────────────────────────────────────────────────────────

def _count_user_prompts(entries: List[Dict[str, Any]]) -> int:
    return len([cp for cp in checkpoints_from_entries(entries) if cp.role == "user"])


def rewind_fluid_session(
    sdk_session_id: str,
    target_uuid: str,
    include_response: bool = True,
    target_role: str = "user",
) -> RewindResult:
    """Rewind a fluid session by moving its head to an earlier node.

    This is fluid's native rewind (``fluid.events.ops.Rewind`` /
    ``NativeLoop``'s handler): nothing is deleted, the head simply moves back
    up the parent chain. Nodes after the new head stay in the tree but off-path,
    so ``load_path`` — and therefore the prompt the next turn builds — stops at
    the checkpoint, and that next turn appends a sibling branch.

    WHERE it cuts is decided by :func:`compute_rewind_keep_count`, the same
    function the JSONL truncation and the session-store truncation use, so a
    rewind lands on the same message under fluid as it does on the legacy path.
    """
    path = load_fluid_path(sdk_session_id)
    if not path:
        return RewindResult(
            success=False,
            message="Fluid rewind failed",
            error=f"No fluid transcript found for session {sdk_session_id}",
        )
    entries = [_node_to_entry(node, sdk_session_id) for node in path]

    keep_count, error = compute_rewind_keep_count(
        entries, target_uuid, include_response, target_role
    )
    if keep_count is None:
        return RewindResult(
            success=False, message="Fluid rewind failed", error=error
        )

    new_head = path[keep_count - 1]["node_id"] if keep_count > 0 else None
    _set_head(sdk_session_id, new_head)

    return RewindResult(
        success=True,
        message="Fluid session head moved",
        checkpoint_uuid=target_uuid,
        messages_removed=len(entries) - keep_count,
        remaining_user_turns=_count_user_prompts(entries[:keep_count]),
    )


def _set_head(session_id: str, node_id: Optional[str]) -> None:
    from app.db.base import get_cursor, get_db
    from app.db.compat import ph

    with get_db() as conn:
        cursor = get_cursor(conn)
        cursor.execute(
            f"UPDATE fluid_sessions SET head_node_id = {ph()} "
            f"WHERE session_id = {ph()}",
            (node_id, session_id),
        )


# ─── Delegated-CLI re-seed (ruling R5) ───────────────────────────────────────
#
# The rewind above moves fluid's head, which is the whole truth for a NATIVE
# session: fluid rebuilds the prompt from ``load_path`` on every turn, so the
# rewound-away turns are simply gone.
#
# The DELEGATED (claude CLI) engine is different. It does not receive the
# conversation — it receives only the new user turn and reconnects to its own
# history via ``--resume <cli_session_id>`` (see ``DelegatedLoop._build_argv``;
# ``EngineCaps.rewind = False``). So after a head-move the CLI happily replays
# the turns fluid just dropped. PR #815 surfaced that as a warning; ruling R5
# (docs/FLUID_AGENTS_PLAN.md) resolves it instead: **fluid owns the context**, so
# the rewind materializes fluid's post-rewind path as a CLI-format session file
# under a FRESH CLI uuid and points the session at it. The next turn's
# ``--resume`` then reconnects to a conversation that ends exactly where the
# user rewound to.
#
# The file format is the same one ``app.core.jsonl_rewind`` truncates in place
# on the legacy path: one JSON object per line under
# ``<projects>/<encoded cwd>/<cli uuid>.jsonl``, each carrying a
# ``uuid``/``parentUuid`` chain and the session's uuid in ``sessionId``. Fields
# the CLI writes for its own bookkeeping (``version``, ``gitBranch``,
# ``entrypoint``, ``promptId``, attachment/queue-operation lines, the trailing
# ``last-prompt`` record) are omitted — they are provenance, not conversation,
# and the CLI accepts a file without them (verified live against the bundled
# binary; see the PR body).


#: CLI transcript entry kinds we can materialize. Everything else fluid's
#: projection can produce (or a future projection might) is provenance, not
#: conversation, and is dropped rather than guessed at.
_RESEEDABLE_TYPES = ("user", "assistant")


@dataclass
class ReseedResult:
    """Outcome of :func:`reseed_delegated_cli_session`.

    ``status`` is one of:

    * ``"reseeded"`` — a synthetic transcript was written and
      ``fluid_sessions.cli_session_id`` now points at it.
    * ``"cleared"``  — the rewind emptied the transcript, so the stored handle
      was dropped; the next turn starts a fresh CLI conversation (which is
      exactly an empty context).
    * ``"skipped"``  — the session has no delegated CLI conversation to diverge
      from (native engine, or delegated but never spawned), so there is nothing
      to re-seed and nothing to warn about.
    * ``"failed"``   — the materialization or the store write failed; the CLI
      still holds the old history and the caller must keep the #815 warning.
    """

    status: str
    cli_session_id: Optional[str] = None
    entries_written: int = 0
    path: Optional[str] = None
    error: Optional[str] = None

    @property
    def context_is_authoritative(self) -> bool:
        """True when the CLI can no longer replay the rewound-away turns."""
        return self.status in ("reseeded", "cleared", "skipped")


def get_cli_session_id(sdk_session_id: str) -> Optional[str]:
    """The delegated CLI's ``--resume`` handle for this fluid session, if any.

    Non-NULL only for a session whose delegated engine actually spawned the CLI
    (``DelegatedLoop`` writes the uuid back off the ``init`` frame — PR #811),
    which makes it the precise discriminator for "this session has a CLI
    conversation that a rewind would leave stale".
    """
    from app.db.base import get_cursor, get_db
    from app.db.compat import ph

    with get_db() as conn:
        cursor = get_cursor(conn)
        cursor.execute(
            f"SELECT cli_session_id FROM fluid_sessions WHERE session_id = {ph()}",
            (sdk_session_id,),
        )
        row = cursor.fetchone()
    if row is None:
        return None
    return dict(row).get("cli_session_id")


def _set_cli_session_id(sdk_session_id: str, cli_session_id: Optional[str]) -> None:
    """Store the handle the next turn will ``--resume``.

    Plain SQL for the same reason every other write here is (module docstring):
    this runs on a synchronous request path that must work without ``fluid``
    importable. ``PgFluidStore``'s per-instance cache is not a hazard — the
    rewind endpoint evicts the pooled client before touching the transcript, so
    the next turn builds a fresh store that reads this row from the database.
    """
    from app.db.base import get_cursor, get_db
    from app.db.compat import ph

    with get_db() as conn:
        cursor = get_cursor(conn)
        cursor.execute(
            f"UPDATE fluid_sessions SET cli_session_id = {ph()} "
            f"WHERE session_id = {ph()}",
            (cli_session_id, sdk_session_id),
        )


def _cli_timestamp(value: Any) -> str:
    """Transcript timestamp in the CLI's own format (``...Z``, not ``+00:00``)."""
    if isinstance(value, str) and value:
        return value.replace("+00:00", "Z")
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _paired_tool_ids(entries: Sequence[Dict[str, Any]]) -> set:
    """Tool ids that have BOTH a ``tool_use`` and its ``tool_result`` on the path.

    A rewind cuts at a user-prompt boundary so pairs normally survive intact,
    but a half pair is fatal on replay (the API rejects a ``tool_use`` with no
    result, and a ``tool_result`` with no preceding use), and a projection gap
    must not be able to poison the whole re-seed. Unpaired blocks are dropped.
    """
    uses: set = set()
    results: set = set()
    for entry in entries:
        for block in (entry.get("message") or {}).get("content") or []:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "tool_use" and block.get("id"):
                uses.add(block["id"])
            elif block.get("type") == "tool_result" and block.get("tool_use_id"):
                results.add(block["tool_use_id"])
    return uses & results


def _reseed_blocks(
    content: Any, paired: set
) -> Optional[Any]:
    """Filter one entry's content down to blocks that can be safely replayed.

    Returns None when nothing survives (the entry itself is then dropped).

    * ``thinking`` blocks go: fluid's projection carries no ``signature`` and the
      API rejects an unsigned thinking block on replay. They are model scratch
      space, not conversation, so losing them costs the re-seeded context
      nothing.
    * unpaired ``tool_use`` / ``tool_result`` blocks go (see
      :func:`_paired_tool_ids`).
    * a user turn that is nothing but text collapses to a plain string, which is
      exactly how the CLI records a human prompt.
    """
    if isinstance(content, str):
        return content or None
    if not isinstance(content, list):
        return None

    kept: List[Dict[str, Any]] = []
    for block in content:
        if not isinstance(block, dict):
            continue
        kind = block.get("type")
        if kind == "thinking":
            continue
        if kind == "tool_use" and block.get("id") not in paired:
            continue
        if kind == "tool_result" and block.get("tool_use_id") not in paired:
            continue
        kept.append(block)
    if not kept:
        return None
    if all(b.get("type") == "text" for b in kept):
        text = "\n".join(b.get("text") or "" for b in kept).strip()
        return text or None
    return kept


def build_cli_transcript_entries(
    entries: Sequence[Dict[str, Any]],
    cli_session_id: str,
    working_dir: str,
) -> List[Dict[str, Any]]:
    """Project fluid transcript entries onto CLI session-file entries.

    Pure — no DB, no filesystem — so the shape is unit-testable on its own.

    Invariants the CLI depends on and the tests pin:

    * every entry carries a fresh ``uuid``; ``parentUuid`` is the previous
      kept entry's uuid and ``None`` on the first, so the file is one
      unbroken chain (fluid node ids are ``nod_...``, not uuids, and reusing
      them would also make two different conversations share message ids);
    * every entry's ``sessionId`` is *this* file's uuid — the CLI keys its
      resume on the filename and cross-checks the field;
    * the first entry is always a ``user`` turn (the API refuses a history that
      opens on the assistant).
    """
    paired = _paired_tool_ids(entries)
    out: List[Dict[str, Any]] = []
    parent_uuid: Optional[str] = None

    for entry in entries:
        if entry.get("type") not in _RESEEDABLE_TYPES:
            continue
        message = entry.get("message") or {}
        role = message.get("role")
        if role not in _RESEEDABLE_TYPES:
            continue
        content = _reseed_blocks(message.get("content"), paired)
        if content is None:
            continue
        # A history opening on an assistant turn is rejected upstream; that can
        # only happen if the leading user turn dropped out above.
        if not out and role != "user":
            continue

        entry_uuid = str(_uuid.uuid4())
        line: Dict[str, Any] = {
            "parentUuid": parent_uuid,
            "isSidechain": False,
            "userType": "external",
            "cwd": working_dir,
            "sessionId": cli_session_id,
            "type": role,
            "uuid": entry_uuid,
            "timestamp": _cli_timestamp(entry.get("timestamp")),
        }
        if role == "user":
            line["message"] = {"role": "user", "content": content}
        else:
            line["message"] = {
                "id": f"msg_{_uuid.uuid4().hex[:24]}",
                "type": "message",
                "role": "assistant",
                "model": message.get("model") or "<synthetic>",
                "content": content if isinstance(content, list) else [
                    {"type": "text", "text": content}
                ],
                "stop_reason": None,
                "stop_sequence": None,
                "usage": {
                    "input_tokens": 0,
                    "output_tokens": 0,
                    "cache_creation_input_tokens": 0,
                    "cache_read_input_tokens": 0,
                },
            }
        out.append(line)
        parent_uuid = entry_uuid

    return out


def _write_cli_transcript(
    lines: Sequence[Dict[str, Any]],
    cli_session_id: str,
    working_dir: str,
    projects_dir: Optional[Path] = None,
) -> Path:
    """Write the materialized transcript to ``<projects>/<cwd>/<uuid>.jsonl``.

    Atomic (temp file + rename in the same directory), matching how
    ``JSONLRewindService.truncate_to_checkpoint`` rewrites a transcript: a
    half-written session file is one the CLI would refuse to resume.
    """
    from app.core.config import settings
    from app.core.jsonl_parser import get_project_dir_name

    root = projects_dir or settings.get_claude_projects_dir
    project_dir = Path(root) / get_project_dir_name(working_dir)
    project_dir.mkdir(parents=True, exist_ok=True)
    target = project_dir / f"{cli_session_id}.jsonl"

    temp_fd, temp_path = tempfile.mkstemp(
        dir=project_dir, prefix=".reseed_", suffix=".jsonl"
    )
    try:
        with os.fdopen(temp_fd, "w", encoding="utf-8") as handle:
            for line in lines:
                handle.write(json.dumps(line) + "\n")
        os.replace(temp_path, target)
    except Exception:
        if os.path.exists(temp_path):
            os.unlink(temp_path)
        raise
    return target


def reseed_delegated_cli_session(
    sdk_session_id: str,
    working_dir: str,
    projects_dir: Optional[Path] = None,
    *,
    force: bool = False,
) -> ReseedResult:
    """Re-seed the delegated CLI from fluid's (already rewound) context.

    Call AFTER :func:`rewind_fluid_session` has moved the head — the transcript
    this materializes is whatever ``load_path`` returns now.

    ``force=True`` writes even when this session has no stored CLI handle.
    Fork uses that: the new fluid session is empty of a handle, but the parent
    was delegated, so the fork must mint its own CLI conversation from the
    copied path or the next turn starts with empty model context.

    Never raises: a failed re-seed is a degraded rewind (the store is still
    correct, only the CLI's own history is stale), and the caller reports that
    to the user rather than failing an operation the user explicitly asked for.
    """
    if not looks_like_fluid_session_id(sdk_session_id):
        return ReseedResult(status="skipped")

    try:
        previous = get_cli_session_id(sdk_session_id)
    except Exception as exc:
        logger.error(
            "fluid re-seed could not read the CLI handle for %s: %s",
            sdk_session_id, exc, exc_info=True,
        )
        return ReseedResult(status="failed", error=str(exc))

    if not previous and not force:
        # Native engine, or a delegated session whose CLI never spawned: there
        # is no CLI-side history, so nothing can diverge.
        return ReseedResult(status="skipped")

    try:
        entries = fluid_transcript_entries(sdk_session_id)
        new_cli_session_id = str(_uuid.uuid4())
        lines = build_cli_transcript_entries(
            entries, new_cli_session_id, working_dir
        )
    except Exception as exc:
        logger.error(
            "fluid re-seed could not materialize a CLI transcript for %s: %s",
            sdk_session_id, exc, exc_info=True,
        )
        return ReseedResult(status="failed", error=str(exc))

    if not lines:
        # Rewound past the first prompt. A fresh CLI conversation IS the empty
        # context, so drop the handle rather than writing an empty file the CLI
        # would refuse to resume.
        try:
            _set_cli_session_id(sdk_session_id, None)
        except Exception as exc:
            logger.error(
                "fluid re-seed could not clear the CLI handle for %s: %s",
                sdk_session_id, exc, exc_info=True,
            )
            return ReseedResult(status="failed", error=str(exc))
        return ReseedResult(status="cleared")

    try:
        path = _write_cli_transcript(
            lines, new_cli_session_id, working_dir, projects_dir
        )
    except Exception as exc:
        logger.error(
            "fluid re-seed could not write the CLI transcript for %s: %s",
            sdk_session_id, exc, exc_info=True,
        )
        return ReseedResult(status="failed", error=str(exc))

    try:
        _set_cli_session_id(sdk_session_id, new_cli_session_id)
    except Exception as exc:
        logger.error(
            "fluid re-seed wrote %s but could not store the handle for %s: %s",
            path, sdk_session_id, exc, exc_info=True,
        )
        return ReseedResult(status="failed", error=str(exc))

    logger.info(
        "fluid re-seed: session=%s cli_session=%s -> %s (%d entries) at %s",
        sdk_session_id, previous, new_cli_session_id, len(lines), path,
    )
    return ReseedResult(
        status="reseeded",
        cli_session_id=new_cli_session_id,
        entries_written=len(lines),
        path=str(path),
    )


# ─── Fork ────────────────────────────────────────────────────────────────────

def _compaction_node_ids(event: Dict[str, Any]) -> set:
    """Node ids a ContextCompaction promotion names (trim pairs + span bounds)."""
    refs: set = set()
    for pair in event.get("trimmed") or []:
        if isinstance(pair, (list, tuple)) and pair:
            refs.add(pair[0])
    for span in event.get("spans") or []:
        if isinstance(span, (list, tuple)) and span:
            refs.add(span[0])
            if len(span) > 1:
                refs.add(span[1])
    return refs


def _events_for_prefix(
    events: Sequence[Dict[str, Any]],
    prefix_ids: set,
    *,
    at_head: bool,
) -> List[Dict[str, Any]]:
    """Events the fork must carry so the next turn rebuilds the same view.

    ContextCompaction promotions live only in the event log; without them the
    fork re-sends the uncompacted tree and the model sees a different context.
    Code-mode inner ToolUse/ToolResult pairs are also event-only.

    A whole-session fork (at the current head) copies every event. A mid-session
    fork keeps events that belong to the prefix path — plus ContextCompaction
    whose named nodes all sit on that prefix — and drops ones tagged with a
    later node.
    """
    if at_head:
        return list(events)

    kept: List[Dict[str, Any]] = []
    include_untagged = True
    for row in events:
        event = _as_dict(row.get("event"))
        cls = event.get("_cls")
        nid = event.get("node_id")
        if cls == "ContextCompaction":
            refs = _compaction_node_ids(event)
            if not refs or refs <= prefix_ids:
                kept.append(row)
            continue
        if nid:
            if nid in prefix_ids:
                include_untagged = True
                kept.append(row)
            else:
                include_untagged = False
        elif include_untagged:
            kept.append(row)
    return kept


def fork_fluid_session(
    source_sdk_session_id: str,
    fork_node_id: str,
    new_sdk_session_id: Optional[str] = None,
    *,
    working_dir: Optional[str] = None,
    projects_dir: Optional[Path] = None,
) -> Tuple[str, int]:
    """Create a new fluid session whose transcript is the source's ancestry up
    to (and including) ``fork_node_id``. Returns ``(new_session_id, node_count)``.

    fluid's in-engine ``Fork`` op only moves the head *within one session* —
    it exists so a single conversation can branch. The app's fork is a
    different product concept: a SECOND session row the user can drive
    independently. Since ``fluid_session_nodes`` is keyed by
    ``(session_id, node_id)``, two sessions cannot literally share rows, so the
    ancestry prefix is copied — but every ``node_id``/``parent_id``/``ts``/
    ``message`` is preserved verbatim, so the forked prefix is byte-identical
    to the original's (which is what keeps provider prompt caching warm) and
    node handles stay comparable across the two sessions.

    The durable event log is copied too (rewritten onto the new session id,
    seqs renumbered 1..N so the EventBus seed stays contiguous). Compaction
    promotions and code-mode inner calls live only there; dropping them made
    the fork's next prompt a different context from the parent's.

    When ``working_dir`` is set and the source has a delegated CLI handle, the
    copied path is also materialized as a fresh CLI transcript (ruling R5)
    so the next ``--resume`` does not start empty.

    Raises ``ValueError`` when the fork point is not on the source's path.
    """
    path = load_fluid_path(source_sdk_session_id)
    index = next(
        (i for i, node in enumerate(path) if node["node_id"] == fork_node_id), None
    )
    if index is None:
        raise ValueError(
            f"fork node {fork_node_id} is not on session {source_sdk_session_id}'s path"
        )
    prefix = path[: index + 1]
    new_id = new_sdk_session_id or new_fluid_session_id()
    prefix_ids = {node["node_id"] for node in prefix}
    at_head = index == len(path) - 1
    kept_events = _events_for_prefix(
        _event_rows(source_sdk_session_id), prefix_ids, at_head=at_head
    )
    source_had_cli = bool(get_cli_session_id(source_sdk_session_id))

    from psycopg2.extras import Json

    from app.db.base import get_cursor, get_db
    from app.db.compat import ph

    _ph = ph()
    with get_db() as conn:
        cursor = get_cursor(conn)
        cursor.execute(
            f"INSERT INTO fluid_sessions (session_id, head_node_id, created_at) "
            f"VALUES ({_ph}, {_ph}, {_ph}) ON CONFLICT (session_id) DO NOTHING",
            (new_id, fork_node_id, time.time()),
        )
        for node in prefix:
            cursor.execute(
                f"INSERT INTO fluid_session_nodes "
                f"(session_id, node_id, parent_id, ts, message) "
                f"VALUES ({_ph}, {_ph}, {_ph}, {_ph}, {_ph}) "
                f"ON CONFLICT (session_id, node_id) DO NOTHING",
                (
                    new_id,
                    node["node_id"],
                    node.get("parent_id"),
                    node.get("ts"),
                    # Same jsonb adaptation PgFluidStore uses on the write path.
                    Json(_as_dict(node.get("message"))),
                ),
            )
        for seq, row in enumerate(kept_events, start=1):
            event = json.loads(json.dumps(_as_dict(row.get("event"))))
            event["session_id"] = new_id
            event["seq"] = seq
            cursor.execute(
                f"INSERT INTO fluid_session_events (session_id, seq, event) "
                f"VALUES ({_ph}, {_ph}, {_ph}) "
                f"ON CONFLICT (session_id, seq) DO NOTHING",
                (new_id, seq, Json(event)),
            )

    # Best-effort: the node/event copy is the source of truth. A failed CLI
    # re-seed leaves the fork driveable on the native path; the next delegated
    # turn would just start a fresh CLI conversation (same degraded case as a
    # failed rewind re-seed).
    if working_dir and source_had_cli:
        reseed = reseed_delegated_cli_session(
            new_id, working_dir, projects_dir, force=True
        )
        if reseed.status == "failed":
            logger.warning(
                "fork CLI re-seed failed for %s (parent %s): %s",
                new_id, source_sdk_session_id, reseed.error,
            )

    return new_id, len(prefix)


# ─── Live transcript rendering ───────────────────────────────────────────────

def fluid_live_messages(
    sdk_session_id: str, working_dir: str = "/workspace"
) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """``(messages, usage_data)`` for a fluid session, in the shape the
    session read paths expect.

    Runs the projected entries through ``jsonl_parser._transform_entries`` —
    the pure, source-agnostic core already shared by the JSONL parser and the
    Postgres session-store parser — so a fluid transcript renders through the
    identical transform (tool cards, thinking blocks, subagent rows, usage
    accounting) as every other source. Subagent children are not resolvable
    from the node tree yet, so no children loader is supplied.

    Code-mode inner calls are included: this is a rendering path, so it wants
    the cards the live feed showed rather than the node-aligned entries rewind
    needs.
    """
    entries = fluid_transcript_entries(sdk_session_id, include_inner_calls=True)
    if not entries:
        return [], {}
    from app.core.jsonl_parser import _transform_entries

    return _transform_entries(entries, sdk_session_id, working_dir)
