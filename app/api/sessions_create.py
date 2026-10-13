"""Create a session before its first message.

A typed first message creates its session over the chat WebSocket, and a
branch session can be created up front with ``POST /sessions/with-worktree``.
This is the same up-front create for a session that runs in the project's own
checkout (or a projectless chat): the composer's call button uses it to make a
normal conversation and then start a call on it. It never creates a voice-lane
session (``mode='voice'``); the call attaches to the conversation afterwards.
"""

import logging
import uuid
from typing import Optional

from fastapi import Depends, HTTPException, Request, status
from pydantic import BaseModel

from app.api.auth import get_api_user_from_request, require_auth
from app.api.common import require_origin_access
from app.api.sessions_helpers import resolve_api_user_profile
from app.api.sessions_worktree import (
    _broadcast_session_created,
    _resolve_default_profile_id,
    persist_promptless_run_binding,
)
from app.core.models import QueryOverrides
from app.core.session_modes import normalize_session_mode
from app.db import database
from app.db.async_database import run_sync

logger = logging.getLogger(__name__)


class CreateSessionRequest(BaseModel):
    """The new-session composer's selections, without a first message."""
    # At most one of the two; neither creates a projectless session, as a
    # typed first message does.
    project_id: Optional[str] = None
    project_v2_id: Optional[str] = None
    profile_id: Optional[str] = None
    # Sidepanel lane: 'code' or 'chat'. 'voice' is refused.
    mode: Optional[str] = None
    # An existing legacy worktree row of this project to run in.
    worktree_id: Optional[str] = None
    overrides: Optional[QueryOverrides] = None


class CreateSessionResponse(BaseModel):
    session_id: str
    mode: str


async def create_empty_session(
    request: Request,
    body: CreateSessionRequest,
    token: str = Depends(require_auth),
):
    """Create an empty session on this install from the composer's picks.

    Mirrors the first-message create on the chat WebSocket (origin access,
    agent check, model/effort/permission binding stamped at birth, lane) but
    starts no turn. The session runs on this install: a call can only attach to
    a conversation owned here, so there is no device pick.
    """
    raw_mode = (body.mode or "").strip().lower()
    if raw_mode == "voice":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="New sessions are conversations; start a call on one instead.",
        )
    mode = normalize_session_mode(raw_mode)

    api_user = get_api_user_from_request(request)
    api_user_id = (api_user or {}).get("id")

    project = None
    project_v2_id = None
    if body.project_id or body.project_v2_id:
        access = await run_sync(
            require_origin_access,
            api_user=api_user,
            project_id=body.project_id,
            project_v2_id=body.project_v2_id,
        )
        project = access["legacy"]
        project_v2_id = (access.get("project_v2") or {}).get("id")
    project_id = (project or {}).get("id")

    worktree_fields = {}
    if body.worktree_id:
        if not project_id:
            raise HTTPException(400, "A worktree needs its project_id.")
        worktree = await run_sync(database.get_worktree, body.worktree_id)
        repository = await run_sync(database.get_git_repository_by_project, project_id)
        if (
            not worktree
            or not repository
            or worktree.get("repository_id") != repository.get("id")
        ):
            raise HTTPException(404, "Worktree not found in this project.")
        worktree_fields = {
            "worktree_id": body.worktree_id,
            "worktree_branch": worktree.get("branch"),
            "worktree_path": worktree.get("path") or worktree.get("worktree_path"),
        }

    # Assigned agents bind the agent before the project-default fallback.
    requested_profile = resolve_api_user_profile(api_user, body.profile_id)
    profile_id = await run_sync(_resolve_default_profile_id, project, requested_profile)
    if not await run_sync(database.get_profile, profile_id):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="That agent no longer exists — pick another one.",
        )

    overrides = body.overrides.model_dump(exclude_none=True) if body.overrides else {}
    session_id = str(uuid.uuid4())

    def _create() -> None:
        from app.core.model_registry import offered_context_window
        from app.db.base import transaction

        # A window the model does not offer is ignored, never stamped as the
        # new chat's pin (the picker's check).
        window = offered_context_window(
            {"model": overrides.get("model"), "profile_id": profile_id},
            overrides.get("context_window"),
        )
        with transaction() as txn:
            database.create_session(
                session_id=session_id,
                profile_id=profile_id,
                project_id=project_id,
                api_user_id=api_user_id,
                title="",
                is_active=True,
                project_v2_id=project_v2_id,
                model=overrides.get("model"),
                effort=overrides.get("effort"),
                permission_mode=overrides.get("permission_mode"),
                context_window=window,
                fast=overrides.get("fast"),
                mode=mode,
                _conn=txn,
                **worktree_fields,
            )
            # A paired device keeps the chat it created (#1877).
            from app.core.mobile_tool_identity import record_creator
            record_creator(session_id, request, conn=txn)
        if mode != "chat" and overrides.get("workspace_project_ids") is not None:
            persist_promptless_run_binding(
                session_id,
                {"workspace_project_ids": overrides["workspace_project_ids"]},
            )

    await run_sync(_create)

    # Bind the V2 project now when only the legacy id resolved it, as the
    # WebSocket create does. Best-effort: the send path self-heals it later.
    if project_id and not project_v2_id:
        try:
            from app.core import project_resolver
            await run_sync(
                project_resolver.resolve_project_v2,
                {"project_id": project_id, "id": session_id},
            )
        except Exception:  # noqa: BLE001
            logger.debug("V2 project bind failed for new session %s", session_id, exc_info=True)

    _broadcast_session_created(session_id)
    return CreateSessionResponse(session_id=session_id, mode=mode)
