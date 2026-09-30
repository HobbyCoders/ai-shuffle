"""Persist a session's pull-request lifecycle and fan it out to sidebars.

The sidepanel renders PR Opened / PR Merged from the session row, not from a
live GitHub poll. Git action buttons (Create PR, Finish, Ship It, merge/close)
and turn-end reconciliation write these fields and broadcast a user-wide
row patch so every device's list updates without a refetch.

Never raises: a persistence or GitHub blip must not break the git flow or
turn teardown. Merge never settles the session. A merged (or closed) PR is
not terminal for the session either: turn-end reconcile will adopt a newer
PR on the same checkout, whether that PR is still open or already merged.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Optional

from app.core.ttl_cache import TTLCache

logger = logging.getLogger(__name__)

# Keep strong references: turn teardown must not discard the refresh. Polls
# coalesce per session and are throttled independently of the header's cache.
_reconcile_tasks: dict[str, asyncio.Task] = {}
_reconcile_again: set[str] = set()
_reconcile_recent = TTLCache(ttl=60, max_entries=256, name="session-pr-reconcile")
_reconcile_slots = asyncio.Semaphore(4)


def schedule_session_pr_reconcile(
    session_id: str, *, force: bool = False
) -> Optional[asyncio.Task]:
    """Queue an owner-local refresh without blocking a turn or a Git poll.

    A turn end bypasses the poll cooldown. If a pre-merge poll is still in
    flight, force a trailing refresh rather than letting its old answer win.
    Failures retry twice; later polls can recover after that bounded attempt.
    """
    pending = _reconcile_tasks.get(session_id)
    if pending is not None:
        if force:
            _reconcile_again.add(session_id)
        return pending
    if not force and _reconcile_recent.get(session_id):
        return None
    # Bound the queue as well as concurrent gh calls. A later poll retries.
    if len(_reconcile_tasks) >= 64:
        return None

    async def refresh() -> None:
        while True:
            _reconcile_again.discard(session_id)
            for delay in (0, 2, 10):
                if delay:
                    await asyncio.sleep(delay)
                async with _reconcile_slots:
                    if await reconcile_session_pr(session_id):
                        break
            else:
                logger.warning("PR reconcile exhausted retries for %s", session_id)
            if session_id not in _reconcile_again:
                break

    def finished(task: asyncio.Task) -> None:
        # A callback also runs when cancelled before the coroutine starts.
        _reconcile_tasks.pop(session_id, None)
        _reconcile_again.discard(session_id)
        if not task.cancelled():
            _reconcile_recent.set(session_id, True)
            if task.exception() is not None:
                logger.warning("PR reconcile task failed for %s: %s", session_id, task.exception())

    task = asyncio.create_task(refresh(), name=f"session-pr:{session_id}")
    _reconcile_tasks[session_id] = task
    task.add_done_callback(finished)
    return task


_VALID_STATES = frozenset({"open", "merged", "closed"})


def _normalize_pr_state(state: Optional[str]) -> Optional[str]:
    if not state:
        return None
    low = str(state).strip().lower()
    if low in _VALID_STATES:
        return low
    return None


def _user_id_for_session(session: dict) -> str:
    return session.get("api_user_id") or "admin"


def _collect_session_ids(
    *,
    session_id: Optional[str] = None,
    project_id: Optional[str] = None,
    branch: Optional[str] = None,
    pr_number: Optional[int] = None,
) -> list[str]:
    from app.db import database

    ids: list[str] = []
    seen: set[str] = set()

    def _add(sid: Optional[str]) -> None:
        if sid and sid not in seen:
            seen.add(sid)
            ids.append(sid)

    _add(session_id)
    if pr_number is not None:
        for row in database.list_sessions_by_pr_number(pr_number):
            _add(row.get("id"))
    if project_id and branch:
        for row in database.list_sessions_for_branch(project_id, branch):
            _add(row.get("id"))
    return ids


async def persist_pr_state(
    *,
    pr_state: str,
    pr_number: Optional[int] = None,
    pr_url: Optional[str] = None,
    session_id: Optional[str] = None,
    project_id: Optional[str] = None,
    branch: Optional[str] = None,
) -> bool:
    """Write PR fields onto matching sessions and broadcast a sidebar patch.

    Matches by ``session_id``, existing ``pr_number``, and/or ``project_id`` +
    ``branch``. No-ops when nothing matches. Never raises. Does not change
    ``is_active``. Returns False on a failed write so reconciliation can retry.
    """
    state = _normalize_pr_state(pr_state)
    if state is None:
        return False
    try:
        from app.db import database
        from app.core.sync_engine import sync_engine

        session_ids = await asyncio.to_thread(
            _collect_session_ids,
            session_id=session_id,
            project_id=project_id,
            branch=branch,
            pr_number=pr_number,
        )
        if not session_ids:
            return True

        updates: dict[str, Any] = {"pr_state": state}
        if pr_number is not None:
            updates["pr_number"] = pr_number
        if pr_url is not None:
            updates["pr_url"] = pr_url

        for sid in session_ids:
            row = await asyncio.to_thread(
                database.update_session_pr_state,
                sid,
                pr_number=pr_number,
                pr_url=pr_url,
                pr_state=state,
            )
            if not row:
                continue
            if row.get("updated_at"):
                updates["updated_at"] = row["updated_at"]
            user_id = _user_id_for_session(row)
            try:
                await sync_engine.broadcast_session_updated(
                    session_id=sid,
                    updates=dict(updates),
                    source_device_id=None,
                )
            except Exception:
                logger.debug("session_updated PR broadcast failed for %s", sid, exc_info=True)
            try:
                await sync_engine.broadcast_user_session_updated(
                    user_id=user_id,
                    session_id=sid,
                    updates=dict(updates),
                    source_device_id=None,
                )
            except Exception:
                logger.debug(
                    "user_session_updated PR broadcast failed for %s", sid, exc_info=True
                )
        return True
    except Exception:
        logger.warning("persist_pr_state failed", exc_info=True)
        return False


def _github_state(pr: Any) -> Optional[str]:
    return _normalize_pr_state(getattr(pr, "state", None))


def _find_pull_for_branch(github_service: Any, repo: str, branch: str) -> Any:
    """Open PR on this branch, else the highest-numbered PR (merged/closed)."""
    pulls = github_service.list_pulls(repo, state="all", limit=100, head=branch)
    matching = [
        item for item in pulls if getattr(item, "head_branch", None) == branch
    ]
    if not matching:
        return None
    open_ones = [p for p in matching if _github_state(p) == "open"]
    pool = open_ones or matching
    return max(pool, key=lambda p: p.number)


async def reconcile_session_pr(session_id: str) -> bool:
    """Best-effort: if this session's branch has a GitHub PR, persist it.

    Prefers an open PR on the session branch; otherwise the latest PR for
    that branch (so a second PR that is already merged replaces the previous
    merged number). No branch: refresh the stored ``pr_number``. Unchanged
    number+state is a no-op.

    Canonical project-only sessions resolve through this runner's checkout;
    a legacy project alias is not required. No remote checkout is materialized.
    A failed lookup or write returns False so the scheduler can retry.
    An authoritative no-op returns True. Never clears state on failure.
    """
    try:
        from app.db import database
        from app.core.github_service import GitHubCliError, github_service

        session = await asyncio.to_thread(database.get_session, session_id)
        if not session:
            return True

        project_id = session.get("project_id")
        # Canonical sessions (including those synced from another worker) may
        # have no legacy project alias. The working-dir helper resolves their
        # runner-local checkout by session_id, not by that legacy alias.
        checkout_project_id = session.get("project_v2_id") or project_id
        branch = session.get("branch") or session.get("worktree_branch")
        pr_number = session.get("pr_number")
        current_state = (session.get("pr_state") or "").lower()
        if not checkout_project_id:
            return True
        if not branch and not pr_number:
            return True

        try:
            from app.api.git_helpers import get_working_dir_async

            working_dir = await get_working_dir_async(
                checkout_project_id, session.get("worktree_id"), session_id
            )
        except Exception:
            logger.debug("PR reconcile: working dir for %s failed", session_id, exc_info=True)
            return False

        if not working_dir:
            return False

        try:
            repo = await asyncio.to_thread(github_service.get_repo_from_remote, working_dir)
        except Exception:
            logger.debug("PR reconcile: repo lookup failed for %s", session_id, exc_info=True)
            return False
        if not repo:
            return True

        pr = None
        try:
            if branch:
                pr = await asyncio.to_thread(
                    _find_pull_for_branch, github_service, repo, branch
                )
            else:
                pr = await asyncio.to_thread(github_service.get_pull, repo, int(pr_number))
        except GitHubCliError:
            logger.debug("PR reconcile: gh lookup failed for %s", session_id, exc_info=True)
            return False
        except Exception:
            logger.debug("PR reconcile: lookup failed for %s", session_id, exc_info=True)
            return False

        if pr is None:
            return True

        state = _github_state(pr)
        if state is None:
            return False
        if pr.number == pr_number and state == current_state:
            return True
        return await persist_pr_state(
            session_id=session_id,
            project_id=project_id,
            branch=branch or getattr(pr, "head_branch", None),
            pr_number=pr.number,
            pr_url=pr.url,
            pr_state=state,
        )
    except Exception:
        logger.debug("reconcile_session_pr failed for %s", session_id, exc_info=True)
        return False
