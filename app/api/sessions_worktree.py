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
from app.api.common import ATTACHMENTS_NEED_LOCAL_CHECKOUT, require_origin_access
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


def _resolve_default_profile_id(project: Optional[dict], body_profile_id: Optional[str]) -> str:
    """The profile a new session uses: explicit → project default → first profile.

    No legacy project dict (remote-only compose by ``project_v2_id``) skips
    project settings and uses the first profile.
    """
    if body_profile_id:
        return body_profile_id
    if project:
        project_settings = project.get("settings") or {}
        profile_id = project_settings.get("default_profile_id")
        if profile_id:
            return profile_id
    profiles = database.get_all_profiles()
    return profiles[0]["id"] if profiles else "claude-code"


async def _persist_and_broadcast_first_message(
    session_id: str, prompt: str, profile_id: str
) -> None:
    """Store the compose-time first message + backfill the title, then broadcast.

    Shared verbatim by the local and remote branch-session paths so the two can
    never drift on what a new worktree session's history looks like.
    """
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


def _broadcast_session_created(session_id: str) -> None:
    """Fire-and-forget sidebar refresh for a freshly created session."""
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


async def _preflight_remote_target(
    target_instance_id: str, pv2: dict
) -> None:
    """Refuse a device pick that cannot possibly run this branch session (D4).

    DB-ONLY and BEFORE anything is created — a session pinned to a device that
    can't materialize its checkout is stranded, not merely slow. Two gates:

      * the target is a registered install that is currently online;
      * the target has an ACTIVE **BASE** checkout of this project, asked via
        the shared ``checkouts.has_active_base_checkout`` predicate — the SAME
        function the cluster git-state API uses for its per-device ``eligible``
        flag, so the composer badge and this gate can never disagree (W3-2).
        Deliberately NOT ``capable_runners``: that helper matches ANY active
        checkout row for the project, including a branch-only worktree row, and
        Phase 3 never clones — creating a branch needs a base checkout to fork
        from, so a branch-only match would queue an op that can only fail.

    Raises HTTPException(409) with wording matching the local 409 family.
    """
    from app.db import checkouts as checkouts_db

    install = await run_sync(database.get_install, target_instance_id)
    if not install:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Unknown device — pick another device.",
        )
    device = install.get("device_name") or target_instance_id
    if not install.get("online"):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"{device} is offline. Bring it online or pick another device.",
        )

    eligible = await run_sync(
        checkouts_db.has_active_base_checkout,
        pv2["id"],
        target_instance_id,
    )
    if not eligible:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"This project isn't checked out on {device} yet. Clone the "
                f"repository there and open the project, then create the branch "
                f"session."
            ),
        )


async def _create_remote_branch_session(
    body: CreateWorktreeSessionRequest,
    project: Optional[dict],
    api_user_id: Optional[str],
    pv2: dict,
    target_instance_id: str,
) -> WorktreeSessionResponse:
    """Create a branch session that will materialize on ANOTHER install (D4).

    Nothing is written to this machine's disk: no ``ensure_checkout``, no upload
    migration. Instead the session is created PINNED to the target (D1), a
    durable ``ensure_worktree`` git op is queued for it (D5 — that row is the
    carrier of the ``create_new_branch`` intent), and any pending uploads are
    lifted into D8 blobs attached to the first turn. The first turn is submitted
    ``no_kick`` + a claim-loop wake, exactly like the WS remote path.

    ``worktree_path`` comes back None — the path will exist on the target, not
    here, and inventing one would be a lie.
    """
    from app.core import turn_claim, turn_manager, upload_transfer
    from app.db import git_ops as git_ops_db

    branch_name = body.branch_name

    # 1. Preflight BEFORE creating anything.
    await _preflight_remote_target(target_instance_id, pv2)

    # 2. Mint the session id up front so the upload lift (which can fail the
    #    whole request) happens before a session row exists — a refused create
    #    must leave nothing behind.
    session_id = str(uuid.uuid4())
    profile_id = _resolve_default_profile_id(project, body.profile_id)
    prompt = (body.prompt or "").strip()

    # Blobs are carried BY A TURN (D8), so they are only lifted when this
    # request also supplies the first prompt. The promptless compose flow
    # (createWorktree, which POSTs first and sends the message over the WS
    # afterwards) carries its paths on that follow-up query instead — lifting
    # them here would orphan them with no turn to attach them to.
    upload_blob_ids: List[str] = []
    if body.pending_upload_paths and prompt:
        try:
            upload_blob_ids = await run_sync(
                upload_transfer.lift_paths_to_blobs,
                body.pending_upload_paths,
                project,
                session_id,
                pv2["id"],
            )
        except Exception as e:  # noqa: BLE001 — refuse, never half-create
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=(
                    f"Couldn't send your attachments to the other device: {e}"
                ),
            ) from e

    # 3. Create the session OWNED BY + PINNED TO the target (D1) and bind it to
    #    the branch, exactly as the local path does.
    def _create() -> None:
        database.create_session(
            session_id=session_id,
            profile_id=profile_id,
            project_id=body.project_id or None,
            project_v2_id=pv2["id"],
            title="",
            api_user_id=api_user_id,
            is_active=True,
            target_instance_id=target_instance_id,
        )
        database.set_session_branch(session_id, branch_name, pv2["id"])

    await run_sync(_create)
    _broadcast_session_created(session_id)

    # 4. Queue the checkout work for the target. This row carries
    #    ``create_new_branch`` — a brand-new branch exists on no disk anywhere,
    #    so the intent has to travel durably or the session can never spawn.
    await run_sync(
        git_ops_db.insert_op,
        target_instance_id,
        "ensure_worktree",
        project_v2_id=pv2["id"],
        params={
            "branch": branch_name,
            "base_branch": body.base_branch,
            "create_new_branch": bool(body.create_new_branch),
        },
    )

    query_started = False
    if prompt:
        await _persist_and_broadcast_first_message(session_id, prompt, profile_id)
        try:
            payload: dict = {}
            if api_user_id:
                payload["api_user_id"] = api_user_id
            if upload_blob_ids:
                payload["upload_blob_ids"] = upload_blob_ids
            turn_manager.submit(
                session_id,
                "worktree",
                prompt=prompt,
                # The 'claude-code' no-profile sentinel is install-local and must
                # never ride a turn another install executes.
                profile_id=None if profile_id == "claude-code" else profile_id,
                project_id=body.project_id or None,
                overrides=body.overrides,
                payload=payload or None,
                no_kick=True,
            )
            await run_sync(turn_claim.notify_turns_changed, session_id)
            query_started = True
        except Exception as e:  # noqa: BLE001
            logger.exception(f"Failed to submit remote branch-session turn: {e}")
            query_started = False

    actual_base = body.base_branch if body.create_new_branch else branch_name
    return WorktreeSessionResponse(
        session_id=session_id,
        worktree_id=None,
        branch_name=branch_name,
        base_branch=actual_base,
        # The checkout lives on the target's disk; there is no local path.
        worktree_path=None,
        status="active",
        query_started=query_started,
        upload_migration_errors=None,
    )


