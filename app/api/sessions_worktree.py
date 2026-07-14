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


def _migrate_uploads_to_dir(
    project_id: str,
    target_dir: Path,
    file_paths: List[str],
) -> List[str]:
    """Copy uploaded files from the project dir into ``target_dir``.

    Shared core of the upload-migration flow: ``target_dir`` is a worktree dir
    for the worktree flow, or the project's own main checkout for the
    non-worktree (cloud "run in main checkout") flow. Returns a list of
    human-readable error messages; a missing or mis-rooted file does not abort
    the whole migration.
    """
    from app.core.project_utils import get_project_working_dir

    errors: List[str] = []
    if not target_dir.exists():
        return [f"Target directory does not exist: {target_dir}"]

    project_dir = Path(get_project_working_dir(project_id))
    # When the target IS the project dir, the files are already in place.
    if target_dir.resolve() == project_dir.resolve():
        return errors
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
            dest = target_dir / rel
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


def _resolve_default_profile_id(project: dict, body_profile_id: Optional[str]) -> str:
    """The profile a new session uses: explicit → project default → first profile."""
    if body_profile_id:
        return body_profile_id
    project_settings = project.get("settings") or {}
    profile_id = project_settings.get("default_profile_id")
    if profile_id:
        return profile_id
    profiles = database.get_all_profiles()
    return profiles[0]["id"] if profiles else "claude-code"


