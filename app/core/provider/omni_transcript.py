"""Flag-aware transcript / checkpoint facade for omni-harness sessions.

Background
----------
With ``AIH_USE_OMNI_HARNESS=1`` + ``AIH_OMNI_STORE=pg`` a session's transcript
is NOT the legacy pair (CLI JSONL under ``~/.claude/projects`` + the
``session_store_entries`` mirror). It is omni's parent-linked node DAG:
``omni_sessions.head_node_id`` points at the leaf, and ``omni_session_nodes``
holds ``(node_id, parent_id, ts, message)`` rows. ``sessions.sdk_session_id``
then carries the omni session id (``ses_<hex12>``), not a CLI UUID.

Every app feature that reads "the transcript" (rewind checkpoint resolution,
fork, the ``/live`` reload path) was written against the legacy sources and
therefore sees an EMPTY transcript for a flag-on session. This module is the
one place that knows how to read the omni store for those features.

The central trick: omni nodes are projected into **JSONL-shaped transcript
entries** — the same ``{"uuid", "type", "message": {"role", "content": [...]}}``
dicts the JSONL parser, the session-store parser, the rewind slicer
(:func:`app.core.jsonl_rewind.compute_rewind_keep_count`), the checkpoint
builder (:func:`app.core.jsonl_rewind.checkpoints_from_entries`) and the DB
anchor matcher (``rewind_db_sync``) already consume. Nothing about slicing,
checkpoint selection or message rendering is re-implemented for omni; only the
*source* changes, so omni sessions inherit the exact semantics the legacy path
has today.

Projection is 1:1 and order-preserving: ``entries[i]`` is always
``load_omni_path()[i]``. Rewind depends on that (a keep-count over entries maps
straight back to the node that must become the new head).

Flag-off safety: nothing here runs for a legacy session. Every public entry
point is gated on :func:`looks_like_omni_session_id` (a cheap ``ses_`` prefix
test that a CLI UUID can never satisfy) before any DB work, and the module is
imported lazily at each call site.

Reads go straight to SQL rather than through
:class:`~app.core.provider.omni_pg_store.PgOmniStore` on purpose: these callers
are synchronous request-path helpers, they must work when ``omni`` itself is not
importable, and the store's per-instance memory cache would just be cold anyway.
"""

from __future__ import annotations

import json
import logging
import time
import uuid as _uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from app.core.jsonl_rewind import (
    Checkpoint,
    RewindResult,
    checkpoints_from_entries,
    compute_rewind_keep_count,
)

logger = logging.getLogger(__name__)

# omni's ``new_id("ses")`` (omni.store.base) — the only id shape a session
# created by the harness can have. A Claude CLI session id is a UUID, so this
# prefix is an exact discriminator between the two transcript worlds.
OMNI_SESSION_ID_PREFIX = "ses_"


# ─── Session identity ────────────────────────────────────────────────────────

def looks_like_omni_session_id(sdk_session_id: Optional[str]) -> bool:
    """Cheap, DB-free "could this be an omni session?" test."""
    return bool(sdk_session_id) and str(sdk_session_id).startswith(
        OMNI_SESSION_ID_PREFIX
    )


def new_omni_session_id() -> str:
    """Mint an id in omni's own format (``omni.store.base.new_id('ses')``)."""
    return f"{OMNI_SESSION_ID_PREFIX}{_uuid.uuid4().hex[:12]}"


def is_omni_session(sdk_session_id: Optional[str]) -> bool:
    """True when *sdk_session_id* names a real row in ``omni_sessions``.

    Prefix-gated so a legacy session never pays for a query, then CONFIRMED
    against the store. The confirmation matters: the id shape alone would make
    every caller treat a typo'd/deleted id as omni and serve an empty
    transcript, whereas an unconfirmed id falls through to the legacy readers
    exactly as before. Deliberately NOT gated on ``AIH_USE_OMNI_HARNESS`` —
    a session recorded while the flag was on stays an omni session after the
    flag is turned off, and its transcript must keep resolving.
    """
    if not looks_like_omni_session_id(sdk_session_id):
        return False
    try:
        return _session_row(str(sdk_session_id)) is not None
    except Exception as exc:  # a DB hiccup must not break the legacy fallback
        logger.debug("omni session probe failed for %s: %s", sdk_session_id, exc)
        return False


