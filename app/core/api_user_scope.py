"""Which API user a session runs as, and what that key may reach.

A session belongs to the key that started it (``sessions.api_user_id``); NULL
is the owner. Worker sessions minted before workers inherited that column
carry NULL and belong to whoever owns the session that spawned them, so every
lookup walks ``is_worker`` parents the same way
:func:`app.api.sessions_helpers.api_user_owns_session` does.

Synchronous (DB reads): async callers go through ``run_sync``/``to_thread``.
"""

from __future__ import annotations

from typing import Any, Iterable, Mapping, Optional, Set

_OWNER_DEPTH = 8


def session_api_user_id(row: Optional[Mapping[str, Any]]) -> Optional[str]:
    """The API user ``row`` runs as; ``None`` means the owner."""
    from app.db import database

    for _ in range(_OWNER_DEPTH):
        if not row:
            return None
        owner = row.get("api_user_id")
        if owner or not row.get("is_worker") or not row.get("parent_session_id"):
            return owner or None
        row = database.get_session(row["parent_session_id"])
    return None


def viewer_api_user_id(session_id: Optional[str]) -> Optional[str]:
    """The API user behind an app session id or its store (``sdk``) id.

    ``None`` when the session is the owner's or has no app row (a store-only
    session, such as a standalone fluid run, is never an API key's).
    """
    if not session_id:
        return None
    from app.db import database

    row = database.get_session(session_id) or database.get_session_by_sdk_id(session_id)
    return session_api_user_id(row)


def visible_store_ids(store_ids: Iterable[str], api_user_id: str) -> Set[str]:
    """The subset of transcript-store ids whose app session ``api_user_id`` owns.

    A store id with no app session row is nobody's key's, so it is never
    visible to one.
    """
    from app.db import database
    from app.db.base import get_db

    ids = sorted({sid for sid in store_ids if sid})
    if not ids:
        return set()
    placeholders = ", ".join(["%s"] * len(ids))
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute(
            "SELECT sdk_session_id, api_user_id, is_worker, parent_session_id "
            f"FROM sessions WHERE sdk_session_id IN ({placeholders})",
            ids,
        )
        rows = [dict(r) for r in cur.fetchall()]
    visible: Set[str] = set()
    for row in rows:
        owner = row.get("api_user_id")
        if not owner and row.get("is_worker") and row.get("parent_session_id"):
            owner = session_api_user_id(database.get_session(row["parent_session_id"]))
        if owner == api_user_id:
            visible.add(row["sdk_session_id"])
    return visible


def worker_refusal(api_user_id: Optional[str], profile_id: str) -> Optional[str]:
    """Why a session running as ``api_user_id`` may not start a worker on
    ``profile_id``; ``None`` = allowed.

    The owner is unrestricted. An API key is held to its assigned agents
    (``profile_ids``, empty = any), and a key that was deleted or deactivated
    may not start anything.
    """
    if not api_user_id:
        return None
    from app.db import database

    user = database.get_api_user(api_user_id)
    if not user or not user.get("is_active", True):
        return "This session's API key is no longer active."
    allowed = user.get("profile_ids") or []
    if allowed and profile_id not in allowed:
        return f"This API key is not allowed to use agent '{profile_id}'."
    return None