async def _create_v2_branch_session(
    request: Request,
    body: CreateWorktreeSessionRequest,
    project: Optional[dict],
    api_user_id: Optional[str],
    pv2: Optional[dict] = None,
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
    from app.core.config import settings

    branch_name = body.branch_name

    # Remote-only compose has no local project dir; D8 lift is project-dir-relative.
    if body.pending_upload_paths and not (body.project_id and project):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=ATTACHMENTS_NEED_LOCAL_CHECKOUT,
        )

    # 0. Composer device pick (D4). A target that is not us takes the remote
    #    path: nothing is materialized here, a durable git op carries the
    #    checkout work to the target, and uploads travel as D8 blobs.
    target_instance_id = (body.target_instance_id or "").strip() or None
    if target_instance_id == settings.effective_instance_id:
        target_instance_id = None

    # 1. Origin-keyed project_v2. The HTTP path passes the row already
    #    resolved by ``require_origin_access``; direct callers still look it
    #    up. ``project_v2_id`` is a direct get (no local ``projects`` row).
    #    ``project_id`` self-heals / self-seeds a post-v60 project.
    if pv2 is None:
        if body.project_v2_id:
            from app.db import projects_v2 as projects_v2_db

            pv2 = await run_sync(projects_v2_db.get_project_v2, body.project_v2_id)
            if not pv2:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail=f"Project not found: {body.project_v2_id}",
                )
        else:
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

    if target_instance_id:
        return await _create_remote_branch_session(
            body, project, api_user_id, pv2, target_instance_id
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
            project_id=body.project_id or None,
            project_v2_id=pv2["id"],
            title="",
            api_user_id=api_user_id,
            is_active=True,
        )
        database.set_session_branch(session_id, branch_name, pv2["id"])

    await run_sync(_create)

    # Broadcast so sidebars update immediately (mirrors the legacy path).
    _broadcast_session_created(session_id)

    query_started = False
    upload_migration_errors: Optional[List[str]] = None

    # Migrate pending uploads into the materialized checkout — BEFORE any turn
    # submit and regardless of whether a prompt was supplied (a promptless
    # compose still needs its attachments to land, else real data loss).
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

    prompt = (body.prompt or "").strip()
    if prompt:
        await _persist_and_broadcast_first_message(session_id, prompt, profile_id)

        # Submit a durable ``source=worktree`` turn. NO worktree_id — the spawn
        # path resolves cwd via the origin resolver off sessions.branch +
        # project_v2_id (materializing the same checkout idempotently).
        try:
            from app.core import turn_manager
            turn_manager.submit(
                session_id,
                "worktree",
                prompt=prompt,
                profile_id=profile_id,
                project_id=body.project_id or None,
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
    # XOR + origin access. Does not 404 on get_project when only
    # ``project_v2_id`` is set (remote-only compose).
    api_user = get_api_user_from_request(request)
    api_user_id: Optional[str] = api_user["id"] if api_user else None
    access = await run_sync(
        require_origin_access,
        api_user=api_user,
        project_id=body.project_id,
        project_v2_id=body.project_v2_id,
    )
    project = access["legacy"]
    if body.pending_upload_paths and not project:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=ATTACHMENTS_NEED_LOCAL_CHECKOUT,
        )

    # V2 branch-intake is the ONLY path (Phase-3 PR-B removed the ``v2_projects``
    # kill switch): a NEW branch session records only sessions.branch +
    # project_v2_id and materializes a deterministic checkout — no legacy
    # worktrees row / md5-hash dir. EXISTING legacy worktree sessions keep
    # resolving through the resolver's legacy ladder in ``query_options``.
    return await _create_v2_branch_session(
        request, body, project, api_user_id, access["project_v2"]
    )
