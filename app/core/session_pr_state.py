"""Persist a session's pull-request lifecycle and fan it out to sidebars.

The sidepanel renders PR Opened / PR Merged from the session row, not from a
live GitHub poll. Git action buttons (Create PR, Finish, Ship It, merge/close)
and turn-end reconciliation write these fields and broadcast a user-wide
row patch so every device's list updates without a refetch.

Never raises: a persistence or GitHub blip must not break the git flow or
turn teardown. Merge never settles the session. A merged (or closed) PR is
not terminal for the session either: turn-end reconcile will adopt a newer
open PR on the same checkout.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Optional

logger = logging.getLogger(__name__)

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
) -> None:
    """Write PR fields onto matching sessions and broadcast a sidebar patch.

    Matches by ``session_id``, existing ``pr_number``, and/or ``project_id`` +
    ``branch``. No-ops when nothing matches. Never raises. Does not change
    ``is_active``.
    """
    state = _normalize_pr_state(pr_state)
    if state is None:
        return
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
            return

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
    except Exception:
        logger.warning("persist_pr_state failed", exc_info=True)


def _github_state(pr: Any) -> Optional[str]:
    return _normalize_pr_state(getattr(pr, "state", None))


def _find_open_pull(github_service: Any, repo: str, branch: str) -> Any:
    for item in github_service.list_pulls(repo, state="open", limit=100):
        if getattr(item, "head_branch", None) == branch:
            return item
    return None


async def reconcile_session_pr(session_id: str) -> None:
    """Best-effort: if this session's branch has a GitHub PR, persist it.

    - ``merged`` / ``closed`` describe that pull request, not the session.
      Keep looking for a newer open PR on the current branch so a session
      that continues after merge (or close) can pick up the next one.
    - Has ``pr_number`` and not yet terminal: refresh that PR (catches
      agent/gh merges).
    - Otherwise: look up an open PR whose head is the session branch.

    A failed GitHub lookup never clears persisted state. Never raises.
    """
    try:
        from app.db import database
        from app.core.github_service import GitHubCliError, github_service

        session = await asyncio.to_thread(database.get_session, session_id)
        if not session:
            return

        project_id = session.get("project_id")
        branch = session.get("worktree_branch") or session.get("branch")
        pr_number = session.get("pr_number")
        current_state = (session.get("pr_state") or "").lower()
        terminal = current_state in ("merged", "closed")
        if not project_id:
            return
        if terminal:
            if not branch:
                return
        elif not branch and not pr_number:
            return

        try:
            from app.api.git_helpers import get_working_dir_async

            working_dir = await get_working_dir_async(
                project_id, session.get("worktree_id"), session_id
            )
        except Exception:
            logger.debug("PR reconcile: working dir for %s failed", session_id, exc_info=True)
            return

        if not working_dir:
            return

        try:
            repo = await asyncio.to_thread(github_service.get_repo_from_remote, working_dir)
        except Exception:
            logger.debug("PR reconcile: repo lookup failed for %s", session_id, exc_info=True)
            return
        if not repo:
            return

        pr = None
        try:
            if terminal or not pr_number:
                if branch:
                    pr = await asyncio.to_thread(
                        _find_open_pull, github_service, repo, branch
                    )
            else:
                pr = await asyncio.to_thread(github_service.get_pull, repo, int(pr_number))
        except GitHubCliError:
            logger.debug("PR reconcile: gh lookup failed for %s", session_id, exc_info=True)
            return
        except Exception:
            logger.debug("PR reconcile: lookup failed for %s", session_id, exc_info=True)
            return

        if pr is None:
            return

        state = _github_state(pr) or "open"
        if terminal and state != "open":
            return
        await persist_pr_state(
            session_id=session_id,
            project_id=project_id,
            branch=branch or getattr(pr, "head_branch", None),
            pr_number=pr.number,
            pr_url=pr.url,
            pr_state=state,
        )
    except Exception:
        logger.debug("reconcile_session_pr failed for %s", session_id, exc_info=True)
