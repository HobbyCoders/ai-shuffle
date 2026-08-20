"""Shared helper for reporting worktree metadata on a session.

Worktree sessions are effectively immortal: we never null ``worktree_id`` and we
never report a session as permanently dead. ``worktree_deleted`` means the
owner's tree is not materialized right now: no active checkout (and, for a
local session, no directory on this disk) and no in-flight ``ensure_worktree``.
It is never derived from the worktree row's ``status``, and a remote session
never stats the viewing install. The frontend uses this purely to drive a
"worktree dir missing" banner/badge; it does not block sending, and nothing is
recreated automatically.

This logic used to be copy-pasted in four near-identical blocks across
``sessions.py``, ``chat_ws_handler.py`` and ``websocket.py`` and had already
drifted. It now lives here so all call sites stay in sync.
"""

import time
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

from app.core.config import settings
from app.core.project_utils import get_worktree_working_dir, resolve_existing_worktree_cwd
from app.db import checkouts as checkouts_db
from app.db import git_ops as git_ops_db

# TTL cache for the worktree-dir existence stat. enrich_session_worktree_meta
# runs synchronously on the event loop on EVERY /live poll (150ms while
# streaming, 1.5s parked — before the 304 short-circuit), so the filesystem must
# not be hit each time. GIL + benign races make a lock unnecessary; a stale
# answer self-corrects within the TTL.
_IS_DIR_TTL_SECONDS = 3.0
_IS_DIR_CACHE_MAX = 256
_is_dir_cache: Dict[str, Tuple[bool, float]] = {}


def _is_dir_cached(path: str) -> bool:
    """``Path(path).is_dir()`` memoized for ``_IS_DIR_TTL_SECONDS``."""
    now = time.monotonic()
    hit = _is_dir_cache.get(path)
    if hit is not None and (now - hit[1]) < _IS_DIR_TTL_SECONDS:
        return hit[0]
    result = Path(path).is_dir()
    if len(_is_dir_cache) >= _IS_DIR_CACHE_MAX:
        # Opportunistic prune: drop expired entries; if everything is somehow
        # fresh, clear outright (correctness is unaffected — it's a cache).
        expired = [k for k, (_, ts) in _is_dir_cache.items() if (now - ts) >= _IS_DIR_TTL_SECONDS]
        for k in expired:
            _is_dir_cache.pop(k, None)
        if len(_is_dir_cache) >= _IS_DIR_CACHE_MAX:
            _is_dir_cache.clear()
    _is_dir_cache[path] = (result, now)
    return result


def _logical_branch(session: Dict[str, Any]) -> Optional[str]:
    """Checkout / git_ops branch: v2 ``sessions.branch``, else legacy worktree."""
    return session.get("branch") or session.get("worktree_branch")


def _legacy_path_deleted(
    session: Dict[str, Any], worktree: Optional[Dict[str, Any]], path: Optional[str]
) -> bool:
    """Owner-disk honesty via the resume-path candidates, not checkouts."""
    if not path:
        return False
    # Only report "deleted" when the preferred path actually resolves —
    # an unresolvable path means "unknown", not "gone" (raises to the
    # caller, which fails open).
    get_worktree_working_dir(
        {"worktree_path": path, "id": session.get("worktree_id") or "unknown"}
    )
    return (
        resolve_existing_worktree_cwd(worktree, session, is_dir=_is_dir_cached)
        is None
    )


def _worktree_deleted(
    session: Dict[str, Any],
    worktree: Optional[Dict[str, Any]],
    path: Optional[str],
) -> bool:
    """True when the owner's tree is gone and nothing is materializing it.

    Remote sessions never stat this install. In-flight ``ensure_worktree``
    (queued or fresh-running) is not-deleted. This path is a pure read —
    never ``fail_op``. Fail-open is the caller's enclosing try.
    """
    owner = session.get("instance_id")
    me = settings.effective_instance_id
    is_remote = bool(owner) and owner != me
    pv2_id = session.get("project_v2_id")
    branch = _logical_branch(session)

    if pv2_id:
        # find_matching is a pure read: stale running is skipped so a zombie
        # cannot hide a newer queued retry. Do not fail_op here — /live polls
        # often run on the viewing install, not the runner that owns the op.
        inflight = git_ops_db.find_matching(
            owner or me, pv2_id, "ensure_worktree", branch
        )
        if inflight:
            return False

    if is_remote:
        if not pv2_id or not owner:
            return False
        return not checkouts_db.has_active_checkout(pv2_id, owner, branch)

    if pv2_id:
        rows = checkouts_db.list_active_checkouts(pv2_id, me, branch)
        if rows:
            return not any(
                _is_dir_cached(r["path"]) for r in rows if r.get("path")
            )

    return _legacy_path_deleted(session, worktree, path)


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
        - ``worktree_deleted`` is True only when the owner's tree is not
          materialized: no active checkout among **all** rows for
          ``(project_v2, owner, branch)``, no in-flight ``ensure_worktree``,
          and (local only) no existing directory. Remote sessions never stat
          the viewing install. Resolver / DB errors fail open to False so a
          blip cannot scare the UI. The worktree row's ``status`` is never
          consulted.
    """
    session_wt_id = session.get("worktree_id")
    session_branch = session.get("worktree_branch")
    # V2 branch-intake (Phase 3): a NEW branch session records only
    # ``sessions.branch`` (no legacy worktree_id/worktree_branch/worktree_path).
    # Surface it as a worktree-mode session so the resume snapshot restores branch
    # mode. Existence comes from checkouts + in-flight git_ops, not from
    # ``path is None → not deleted``.
    session_v2_branch = session.get("branch")

    # Not a worktree/branch session at all — nothing to add.
    if not (session_wt_id or session_branch or session.get("worktree_path") or session_v2_branch):
        return {}

    # Prefer the live worktree row's values; fall back to the session's backup
    # columns, then the v2 logical branch, when the row is missing or carries no
    # branch/path.
    branch = (worktree.get("branch_name") if worktree else None) or session_branch or session_v2_branch
    path = (worktree.get("worktree_path") if worktree else None) or session.get(
        "worktree_path"
    )

    # Guard every failure mode and fall back to False: a resolution/stat/DB
    # error must not scare the UI into showing the worktree as missing.
    worktree_deleted = False
    try:
        worktree_deleted = _worktree_deleted(session, worktree, path)
    except Exception:  # noqa: BLE001
        worktree_deleted = False

    return {
        # Never null — keep the session reopenable.
        "worktree_id": session_wt_id,
        "worktree_branch": branch,
        "worktree_path": path,
        "worktree_deleted": worktree_deleted,
        "execution_mode": "worktree",
    }
