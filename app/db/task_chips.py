"""Durable background-task chips of a chat: suggested work the owner can start.

Rows live on the worker that owns the chat and reach other workers through the
same owner-ordered mirror as prompts. Only the owner writes; a mirror's action
travels over the signed control channel. Nothing here starts work.
"""
from contextlib import nullcontext
from typing import Any, Dict, List, Optional
from uuid import uuid4

from app.db.base import get_db, transaction

PENDING = "pending"
STARTING = "starting"
STARTED = "started"
DISMISSED = "dismissed"
FAILED = "failed"
# Internal: a rewind took it back before the owner acted. A soft delete, so the
# mirror's upsert carries it; the API never shows these rows.
REMOVED = "removed"

OPEN_STATES = (PENDING, STARTING, FAILED)
# Only the owner can move these on: a suggestion to start, a failed start to
# retry or dismiss. A ``starting`` chip is the app's turn, not the owner's.
WAITING_STATES = (PENDING, FAILED)
MAX_OPEN = 10
# A claim older than this has no live start behind it, whatever the process did.
STALE_CLAIM_SECONDS = 300

_OPEN_IN = "(%s, %s, %s)"
_COLUMNS = ("id, session_id, origin_session_id, origin_seq, request_id, title, summary, prompt, base, "
            "base_commit, project_v2_id, origin_label, origin_mode, origin_tools, model, provider_id, model_label, effort, "
            "state, attempt, error, started_session_id, started_title, "
            "created_at, updated_at, claimed_at")


def _dict(row) -> Optional[Dict[str, Any]]:
    return dict(row) if row else None


def create(session_id: str, *, origin_session_id: str, origin_seq: int, request_id: str, title: str,
           summary: str, prompt: str, base: str, base_commit: Optional[str],
           project_v2_id: Optional[str], model: str, provider_id: Optional[str] = None,
           model_label: Optional[str] = None, effort: Optional[str] = None,
           origin_label: Optional[str] = None, origin_mode: Optional[str] = None,
           origin_tools: Optional[str] = None, chip_id: Optional[str] = None, _conn=None) -> Dict[str, Any]:
    """Insert a chip, or return the open one it duplicates.

    ``{"chip": row, "created": bool}``; ``chip`` is None with ``"limit": True``
    at :data:`MAX_OPEN`. Serialized per chat by a row lock on its session, so
    two racing calls cannot both pass the checks. ``origin_tools`` is the JSON of
    the tools the suggesting agent could use; None when the chat itself suggested.
    ``_conn`` runs it in the caller's transaction.
    """
    with nullcontext(_conn) if _conn is not None else transaction() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT id FROM sessions WHERE id=%s FOR UPDATE", (session_id,))
        cursor.execute(
            f"SELECT {_COLUMNS} FROM session_task_chips WHERE session_id=%s AND origin_session_id=%s "
            "AND request_id=%s", (session_id, origin_session_id, request_id))
        previous = cursor.fetchone()
        if previous:
            return {"chip": dict(previous), "created": False}
        cursor.execute(
            f"SELECT {_COLUMNS} FROM session_task_chips WHERE session_id=%s AND state IN {_OPEN_IN} "
            "AND lower(title)=lower(%s) LIMIT 1", (session_id, *OPEN_STATES, title))
        previous = cursor.fetchone()
        if previous:
            return {"chip": dict(previous), "created": False}
        cursor.execute(f"SELECT COUNT(*) AS n FROM session_task_chips WHERE session_id=%s AND state IN {_OPEN_IN}",
                       (session_id, *OPEN_STATES))
        if cursor.fetchone()["n"] >= MAX_OPEN:
            return {"chip": None, "created": False, "limit": True}
        cursor.execute(
            f"INSERT INTO session_task_chips (id, session_id, origin_session_id, origin_seq, request_id, "
            f"title, summary, prompt, base, base_commit, project_v2_id, origin_label, origin_mode, "
            f"origin_tools, model, provider_id, model_label, effort, state) "
            f"VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING {_COLUMNS}",
            (chip_id or str(uuid4()), session_id, origin_session_id, origin_seq, request_id, title, summary,
             prompt, base, base_commit, project_v2_id, origin_label, origin_mode, origin_tools, model,
             provider_id, model_label, effort, PENDING))
        return {"chip": dict(cursor.fetchone()), "created": True}


def find_request(origin_session_id: str, request_id: str, cursor=None) -> Optional[Dict[str, Any]]:
    """The chip an agent already recorded under this request id, whichever chat shows it."""
    query = (f"SELECT {_COLUMNS} FROM session_task_chips WHERE origin_session_id=%s AND request_id=%s LIMIT 1",
             (origin_session_id, request_id))
    if cursor is not None:
        cursor.execute(*query)
        return _dict(cursor.fetchone())
    with get_db() as conn:
        read = conn.cursor()
        read.execute(*query)
        return _dict(read.fetchone())


def get(session_id: str, chip_id: str) -> Optional[Dict[str, Any]]:
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(f"SELECT {_COLUMNS} FROM session_task_chips WHERE id=%s AND session_id=%s AND state<>%s",
                       (chip_id, session_id, REMOVED))
        return _dict(cursor.fetchone())


def get_any(chip_id: str) -> Optional[Dict[str, Any]]:
    """By id alone, removed rows included: a mirror only needs the chat it belongs to."""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(f"SELECT {_COLUMNS} FROM session_task_chips WHERE id=%s", (chip_id,))
        return _dict(cursor.fetchone())


