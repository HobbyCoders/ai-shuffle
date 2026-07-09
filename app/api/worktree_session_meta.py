"""Shared helper for reporting worktree metadata on a session.

Worktree sessions are effectively immortal: we never null ``worktree_id`` and we
never report a session as permanently dead. ``worktree_deleted`` means only "the
worktree directory is not on disk right now" (derived from an actual stat, never
from the row's ``status``). The frontend uses this purely to drive a "worktree
dir missing" banner/badge; it does not block sending, and nothing is recreated
automatically.

This logic used to be copy-pasted in four near-identical blocks across
``sessions.py``, ``chat_ws_handler.py`` and ``websocket.py`` and had already
drifted. It now lives here so all call sites stay in sync.
"""

import time
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

from app.core.project_utils import get_worktree_working_dir, resolve_existing_worktree_cwd

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
        - ``worktree_deleted`` reflects ACTUAL disk state: True only when the
          resolved worktree directory does not exist on THIS device right now. A
          session resumes on any existing cwd regardless of the row's ``status``
          (a per-device sync sweep can flip the status without the dir moving), so
          status is intentionally not consulted here. The frontend uses this
          purely to drive a "worktree dir missing" banner/badge.
    """
    session_wt_id = session.get("worktree_id")
    session_branch = session.get("worktree_branch")

    # Not a worktree session at all — nothing to add.
    if not (session_wt_id or session_branch or session.get("worktree_path")):
        return {}

    # Prefer the live worktree row's values; fall back to the session's backup
    # columns when the row is missing or carries no branch/path.
    branch = (worktree.get("branch_name") if worktree else None) or session_branch
    path = (worktree.get("worktree_path") if worktree else None) or session.get(
        "worktree_path"
    )

    # Derive "deleted" from actual disk existence (TTL-cached stat). Same shared
    # rule as the resume path (resolve_existing_worktree_cwd): row path first,
    # then the session's backup path — deleted only when NO candidate dir exists.
    # Guard every failure mode and fall back to False: a resolution/stat error
    # must not scare the UI into showing the worktree as missing.
    worktree_deleted = False
    try:
        if path:
            # Only report "deleted" when the preferred path actually resolves —
            # an unresolvable path means "unknown", not "gone".
            get_worktree_working_dir(
                {"worktree_path": path, "id": session_wt_id or "unknown"}
            )
            worktree_deleted = (
                resolve_existing_worktree_cwd(worktree, session, is_dir=_is_dir_cached)
                is None
            )
    except Exception:  # noqa: BLE001
        worktree_deleted = False

    return {
        # Never null — keep the session reopenable.
        "worktree_id": session_wt_id,
        "worktree_branch": branch,
        "worktree_path": path,
        # Disk-based: True means the worktree directory is not present right now.
        "worktree_deleted": worktree_deleted,
        "execution_mode": "worktree",
    }