async def _create_v2_branch_session(
    request: Request,
    body: CreateWorktreeSessionRequest,
    project: dict,
    api_user_id: Optional[str],
) -> WorktreeSessionResponse:
    """V2 branch-intake: create a branch session WITHOUT a legacy worktree row.

    The session records only its logical ``sessions.branch`` + origin-keyed
    ``project_v2_id``; the deterministic per-runner checkout
    (``{workspace}/checkouts/{project}/{branch}``) is materialized here via
    ``checkout_manager.ensure_checkout`` (so uploads can be migrated into it and
    the response can report the path), and re-materialized idempotently by the
    resolver at spawn. ``worktree_id``/``worktree_branch``/``worktree_path`` stay
    NULL — no md5-hash worktree dir, no ``worktrees`` row. Legacy worktree
    sessions (existing rows) keep working through the unchanged legacy path.
    """
    from app.core import project_resolver, checkout_manager

    branch_name = body.branch_name

    # 1. Resolve the origin-keyed project_v2 for this legacy project (self-heals /
    #    self-seeds a project created after the v60 collapse).
    session_stub = {"project_id": body.project_id}
    pv2 = await run_sync(project_resolver.resolve_project_v2, session_stub)
    if not pv2:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=(
                f"Could not resolve an origin-keyed project for '{body.project_id}'. "
                f"The project may not be fully initialized on this device."
            ),
        )

    # 2. Materialize the deterministic (project, branch) checkout. Idempotent with
    #    the spawn path. Threads the caller's fork intent: ``create_new_branch``
    #    mints ``branch_name`` from ``base_branch``; otherwise an existing branch is
    #    checked out (local) or recovered from origin, and a branch that exists
    #    nowhere yields None (never a silently-fabricated empty branch). None → the
    #    branch isn't materializable on this runner (no base checkout — Phase 3
    #    never clones — or a non-new branch absent locally and on origin).
    checkout_path = await run_sync(
        checkout_manager.ensure_checkout,
        pv2,
        branch_name,
        base_branch=body.base_branch,
        create_new_branch=body.create_new_branch,
    )
    if not checkout_path:
        if not body.create_new_branch:
            detail = (
                f"Branch '{branch_name}' couldn't be prepared on this device — it "
                f"isn't checked out here and wasn't found locally or on origin. "
                f"Push it from the device that has it, or create it as a new branch."
            )
        else:
            detail = (
                "This project isn't checked out on this device yet. Clone the "
                "repository here and open the project, then create the branch session."
            )
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=detail,
        )

    # 3. Create the session bound to branch + project_v2 (NO legacy worktree row).
    session_id = str(uuid.uuid4())
    profile_id = _resolve_default_profile_id(project, body.profile_id)

    def _create() -> None:
        database.create_session(
            session_id=session_id,
            profile_id=profile_id,
            project_id=body.project_id,
            title="",
            api_user_id=api_user_id,
        )
        database.set_session_branch(session_id, branch_name, pv2["id"])

    await run_sync(_create)

    # Broadcast so sidebars update immediately (mirrors the legacy path).
    try:
        from app.core.sync_engine import sync_engine
        import asyncio as _asyncio
        _asyncio.ensure_future(sync_engine.broadcast_user_session_created(
            user_id="admin",
            session_id=session_id,
            source_device_id=None,
        ))
    except Exception as e:  # noqa: BLE001
        logger.warning(f"Failed to broadcast session creation: {e}")

    query_started = False
    upload_migration_errors: Optional[List[str]] = None

    prompt = (body.prompt or "").strip()
    if prompt:
        # 1. Migrate pending uploads into the materialized checkout.
        if body.pending_upload_paths:
            try:
                upload_migration_errors = await run_sync(
                    _migrate_uploads_to_dir,
                    body.project_id,
                    Path(checkout_path),
                    body.pending_upload_paths,
                ) or None
                if upload_migration_errors:
                    logger.warning(
                        f"Upload migration partial for checkout {checkout_path}: "
                        f"{upload_migration_errors}"
                    )
            except Exception as e:  # noqa: BLE001
                logger.warning(f"Upload migration failed: {e}")
                upload_migration_errors = [str(e)]

        # 2. Persist the user message + title backfill.
        title = prompt[:100].strip()
        if len(prompt) > 100:
            title = title.rsplit(' ', 1)[0] + "..."
        msg_role, msg_meta = _detect_command_meta(prompt)

        def _persist_first_message():
            existing = database.get_session(session_id) or {}
            if not (existing.get("title") or "").strip() and title:
                database.update_session(session_id=session_id, title=title)
            database.add_session_message(
                session_id=session_id,
                role=msg_role,
                content=prompt,
                metadata=msg_meta,
                profile_id=profile_id,
            )

        await run_sync(_persist_first_message)

        # 3. Broadcast the user message + title.
        try:
            from app.core.sync_engine import sync_engine
            await sync_engine.broadcast_message_added(
                session_id=session_id,
                message={"role": "user", "content": prompt, "profile_id": profile_id},
                source_device_id=None,
            )
            if title:
                await sync_engine.broadcast_session_updated(
                    session_id=session_id,
                    updates={"title": title},
                    source_device_id=None,
                )
        except Exception as e:  # noqa: BLE001
            logger.warning(f"Failed to broadcast first message: {e}")

        # 4. Submit a durable ``source=worktree`` turn. NO worktree_id — the spawn
        #    path resolves cwd via the origin resolver off sessions.branch +
        #    project_v2_id (materializing the same checkout idempotently).
        try:
            from app.core import turn_manager
            turn_manager.submit(
                session_id,
                "worktree",
                prompt=prompt,
                profile_id=profile_id,
                project_id=body.project_id,
                overrides=body.overrides,
                payload={"api_user_id": api_user_id} if api_user_id else None,
            )
            query_started = True
        except Exception as e:  # noqa: BLE001
            logger.exception(f"Failed to submit v2 branch-session turn: {e}")
            query_started = False

    # Report the ACTUAL base honestly: a newly-minted branch was forked from
    # ``body.base_branch``; an existing/origin-recovered branch was materialized
    # from itself (NOT forked from ``base_branch``), so echo its own name rather
    # than falsely claiming a fork point.
    actual_base = body.base_branch if body.create_new_branch else branch_name

    return WorktreeSessionResponse(
        session_id=session_id,
        worktree_id=None,
        branch_name=branch_name,
        base_branch=actual_base,
        worktree_path=checkout_path,
        status="active",
        query_started=query_started,
        upload_migration_errors=upload_migration_errors,
    )


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

    Since Phase-3 PR-B this always takes the V2 branch-intake path (the
    ``v2_projects`` kill switch was removed): the new session records only
    ``sessions.branch`` + ``project_v2_id`` and materializes a deterministic
    ``{workspace}/checkouts/{project}/{branch}`` checkout — no legacy
    ``worktrees`` row / md5-hash dir. EXISTING legacy worktree sessions keep
    resolving through the resolver's legacy ladder in ``query_options``.
    """
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

    # V2 branch-intake is the ONLY path (Phase-3 PR-B removed the ``v2_projects``
    # kill switch): a NEW branch session records only sessions.branch +
    # project_v2_id and materializes a deterministic checkout — no legacy
    # worktrees row / md5-hash dir. EXISTING legacy worktree sessions keep
    # resolving through the resolver's legacy ladder in ``query_options``.
    return await _create_v2_branch_session(request, body, project, api_user_id)
