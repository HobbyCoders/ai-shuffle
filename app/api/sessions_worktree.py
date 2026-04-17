"""
Worktree session creation endpoint.

Extracted from sessions.py for maintainability. The sub-router is
included in the main sessions router for backward compatibility.
"""

import shutil
import uuid
from pathlib import Path
from typing import List, Optional, Tuple

from fastapi import APIRouter, HTTPException, Depends, status, Request

from app.db import database
from app.db.async_database import run_sync
from app.api.auth import require_auth, get_api_user_from_request
from app.api.sessions_helpers import (
    CreateWorktreeSessionRequest,
    WorktreeSessionResponse,
)

import logging
logger = logging.getLogger(__name__)

# Sub-router — included by the main sessions router (no prefix needed)
worktree_router = APIRouter(tags=["Sessions"])


def _migrate_uploads_sync(
    project_id: str,
    worktree: dict,
    file_paths: List[str],
) -> List[str]:
    """Copy uploaded files from project dir into the new worktree.

    Mirrors the logic of ``POST /projects/{id}/git/migrate-uploads`` so the
    auto-worktree flow can migrate uploads server-side without a second
    round-trip. Returns a list of human-readable error messages; a missing
    or mis-rooted file does not abort the whole migration.
    """
    from app.core.project_utils import get_project_working_dir, get_worktree_working_dir

    errors: List[str] = []
    worktree_dir = Path(get_worktree_working_dir(worktree))
    if not worktree_dir.exists():
        return [f"Worktree directory does not exist: {worktree_dir}"]

    project_dir = Path(get_project_working_dir(project_id))
    for file_path in file_paths:
        try:
            src = Path(file_path)
            if not src.exists():
                errors.append(f"File not found: {file_path}")
                continue
            try:
                rel = src.relative_to(project_dir)
            except ValueError:
                errors.append(f"File not in project directory: {file_path}")
                continue
            dest = worktree_dir / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(str(src), str(dest))
        except Exception as e:  # noqa: BLE001
            errors.append(f"Failed to copy {file_path}: {e}")
    return errors


def _detect_command_meta(prompt: str) -> Tuple[str, Optional[dict]]:
    """Classify a user prompt as a regular message or a slash command.

    Matches the behaviour of ``ChatWebSocketHandler.handle_query`` so the
    message row stored here is identical to one stored via WebSocket.
    """
    stripped = prompt.strip()
    if stripped.startswith("/"):
        parts = stripped.split(None, 1)
        return "command", {
            "command_name": parts[0],
            "command_args": parts[1] if len(parts) > 1 else "",
        }
    return "user", None