def list_for_session(session_id: str) -> List[Dict[str, Any]]:
    """Every chip still to show, oldest first."""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(f"SELECT {_COLUMNS} FROM session_task_chips WHERE session_id=%s AND state<>%s "
                       "ORDER BY created_at, id", (session_id, REMOVED))
        return [dict(row) for row in cursor.fetchall()]


def waiting_counts(session_ids: List[str]) -> Dict[str, Dict[str, Any]]:
    """How many chips wait on the owner per chat, and whose chat it is, in one query.

    Every chat that exists gets an entry, zero included, so a caller can publish
    the count dropping to nothing.
    """
    ids = list(dict.fromkeys(session_ids))
    if not ids:
        return {}
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT s.id, s.api_user_id, COUNT(c.id) AS waiting FROM sessions s "
            "LEFT JOIN session_task_chips c ON c.session_id=s.id AND c.state IN (%s, %s) "
            f"WHERE s.id IN ({', '.join(['%s'] * len(ids))}) GROUP BY s.id, s.api_user_id",
            (*WAITING_STATES, *ids))
        return {row["id"]: {"api_user_id": row["api_user_id"], "pending_task_chips": int(row["waiting"])}
                for row in cursor.fetchall()}


def source_of(started_session_id: str) -> Optional[Dict[str, Any]]:
    """The chip that started this chat, naming the chat it was suggested in."""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT c.id AS chip_id, c.session_id, s.title FROM session_task_chips c "
            "LEFT JOIN sessions s ON s.id=c.session_id WHERE c.started_session_id=%s LIMIT 1",
            (started_session_id,))
        return _dict(cursor.fetchone())


def claim(session_id: str, chip_id: str) -> Optional[Dict[str, Any]]:
    """Move a startable chip to ``starting``; None when it was not startable.

    The single point that decides who starts a chip: two clients or two
    devices race here and exactly one gets the row. A stale claim (the process
    died mid-start) is startable again.
    """
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            f"UPDATE session_task_chips SET state=%s, attempt=attempt+1, error=NULL, claimed_at=NOW(), "
            "updated_at=NOW() WHERE id=%s AND session_id=%s AND (state IN (%s, %s) OR (state=%s AND "
            f"claimed_at < NOW() - make_interval(secs => {STALE_CLAIM_SECONDS}))) RETURNING {_COLUMNS}",
            (STARTING, chip_id, session_id, PENDING, FAILED, STARTING))
        row = _dict(cursor.fetchone())
        conn.commit()
        return row


def mark_started(cursor, chip_id: str, attempt: int, new_session_id: str, title: str) -> bool:
    """In the transaction that creates the chat, so a chip never loses its chat."""
    cursor.execute(
        "UPDATE session_task_chips SET state=%s, started_session_id=%s, started_title=%s, error=NULL, "
        "updated_at=NOW() WHERE id=%s AND state=%s AND attempt=%s",
        (STARTED, new_session_id, title, chip_id, STARTING, attempt))
    return cursor.rowcount > 0


def mark_failed(chip_id: str, attempt: int, error: str) -> Optional[Dict[str, Any]]:
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            f"UPDATE session_task_chips SET state=%s, error=%s, updated_at=NOW() "
            f"WHERE id=%s AND state=%s AND attempt=%s RETURNING {_COLUMNS}",
            (FAILED, error, chip_id, STARTING, attempt))
        row = _dict(cursor.fetchone())
        conn.commit()
        return row


def dismiss(session_id: str, chip_id: str) -> Optional[Dict[str, Any]]:
    """None when the chip can no longer be dismissed (started, or starting)."""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            f"UPDATE session_task_chips SET state=%s, updated_at=NOW() "
            f"WHERE id=%s AND session_id=%s AND state IN (%s, %s) RETURNING {_COLUMNS}",
            (DISMISSED, chip_id, session_id, PENDING, FAILED))
        row = _dict(cursor.fetchone())
        conn.commit()
        return row


def remove_rewound(conn, session_id: str, from_seq: int) -> List[str]:
    """A rewind erased the turn that raised these: take back the pending ones.

    Runs in the rewind's own transaction. A chip the owner already acted on
    stays: its chat is real, and a failed or dismissed chip records that choice.
    """
    cursor = conn.cursor()
    cursor.execute(
        "UPDATE session_task_chips SET state=%s, updated_at=NOW() "
        "WHERE session_id=%s AND state=%s AND origin_seq>=%s RETURNING id",
        (REMOVED, session_id, PENDING, from_seq))
    return [row["id"] for row in cursor.fetchall()]


def remove_all_pending(conn, session_id: str) -> List[str]:
    """Clearing a chat rewinds past every turn: :func:`remove_rewound` from the start."""
    return remove_rewound(conn, session_id, 0)


def fail_interrupted(device_id: Optional[str], instance_id: Optional[str], error: str) -> List[str]:
    """Boot: no start survives a restart, so a ``starting`` chip owned here failed."""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "UPDATE session_task_chips SET state=%s, error=%s, updated_at=NOW() "
            "WHERE state=%s AND session_id IN (SELECT id FROM sessions WHERE "
            "owner_device_id=%s OR (owner_device_id IS NULL AND instance_id=%s)) RETURNING session_id",
            (FAILED, error, STARTING, device_id, instance_id))
        rows = [row["session_id"] for row in cursor.fetchall()]
        conn.commit()
        return rows
