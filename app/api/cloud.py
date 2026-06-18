"""
Cloud intake — the *worker* side of cloud offload.

When another AI Shuffle install (the *primary*) runs a session in "cloud"
execution mode, it calls these endpoints on this install to do the real work:

- ``POST /api/v1/cloud/intake`` — link or create a project for a GitHub repo
  (cloning it into ``/workspace`` if needed), spin up a git worktree session and
  start the first query.
- ``POST /api/v1/cloud/intake/{session_id}/message`` — run a follow-up prompt.
- ``POST /api/v1/cloud/intake/{session_id}/interrupt`` — interrupt a running query.

All endpoints authenticate via ``require_auth`` so the primary can reach them
with an API key. The resulting sessions/worktrees are ordinary local sessions on
this install — the primary just mirrors their live state back to the user.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel

from app.db import database
from app.db.async_database import run_sync
from app.api.auth import require_auth, get_api_user_from_request

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/cloud", tags=["Cloud"])


class CloudIntakeRequest(BaseModel):
    """A primary install asking this worker to run a repo session."""
    project_id: str
    repo_url: str
    github_repo_name: Optional[str] = None
    default_branch: str = "main"
    prompt: str
    profile_id: Optional[str] = None
    branch_name: Optional[str] = None
    base_branch: Optional[str] = None
    pending_upload_paths: Optional[List[str]] = None
    overrides: Optional[Dict[str, Any]] = None


class CloudIntakeResponse(BaseModel):
    session_id: str
    project_id: str
    worktree_id: str
    branch_name: str
    base_branch: Optional[str] = None
    worktree_path: str
    status: str = "active"
    query_started: bool = False
    project_created: bool = False
    upload_migration_errors: Optional[List[str]] = None


class CloudMessageRequest(BaseModel):
    prompt: str
    profile_id: Optional[str] = None
    overrides: Optional[Dict[str, Any]] = None


def _ensure_project_for_repo(
    project_id: str,
    repo_url: str,
    github_repo_name: Optional[str],
    default_branch: str,
) -> tuple[Dict[str, Any], bool]:
    """Link to an existing project with this id, or create one in /workspace.

    Returns ``(project, created)``. When creating, the GitHub repo is cloned
    into ``/workspace/<project_id>`` and a git_repositories record is written.
    Project ids are intentionally matched verbatim across installs so the same
    repo lands in the same logical project.
    """
    from app.api.projects import validate_project_path
    from app.api.git_helpers import ensure_git_repository_record
    from app.core.project_utils import get_project_working_dir
    from app.core.git_service import git_service

    existing = database.get_project(project_id)
    if existing:
        # Make sure a git repo record exists so worktrees can be created.
        working_dir = get_project_working_dir(existing)
        try:
            ensure_git_repository_record(project_id, working_dir)
        except Exception as e:  # noqa: BLE001
            logger.warning("ensure_git_repository_record failed for %s: %s", project_id, e)
        return existing, False

    # Create a fresh project rooted at /workspace/<project_id> and clone into it.
    project_path = validate_project_path(project_id, allow_create=True)
    project_path.mkdir(parents=True, exist_ok=True)

    project = database.create_project(
        project_id=project_id,
        name=github_repo_name or project_id,
        description=f"Cloud offload of {github_repo_name or repo_url}",
        path=project_id,
        settings_dict=None,
    )

    working_dir = get_project_working_dir(project)
    success, error = git_service.clone_repo(working_dir, repo_url)
    if not success:
        # Roll back the empty project so a retry can start clean.
        try:
            database.delete_project(project_id)
        except Exception:  # noqa: BLE001
            pass
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Failed to clone repository: {error}",
        )

    try:
        ensure_git_repository_record(project_id, working_dir)
    except Exception as e:  # noqa: BLE001
        logger.warning("ensure_git_repository_record failed after clone: %s", e)

    return project, True


@router.post("/intake", response_model=CloudIntakeResponse)
async def cloud_intake(
    request: Request,
    body: CloudIntakeRequest,
    token: str = Depends(require_auth),
):
    """Link/create a project, create a worktree session and start the query."""
    from app.core.worktree_manager import worktree_manager, WorktreeError
    from app.api.sessions_worktree import _migrate_uploads_sync, _detect_command_meta
    from app.core.headless_query_runner import launch_headless_query
    from app.core.sync_engine import sync_engine

    prompt = (body.prompt or "").strip()
    if not prompt:
        raise HTTPException(status_code=400, detail="Empty prompt")

    api_user = get_api_user_from_request(request)
    api_user_id: Optional[str] = api_user["id"] if api_user else None

    # 1. Ensure a project exists for the repo (clone on first use).
    try:
        project, project_created = await run_sync(
            _ensure_project_for_repo,
            body.project_id,
            body.repo_url,
            body.github_repo_name,
            body.default_branch,
        )
    except HTTPException:
        raise
    except Exception as e:  # noqa: BLE001
        logger.exception("Cloud intake project setup failed")
        raise HTTPException(status_code=500, detail=f"Project setup failed: {e}")

    project_id = project["id"]

    # 2. Resolve the branch name. When the primary doesn't pin one, derive a
    #    fresh branch from the prompt (same slug scheme as the worktree flow).
    branch_name = (body.branch_name or "").strip()
    create_new_branch = not branch_name
    if not branch_name:
        from app.api.git_ai import slugify_branch, infer_prefix
        branch_name = slugify_branch(prompt, infer_prefix(prompt))

    # Create the worktree + session. base_branch is left to the worktree
    # manager's default (the freshly-cloned checkout) unless the caller pinned
    # one — avoids guessing main vs. master for the clone.
    try:
        worktree, session = await run_sync(
            worktree_manager.create_worktree_session,
            project_id,
            branch_name,
            create_new_branch,
            body.base_branch,
            body.profile_id,
        )
    except WorktreeError as e:
        logger.warning("Cloud worktree creation failed: %s", e)
        raise HTTPException(status_code=400, detail=str(e))
    except Exception:
        logger.exception("Unexpected error creating cloud worktree session")
        raise HTTPException(status_code=500, detail="Failed to create worktree session")

    # 3. Migrate any uploaded files into the worktree.
    upload_errors: Optional[List[str]] = None
    if body.pending_upload_paths:
        try:
            upload_errors = await run_sync(
                _migrate_uploads_sync, project_id, worktree, body.pending_upload_paths
            ) or None
        except Exception as e:  # noqa: BLE001
            upload_errors = [str(e)]

    # 4. Persist the first user message + title, then launch the query.
    profile_id = body.profile_id or session.get("profile_id") or "claude-code"
    title = prompt[:100].strip()
    if len(prompt) > 100:
        title = title.rsplit(" ", 1)[0] + "..."
    msg_role, msg_meta = _detect_command_meta(prompt)

    def _persist_first_message():
        existing = database.get_session(session["id"]) or {}
        if not (existing.get("title") or "").strip() and title:
            database.update_session(session_id=session["id"], title=title)
        database.add_session_message(
            session_id=session["id"],
            role=msg_role,
            content=prompt,
            metadata=msg_meta,
            profile_id=profile_id,
        )

    await run_sync(_persist_first_message)

    try:
        await sync_engine.broadcast_user_session_created(
            user_id="admin", session_id=session["id"], source_device_id=None
        )
    except Exception as e:  # noqa: BLE001
        logger.warning("Cloud intake session broadcast failed: %s", e)

    query_started = False
    try:
        await launch_headless_query(
            prompt=prompt,
            session_id=session["id"],
            profile_id=profile_id,
            project_id=project_id,
            user_id="admin",
            api_user_id=api_user_id,
            worktree_id=worktree["id"],
            overrides=body.overrides,
        )
        query_started = True
    except Exception as e:  # noqa: BLE001
        logger.exception("Cloud intake failed to launch query: %s", e)

    return CloudIntakeResponse(
        session_id=session["id"],
        project_id=project_id,
        worktree_id=worktree["id"],
        branch_name=worktree["branch_name"],
        base_branch=worktree.get("base_branch") or body.base_branch,
        worktree_path=worktree["worktree_path"],
        status="active",
        query_started=query_started,
        project_created=project_created,
        upload_migration_errors=upload_errors,
    )


@router.post("/intake/{session_id}/message")
async def cloud_intake_message(
    request: Request,
    session_id: str,
    body: CloudMessageRequest,
    token: str = Depends(require_auth),
):
    """Run a follow-up prompt on an existing cloud-worker session."""
    from app.api.sessions_worktree import _detect_command_meta
    from app.core.headless_query_runner import launch_headless_query
    from app.core.sync_engine import sync_engine

    prompt = (body.prompt or "").strip()
    if not prompt:
        raise HTTPException(status_code=400, detail="Empty prompt")

    session = await run_sync(database.get_session, session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")

    api_user = get_api_user_from_request(request)
    api_user_id: Optional[str] = api_user["id"] if api_user else None

    profile_id = body.profile_id or session.get("profile_id") or "claude-code"
    msg_role, msg_meta = _detect_command_meta(prompt)

    await run_sync(
        database.add_session_message,
        session_id=session_id,
        role=msg_role,
        content=prompt,
        profile_id=profile_id,
    )
    try:
        await sync_engine.broadcast_message_added(
            session_id=session_id,
            message={"role": "user", "content": prompt, "profile_id": profile_id},
            source_device_id=None,
        )
    except Exception as e:  # noqa: BLE001
        logger.warning("Cloud message broadcast failed: %s", e)

    try:
        await launch_headless_query(
            prompt=prompt,
            session_id=session_id,
            profile_id=profile_id,
            project_id=session.get("project_id"),
            user_id="admin",
            api_user_id=api_user_id,
            worktree_id=session.get("worktree_id"),
            overrides=body.overrides,
        )
    except Exception as e:  # noqa: BLE001
        logger.exception("Cloud follow-up query failed: %s", e)
        raise HTTPException(status_code=500, detail="Failed to start query")

    return {"ok": True, "session_id": session_id}


@router.post("/intake/{session_id}/interrupt")
async def cloud_intake_interrupt(
    session_id: str,
    token: str = Depends(require_auth),
):
    """Interrupt a running query on a cloud-worker session."""
    from app.core.query_engine import interrupt_session
    from app.api.websocket import _active_chat_sessions, _active_sessions_lock, _stopped_sessions

    session = await run_sync(database.get_session, session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")

    _stopped_sessions.add(session_id)
    interrupted = await interrupt_session(session_id)

    # Force-cancel the task if the SDK doesn't stop quickly.
    async with _active_sessions_lock:
        task = _active_chat_sessions.get(session_id)
    if task and not task.done():
        import asyncio

        async def _safety_cancel(t, timeout=10.0):
            await asyncio.sleep(timeout)
            if not t.done():
                t.cancel()

        asyncio.ensure_future(_safety_cancel(task))

    return {"ok": True, "interrupted": interrupted}
