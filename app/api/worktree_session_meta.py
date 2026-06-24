"""Shared helper for reporting worktree metadata on a session.

Worktree sessions are effectively immortal: once a session is a worktree
session it ALWAYS stays reopenable. We never null ``worktree_id`` and we never
report a session as permanently dead. ``worktree_deleted`` means only "the
worktree is not materialized on disk right now" — the backend recreates it from
its branch (last git state) on the next send. The frontend uses this purely to
drive a transient reopen spinner; it no longer blocks sending or shows a lock
badge.

This logic used to be copy-pasted in four near-identical blocks across
``sessions.py``, ``chat_ws_handler.py`` and ``websocket.py`` and had already
drifted. It now lives here so all call sites stay in sync.
"""

from typing import Any, Dict, Optional

from app.db.base import WorktreeStatus


def enrich_session_worktree_meta(
    session: Dict[str, Any], worktree: Optional[Dict[str, Any]]
) -> Dict[str, Any]:
    """Compute worktree metadata for a session.

    Args:
        session: The session row (carries the backup ``worktree_id`` /
            ``worktree_branch`` / ``worktree_path`` columns).
        worktree: The linked worktree row (already fetched by the caller via
            ``database.get_worktree``), or ``None`` if no row could be loaded.

    Returns:
        A dict with the keys ``worktree_id``, ``worktree_branch``,
        ``worktree_path``, ``worktree_deleted`` and ``execution_mode`` for a
        worktree session. For a NON-worktree session returns an EMPTY dict so
        callers can merge it without changing any existing behavior.

    Semantics:
        - We NEVER null ``worktree_id`` — a worktree session always stays
          reopenable.
        - ``worktree_deleted`` is True ONLY when the worktree is not materialized
          right now (no ACTIVE worktree row). It is a transient "will be
          recreated on next send" flag, not a permanent-death flag.
    """
    session_wt_id = session.get("worktree_id")
    session_branch = session.get("worktree_branch")

    # Not a worktree session at all — nothing to add.
    if not (session_wt_id or session_branch or session.get("worktree_path")):
        return {}

    is_active = bool(worktree and worktree.get("status") == WorktreeStatus.ACTIVE)

    # Prefer the live worktree row's values; fall back to the session's backup
    # columns when the row is missing or carries no branch/path.
    branch = (worktree.get("branch_name") if worktree else None) or session_branch
    path = (worktree.get("worktree_path") if worktree else None) or session.get(
        "worktree_path"
    )

    return {
        # Never null — keep the session reopenable.
        "worktree_id": session_wt_id,
        "worktree_branch": branch,
        "worktree_path": path,
        # True only means "not materialized right now; recreated on next send".
        "worktree_deleted": not is_active,
        "execution_mode": "worktree",
    }