# ─── Raw store reads ─────────────────────────────────────────────────────────

def _session_row(session_id: str) -> Optional[Dict[str, Any]]:
    from app.db.base import get_cursor, get_db
    from app.db.compat import ph

    with get_db() as conn:
        cursor = get_cursor(conn)
        cursor.execute(
            f"SELECT session_id, head_node_id, created_at FROM omni_sessions "
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
            f"SELECT node_id, parent_id, ts, message FROM omni_session_nodes "
            f"WHERE session_id = {ph()}",
            (session_id,),
        )
        rows = cursor.fetchall()
    return [dict(r) for r in rows]


def _as_dict(value: Any) -> Dict[str, Any]:
    """``jsonb`` arrives as a dict from psycopg2 but as a JSON string from the
    test shim's SQLite — normalize (mirrors ``omni_pg_store._as_dict``)."""
    if isinstance(value, dict):
        return value
    try:
        return json.loads(value)
    except Exception:
        return {}


def load_omni_path(sdk_session_id: str) -> List[Dict[str, Any]]:
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


# ─── omni node → JSONL-shaped transcript entry ───────────────────────────────

def _block_to_transcript(block: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """One omni content block (``ContentBlock.to_dict``) → an Anthropic-shaped
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


def omni_transcript_entries(sdk_session_id: str) -> List[Dict[str, Any]]:
    """The session's transcript as JSONL-shaped entries, root-first.

    This is the facade's core: hand the result to any existing entries-based
    reader (``checkpoints_from_entries``, ``compute_rewind_keep_count``,
    ``jsonl_parser._transform_entries``, ``rewind_db_sync``'s matchers) and it
    behaves exactly as it does for a legacy transcript.
    """
    if not looks_like_omni_session_id(sdk_session_id):
        return []
    return [
        _node_to_entry(node, sdk_session_id)
        for node in load_omni_path(sdk_session_id)
    ]


# ─── Checkpoints ─────────────────────────────────────────────────────────────

def get_omni_checkpoints(sdk_session_id: str) -> List[Checkpoint]:
    """Rewind checkpoints for an omni session (uuid == omni ``node_id``).

    Same population and ordering rules as
    :meth:`JSONLRewindService.get_checkpoints` — they share
    :func:`checkpoints_from_entries` — so tool-result-only user nodes are
    excluded and the occurrence ordinals the content guard computes line up
    with the ones the client sees.
    """
    return checkpoints_from_entries(omni_transcript_entries(sdk_session_id))


# ─── Rewind ──────────────────────────────────────────────────────────────────

def _count_user_prompts(entries: List[Dict[str, Any]]) -> int:
    return len([cp for cp in checkpoints_from_entries(entries) if cp.role == "user"])


def rewind_omni_session(
    sdk_session_id: str,
    target_uuid: str,
    include_response: bool = True,
    target_role: str = "user",
) -> RewindResult:
    """Rewind an omni session by moving its head to an earlier node.

    This is omni's native rewind (``omni.events.ops.Rewind`` /
    ``NativeLoop``'s handler): nothing is deleted, the head simply moves back
    up the parent chain. Nodes after the new head stay in the tree but off-path,
    so ``load_path`` — and therefore the prompt the next turn builds — stops at
    the checkpoint, and that next turn appends a sibling branch.

    WHERE it cuts is decided by :func:`compute_rewind_keep_count`, the same
    function the JSONL truncation and the session-store truncation use, so a
    rewind lands on the same message under omni as it does on the legacy path.
    """
    path = load_omni_path(sdk_session_id)
    if not path:
        return RewindResult(
            success=False,
            message="Omni rewind failed",
            error=f"No omni transcript found for session {sdk_session_id}",
        )
    entries = [_node_to_entry(node, sdk_session_id) for node in path]

    keep_count, error = compute_rewind_keep_count(
        entries, target_uuid, include_response, target_role
    )
    if keep_count is None:
        return RewindResult(
            success=False, message="Omni rewind failed", error=error
        )

    new_head = path[keep_count - 1]["node_id"] if keep_count > 0 else None
    _set_head(sdk_session_id, new_head)

    return RewindResult(
        success=True,
        message="Omni session head moved",
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
            f"UPDATE omni_sessions SET head_node_id = {ph()} "
            f"WHERE session_id = {ph()}",
            (node_id, session_id),
        )


# ─── Fork ────────────────────────────────────────────────────────────────────

def fork_omni_session(
    source_sdk_session_id: str,
    fork_node_id: str,
    new_sdk_session_id: Optional[str] = None,
) -> Tuple[str, int]:
    """Create a new omni session whose transcript is the source's ancestry up
    to (and including) ``fork_node_id``. Returns ``(new_session_id, node_count)``.

    omni's in-engine ``Fork`` op only moves the head *within one session* —
    it exists so a single conversation can branch. The app's fork is a
    different product concept: a SECOND session row the user can drive
    independently. Since ``omni_session_nodes`` is keyed by
    ``(session_id, node_id)``, two sessions cannot literally share rows, so the
    ancestry prefix is copied — but every ``node_id``/``parent_id``/``ts``/
    ``message`` is preserved verbatim, so the forked prefix is byte-identical
    to the original's (which is what keeps provider prompt caching warm) and
    node handles stay comparable across the two sessions.

    The durable event log (``omni_session_events``) is deliberately NOT copied:
    it is a replay bus for live subscribers keyed to the run that produced it,
    not transcript state, and the fork starts its own run at seq 0.

    Raises ``ValueError`` when the fork point is not on the source's path.
    """
    path = load_omni_path(source_sdk_session_id)
    index = next(
        (i for i, node in enumerate(path) if node["node_id"] == fork_node_id), None
    )
    if index is None:
        raise ValueError(
            f"fork node {fork_node_id} is not on session {source_sdk_session_id}'s path"
        )
    prefix = path[: index + 1]
    new_id = new_sdk_session_id or new_omni_session_id()

    from psycopg2.extras import Json

    from app.db.base import get_cursor, get_db
    from app.db.compat import ph

    _ph = ph()
    with get_db() as conn:
        cursor = get_cursor(conn)
        cursor.execute(
            f"INSERT INTO omni_sessions (session_id, head_node_id, created_at) "
            f"VALUES ({_ph}, {_ph}, {_ph}) ON CONFLICT (session_id) DO NOTHING",
            (new_id, fork_node_id, time.time()),
        )
        for node in prefix:
            cursor.execute(
                f"INSERT INTO omni_session_nodes "
                f"(session_id, node_id, parent_id, ts, message) "
                f"VALUES ({_ph}, {_ph}, {_ph}, {_ph}, {_ph}) "
                f"ON CONFLICT (session_id, node_id) DO NOTHING",
                (
                    new_id,
                    node["node_id"],
                    node.get("parent_id"),
                    node.get("ts"),
                    # Same jsonb adaptation PgOmniStore uses on the write path.
                    Json(_as_dict(node.get("message"))),
                ),
            )
    return new_id, len(prefix)


# ─── Live transcript rendering ───────────────────────────────────────────────

def omni_live_messages(
    sdk_session_id: str, working_dir: str = "/workspace"
) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """``(messages, usage_data)`` for an omni session, in the shape the
    session read paths expect.

    Runs the projected entries through ``jsonl_parser._transform_entries`` —
    the pure, source-agnostic core already shared by the JSONL parser and the
    Postgres session-store parser — so an omni transcript renders through the
    identical transform (tool cards, thinking blocks, subagent rows, usage
    accounting) as every other source. Subagent children are not resolvable
    from the node tree yet, so no children loader is supplied.
    """
    entries = omni_transcript_entries(sdk_session_id)
    if not entries:
        return [], {}
    from app.core.jsonl_parser import _transform_entries

    return _transform_entries(entries, sdk_session_id, working_dir)
