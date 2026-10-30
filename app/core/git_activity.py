"""Owner-side preconditions for user-requested Git mutations."""
import logging
from pathlib import Path

from fastapi import HTTPException

from app.core.git_service import git_service
from app.core.project_resolver import resolve_existing_cwd, resolve_project_v2
from app.core.task_registry import task_registry
from app.db import database
from app.db.base import get_db
from app.db.checkouts import get_checkouts_by_path
from app.db.sessions import list_sessions_for_cleanup
from app.network import control


logger = logging.getLogger(__name__)


class GitActivityUnavailable(RuntimeError):
    def __init__(self, message, *, public_reason="repository activity could not be checked"):
        super().__init__(message)
        self.public_reason = public_reason

def _live_session_ids():
    # Uncached leases plus draining turns; one query rather than N guard reads.
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT session_id FROM runs WHERE state='active' AND lease_expires_at >= NOW() "
                       "UNION SELECT session_id FROM turns WHERE state IN ('running','interrupting','draining')")
        return {row['session_id'] for row in cursor.fetchall()}

def _project_candidates(project_id, working_dir=None):
    # A project linked late keeps its placeholder identity next to the remote
    # one at the same folder (#802); a chat on either can be using it.
    identities = [row['project_id'] for row in get_checkouts_by_path(working_dir)] if working_dir else []
    canonical = identities[0] if len(identities) == 1 else (identities or None)
    if not canonical:
        project = resolve_project_v2({'project_id': project_id})
        canonical = project['id'] if project else None
    # Only sessions that can be active are read: a project keeps thousands of
    # finished chats and Task workers, and loading every row on each Git status
    # poll held a pooled connection for many seconds under load.
    live = _live_session_ids()
    possible = live | set(task_registry.active_session_ids())
    if not possible:
        return []
    return _active(list_sessions_for_cleanup(project_id, canonical, possible), live)


def active_git_sessions(sessions):
    """Shared uncached activity truth for Git mutations and stale-registration cleanup."""
    return _active(sessions, _live_session_ids())


def _active(sessions, live):
    return [s for s in sessions if s.get('id') and
            (s['id'] in live or task_registry.has_active_background_work(s['id'])
             or task_registry.has_running_monitor_shell(s['id']))]

def project_has_active_git_sessions(project_id, working_dir=None):
    try:
        return bool(_project_candidates(project_id, working_dir))
    except Exception as exc:
        raise HTTPException(503, 'Cannot verify repository activity. Git changes are blocked.') from exc

def busy_git_target_sessions(project_id, working_dir, branches=(), exclude=(), *, include_remote=False):
    """Busy users of this cwd OR branch. Hidden/v2 alias sessions are included.

    Return an empty list only when idle has been positively established. Unknown
    liveness/cwd is not proof of safety; callers either block or return HTTP 503.
    ``exclude`` names sessions the caller has already accounted for (a session
    handoff's own reservations), which are skipped rather than evaluated.
    Remote-owned sessions use another worker's checkout and are skipped unless
    ``include_remote`` protects a shared GitHub operation rather than local Git.
    """
    session_id = None
    try:
        skip = set(exclude)
        candidates = [s for s in _project_candidates(project_id, working_dir) if s['id'] not in skip]
        if not candidates:
            return []
        target = Path(working_dir).resolve() if working_dir else None
        wanted = {b for b in branches if b}
        if working_dir:
            current = git_service.get_current_branch(working_dir)
            if current:
                wanted.add(current)
        busy = []
        for session in candidates:
            session_id = session['id']
            remote = control.owner_of(session) is not None
            if remote and not include_remote:
                continue  # Another worker's checkout cannot use this worker's Git state.
            branch = session.get('branch') or session.get('worktree_branch')
            if branch and branch in wanted:
                busy.append(session['id'])
                continue
            if remote:
                if branch and wanted:
                    continue  # Known unrelated branch on another worker.
                raise GitActivityUnavailable(
                    f"Unknown branch of an active remote session (session {session['id']})",
                    public_reason="an active session's branch is unknown",
                )
            if session.get('worktree_id'):
                from app.core.project_utils import get_worktree_working_dir
                row = database.get_worktree(session['worktree_id'])
                cwd = get_worktree_working_dir(row) if row else None
            else:
                cwd = resolve_existing_cwd(session)
            if not cwd:
                raise GitActivityUnavailable(
                    f"Unknown cwd of an active session (session {session['id']})",
                    public_reason="an active session's checkout location is unknown",
                )
            if target and Path(cwd).resolve() == target:
                busy.append(session['id'])
            else:
                actual_branch = git_service.get_current_branch(cwd)
                if branch in wanted or actual_branch in wanted:
                    busy.append(session['id'])
                elif wanted and not branch and not actual_branch:
                    raise GitActivityUnavailable(
                        f"Unknown branch of an active session (session {session['id']})",
                        public_reason="an active session's branch is unknown",
                    )
        return busy
    except GitActivityUnavailable:
        raise
    except Exception as exc:
        context = f' (session {session_id})' if session_id else ''
        raise GitActivityUnavailable(f'Cannot verify checkout activity{context}') from exc

def require_idle_git_target(project_id, working_dir, branches=(), *, include_remote=False):
    try:
        busy = busy_git_target_sessions(project_id, working_dir, branches, include_remote=include_remote)
    except GitActivityUnavailable as exc:
        logger.debug("Cannot verify Git activity for project %s: %s", project_id, exc, exc_info=True)
        raise HTTPException(503, f'Cannot verify worktree/branch activity: {exc.public_reason}. Git changes are blocked until activity can be verified.') from exc
    if busy:
        raise HTTPException(409, 'A session using this worktree or branch is active. Stop it or wait before changing Git state.')

def git_target_mutations_blocked(project_id, working_dir):
    try:
        return bool(busy_git_target_sessions(project_id, working_dir))
    except GitActivityUnavailable:
        return True