@worktree_router.post("/with-worktree", response_model=WorktreeSessionResponse)
async def create_session_with_worktree(
    request: Request,
    body: CreateWorktreeSessionRequest,
    token: str = Depends(require_auth)
):
    """
    Create a new session with an associated git worktree.

    Optionally kicks off the first Claude query server-side when ``prompt``
    is provided. The query is spawned as a detached task that persists
    state via ``sync_engine`` and the live session store, so the user can
    disconnect (lock phone, switch session, close tab) during creation
    without cancelling the work.
    """
    from app.core.worktree_manager import worktree_manager, WorktreeError

    # Verify project exists and user has access
    project = await run_sync(database.get_project, body.project_id)
    if not project:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Project not found: {body.project_id}"
        )

    # Check API user restrictions
    api_user = get_api_user_from_request(request)
    api_user_id: Optional[str] = api_user["id"] if api_user else None
    if api_user and api_user.get("project_id"):
        if api_user["project_id"] != body.project_id:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Access denied to this project"
            )

    # Create worktree and session
    try:
        worktree, session = worktree_manager.create_worktree_session(
            project_id=body.project_id,
            branch_name=body.branch_name,
            create_new_branch=body.create_new_branch,
            base_branch=body.base_branch,
            profile_id=body.profile_id
        )
    except WorktreeError as e:
        logger.warning(f"Worktree creation failed: {e}")
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(e)
        )
    except Exception:
        logger.exception("Unexpected error creating worktree session")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to create worktree session"
        )

    logger.debug(f"Created worktree session: {session['id']} with worktree: {worktree['id']}")

    # Broadcast to all connected devices so sidebars update immediately.
    # Without this, sessions created via REST (not WS) never appear in
    # the sidebar until the user manually refreshes.
    try:
        from app.core.sync_engine import sync_engine
        import asyncio as _asyncio
        _asyncio.ensure_future(sync_engine.broadcast_user_session_created(
            user_id="admin",
            session_id=session["id"],
            source_device_id=None
        ))
    except Exception as e:
        logger.warning(f"Failed to broadcast session creation: {e}")

    # ------------------------------------------------------------------
    # Optional: kick off the first query server-side
    # ------------------------------------------------------------------
    query_started = False
    upload_migration_errors: Optional[List[str]] = None

    prompt = (body.prompt or "").strip()
    if prompt:
        # 1. Migrate pending uploads into the new worktree (best-effort —
        #    errors are surfaced in the response but don't block the query).
        if body.pending_upload_paths:
            try:
                upload_migration_errors = await run_sync(
                    _migrate_uploads_sync,
                    body.project_id,
                    worktree,
                    body.pending_upload_paths,
                ) or None
                if upload_migration_errors:
                    logger.warning(
                        f"Upload migration partial for worktree {worktree['id']}: "
                        f"{upload_migration_errors}"
                    )
            except Exception as e:  # noqa: BLE001
                logger.warning(f"Upload migration failed: {e}")
                upload_migration_errors = [str(e)]

        # 2. Persist the user message (atomic with session title backfill).
        profile_id = body.profile_id or session.get("profile_id") or "claude-code"
        title = prompt[:100].strip()
        if len(prompt) > 100:
            title = title.rsplit(' ', 1)[0] + "..."

        msg_role, msg_meta = _detect_command_meta(prompt)

        def _persist_first_message():
            # Backfill the title (worktree_manager creates the session with
            # an empty title) and store the user message.
            existing = database.get_session(session["id"]) or {}
            if not (existing.get("title") or "").strip() and title:
                database.update_session(
                    session_id=session["id"],
                    title=title,
                )
            database.add_session_message(
                session_id=session["id"],
                role=msg_role,
                content=prompt,
                metadata=msg_meta,
                profile_id=profile_id,
            )

        await run_sync(_persist_first_message)

        # 3. Broadcast the user message + title update so any already-
        #    connected devices (or devices that reconnect mid-run) see
        #    the prompt without waiting for a poll.
        try:
            from app.core.sync_engine import sync_engine
            await sync_engine.broadcast_message_added(
                session_id=session["id"],
                message={
                    "role": "user",
                    "content": prompt,
                    "profile_id": profile_id,
                },
                source_device_id=None,
            )
            if title:
                await sync_engine.broadcast_session_updated(
                    session_id=session["id"],
                    updates={"title": title},
                    source_device_id=None,
                )
        except Exception as e:  # noqa: BLE001
            logger.warning(f"Failed to broadcast first message: {e}")

        # 4. Launch run_query in a detached task. The task is registered in
        #    _active_chat_sessions so /stop and reconnect-attach work.
        try:
            from app.core.headless_query_runner import launch_headless_query
            await launch_headless_query(
                prompt=prompt,
                session_id=session["id"],
                profile_id=profile_id,
                project_id=body.project_id,
                user_id="admin",
                api_user_id=api_user_id,
                worktree_id=worktree["id"],
                overrides=body.overrides,
            )
            query_started = True
        except Exception as e:  # noqa: BLE001
            logger.exception(f"Failed to launch headless query: {e}")
            # Don't fail the whole endpoint — session + worktree were created
            # successfully, frontend can fall back to WS-based query.
            query_started = False

    return WorktreeSessionResponse(
        session_id=session["id"],
        worktree_id=worktree["id"],
        branch_name=worktree["branch_name"],
        base_branch=worktree.get("base_branch") or body.base_branch,
        worktree_path=worktree["worktree_path"],
        status="active",
        query_started=query_started,
        upload_migration_errors=upload_migration_errors,
    )
