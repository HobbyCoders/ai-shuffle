"""
Cloud intake worker — clone/link a project and run an offloaded session.

When another AI Shuffle install (the originating device) runs a session in
"cloud" execution mode, the owning install does the real work here:
``run_cloud_intake`` links or creates a project for a GitHub repo (cloning it
into ``/workspace`` if needed), spins up a git worktree session and starts the
first query. The resulting session/worktree is an ordinary local session on this
install — the originating device just mirrors its live state back to the user.

This module is the worker-side machinery. It is invoked by the cluster intake
endpoint (``POST /api/v1/cluster/intake`` in ``app.api.cluster``), which calls
``run_cloud_intake`` directly after authenticating the peer with the shared
cluster secret. ``CloudIntakeRequest``/``CloudIntakeResponse`` are the shared
request/response shapes.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from fastapi import HTTPException, status
from pydantic import BaseModel

from app.db import database
from app.db.async_database import run_sync

logger = logging.getLogger(__name__)


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
    # Explicit "Cloud = Local on another device" contract (see module docstring
    # and run_cloud_intake). When ``use_worktree`` is True the worker forks a new
    # worktree (legacy behaviour); when False it runs a normal session directly in
    # the project's main checkout, on ``branch_name`` (checked out / created
    # there). ``create_new_branch`` makes the worker create ``branch_name`` off
    # ``base_branch`` first; ``auto_stash`` stashes a dirty checkout before
    # switching instead of failing with 409.
    use_worktree: bool = True
    create_new_branch: bool = False
    auto_stash: bool = False


class CloudIntakeResponse(BaseModel):
    session_id: str
    project_id: str
    # Worktree fields are None for the non-worktree ("run in main checkout") path.
    worktree_id: Optional[str] = None
    branch_name: str
    base_branch: Optional[str] = None
    worktree_path: Optional[str] = None
    status: str = "active"
    query_started: bool = False
    project_created: bool = False
    upload_migration_errors: Optional[List[str]] = None


def _normalize_remote_url(url: Optional[str]) -> str:
    """Normalize a git remote URL for comparison.

    Strips a single trailing ``.git`` and any trailing slash, and lower-cases
    the result so ``https://github.com/Owner/Repo.git`` and
    ``https://github.com/owner/repo`` compare equal.
    """
    if not url:
        return ""
    normalized = url.strip().rstrip("/")
    if normalized.lower().endswith(".git"):
        normalized = normalized[: -len(".git")]
    return normalized.lower()


def _instance_suffixed_id(project_id: str) -> str:
    """Derive a collision-free project id for THIS install.

    Mirrors ``app.api.projects.create_project``: append a short hash of this
    install's instance id so the same logical project id can live as a
    *separate* row per install in the shared database.
    """
    import hashlib

    from app.core.config import settings

    instance_id = settings.effective_instance_id or ""
    suffix = hashlib.sha256(instance_id.encode()).hexdigest()[:6]
    return f"{project_id}-{suffix}"


def _find_local_project_for_repo(
    project_id: str,
    repo_url: str,
) -> Optional[Dict[str, Any]]:
    """Resolve THIS install's already-present project for ``repo_url`` — no clone.

    Read-only lookup half of ``_ensure_project_for_repo`` (cases 1 & 2), shared
    with the cross-install git-state query so it can report the worker's branches
    without side effects beyond an idempotent ``git_repositories`` record upsert.
    Returns the project dict whose working dir is a real git repo *on this
    install*, or ``None`` if nothing usable is present (caller decides whether to
    clone). Resolution order:

    1. Verbatim id, only if it is a usable git repo here (single-install /
       same-machine case).
    2. This instance's own clone of the repo (``get_all_projects`` is already
       instance-filtered) matched by normalized git remote URL.
    """
    from app.api.git_helpers import ensure_git_repository_record
    from app.core.project_utils import get_project_working_dir
    from app.core.git_service import git_service

    target_remote = _normalize_remote_url(repo_url)

    # 1. Verbatim id — reuse only if its working dir is a real git repo *here*.
    #    This keeps the single-install / same-machine path unchanged. On a shared
    #    DB the row may belong to another install whose path doesn't exist here;
    #    in that case is_git_repo() is False and we fall through.
    existing = database.get_project(project_id)
    if existing and git_service.is_git_repo(get_project_working_dir(existing)):
        try:
            ensure_git_repository_record(existing["id"], get_project_working_dir(existing))
        except Exception as e:  # noqa: BLE001
            logger.warning("ensure_git_repository_record failed for %s: %s", existing["id"], e)
        return existing

    # 2. This instance's own clone of the same repo. get_all_projects() filters
    #    by effective_instance_id, so these rows are guaranteed local. Match on
    #    the git remote (from the git_repositories record or read live) and
    #    require the working dir to actually be a git repo here.
    if target_remote:
        for candidate in database.get_all_projects():
            working_dir = get_project_working_dir(candidate)
            if not git_service.is_git_repo(working_dir):
                continue
            candidate_remote = ""
            repo_record = database.get_git_repository_by_project(candidate["id"])
            if repo_record and repo_record.get("remote_url"):
                candidate_remote = _normalize_remote_url(repo_record["remote_url"])
            if not candidate_remote:
                candidate_remote = _normalize_remote_url(git_service.get_remote_url(working_dir))
            if candidate_remote and candidate_remote == target_remote:
                try:
                    ensure_git_repository_record(candidate["id"], working_dir)
                except Exception as e:  # noqa: BLE001
                    logger.warning(
                        "ensure_git_repository_record failed for %s: %s", candidate["id"], e
                    )
                return candidate

    return None


def _ensure_project_for_repo(
    project_id: str,
    repo_url: str,
    github_repo_name: Optional[str],
    default_branch: str,
) -> tuple[Dict[str, Any], bool]:
    """Resolve THIS install's project for ``repo_url``, cloning if necessary.

    Returns ``(project, created)``.

    Shared-DB rationale: cloud primary and worker installs share one Postgres
    database, but ``database.get_project(id)`` looks up purely by id and does
    NOT filter by ``instance_id``. So the verbatim ``project_id`` the primary
    sends may resolve to the *primary's* own row, whose ``path`` is a directory
    that exists only on the primary (e.g. a Windows path). Blindly reusing that
    row makes the worker treat a non-existent path as the project root and fail
    with "not a git repository". To avoid that we resolve to a project whose
    working dir is actually a git repo *on this install*, in this order:

    1-2. An already-present local project (see ``_find_local_project_for_repo``).
    3. Otherwise create + clone a fresh project, choosing an instance-suffixed
       id when the verbatim id is already held by a foreign-instance row.
    """
    from app.api.projects import validate_project_path
    from app.api.git_helpers import ensure_git_repository_record
    from app.core.project_utils import get_project_working_dir
    from app.core.git_service import git_service
    from app.core.config import settings

    # 1-2. Reuse an already-present local clone if there is one (no clone).
    found = _find_local_project_for_repo(project_id, repo_url)
    if found is not None:
        return found, False

    existing = database.get_project(project_id)

    # 3. Nothing usable on this install — create + clone a fresh project.
    #    Pick an id that won't collide with a foreign row: if the verbatim id is
    #    free use it; if it's held by a *different* instance's row, suffix it the
    #    same way create_project() does so both rows can coexist.
    target_id = project_id
    if existing:
        existing_instance = existing.get("instance_id")
        if existing_instance and existing_instance != settings.effective_instance_id:
            target_id = _instance_suffixed_id(project_id)
        # If the colliding row is ours (same/empty instance) we still can't reuse
        # it (step 1 proved it isn't a git repo here), so fall through and clone
        # into its existing working dir below rather than re-creating the row.

    target_existing = database.get_project(target_id)
    if target_existing:
        # A row already exists at the target id but isn't a git repo (otherwise
        # step 1 would have returned it). Clone into its existing dir instead of
        # calling create_project again (which would hit a PK/path conflict).
        project = target_existing
        created = False
        working_dir = get_project_working_dir(project)
    else:
        project_path = validate_project_path(target_id, allow_create=True)
        project_path.mkdir(parents=True, exist_ok=True)
        project = database.create_project(
            project_id=target_id,
            name=github_repo_name or target_id,
            description=f"Cloud offload of {github_repo_name or repo_url}",
            path=target_id,
            settings_dict=None,
        )
        created = True
        working_dir = get_project_working_dir(project)

    success, error = git_service.clone_repo(working_dir, repo_url)
    if not success:
        # Roll back only a row we freshly created so a retry can start clean.
        if created:
            try:
                database.delete_project(target_id)
            except Exception:  # noqa: BLE001
                pass
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Failed to clone repository: {error}",
        )

    try:
        ensure_git_repository_record(project["id"], working_dir)
    except Exception as e:  # noqa: BLE001
        logger.warning("ensure_git_repository_record failed after clone: %s", e)

    return project, created


def _switch_or_create_branch_sync(
    working_dir: str,
    *,
    branch_name: str,
    base_branch: Optional[str],
    create_new_branch: bool,
    auto_stash: bool,
) -> str:
    """Put the main checkout on ``branch_name`` (non-worktree cloud path).

    Mirrors the local checkout endpoint (``app/api/git.py``) so "Cloud = Local":
    switching to an existing branch (or creating one off ``base_branch`` then
    checking it out) blocks on a dirty tree with the *same* 409 message unless
    ``auto_stash`` is set, in which case it stashes first. When the requested
    branch is already current and no new branch is asked for, it is a no-op.
    Returns the branch the checkout now sits on. Runs sync (called via run_sync).
    """
    from app.core.git_service import git_service

    current = git_service.get_current_branch(working_dir)

    # Already where we want to be — no checkout, so no dirty-tree concern.
    if not create_new_branch and (not branch_name or branch_name == current):
        return current or branch_name

    # A checkout is required. Guard a dirty tree exactly like the checkout
    # endpoint (untracked files don't block; staged/modified/conflicts do).
    status_data = git_service.get_status(working_dir)
    has_uncommitted = bool(
        status_data.get("staged")
        or status_data.get("modified")
        or status_data.get("conflicts")
    )
    if has_uncommitted:
        if not auto_stash:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Cannot checkout: you have uncommitted changes. Commit or stash them first.",
            )
        if not git_service.stash_push(working_dir, f"auto-stash before checkout {branch_name}"):
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Cannot checkout: failed to stash your uncommitted changes.",
            )

    if create_new_branch:
        start_point = (base_branch or "").strip() or None
        if not git_service.create_branch(working_dir, branch_name, start_point=start_point):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Failed to create branch '{branch_name}'. It may already exist.",
            )

    if not git_service.checkout(working_dir, branch_name):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Failed to checkout '{branch_name}'. It may not exist.",
        )
    return branch_name


def _create_plain_session_sync(
    project: Dict[str, Any],
    profile_id: Optional[str],
) -> Dict[str, Any]:
    """Create a normal (non-worktree) session in the project's main checkout.

    Mirrors ``worktree_manager.create_worktree_session``'s session row, minus the
    worktree linkage, and resolves a profile the same way (request → project
    default → first profile). ``create_session`` stamps this install's
    ``instance_id`` so the cross-install relay can reach the session.
    """
    import uuid

    resolved_profile = profile_id
    if not resolved_profile:
        project_settings = project.get("settings", {}) or {}
        resolved_profile = project_settings.get("default_profile_id")
        if not resolved_profile:
            profiles = database.get_all_profiles()
            if not profiles:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="No profiles available. Please create a profile first.",
                )
            resolved_profile = profiles[0]["id"]

    session_id = f"ses-{uuid.uuid4().hex[:12]}"
    return database.create_session(
        session_id=session_id,
        profile_id=resolved_profile,
        project_id=project["id"],
        title=None,
        worktree_id=None,
        worktree_branch=None,
        worktree_path=None,
    )


async def _finalize_intake_session(
    *,
    session: Dict[str, Any],
    project_id: str,
    prompt: str,
    profile_id: str,
    api_user_id: Optional[str],
    worktree_id: Optional[str],
    overrides: Optional[Dict[str, Any]],
) -> bool:
    """Persist the first message + title, broadcast, and launch the query.

    Shared tail of both intake paths (worktree and main-checkout). Returns
    ``query_started``. ``worktree_id`` is ``None`` for the non-worktree path —
    ``resolve_working_dir`` then runs in the project's main checkout.
    """
    from app.api.sessions_worktree import _detect_command_meta
    from app.core import turn_manager
    from app.core.sync_engine import sync_engine

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

    try:
        # Submit a durable ``source=intake`` turn; ``submit`` kicks the scheduler
        # into the ONE spawn path. Called ON the loop (not via run_sync) so the
        # kick can schedule the runtime task. ``api_user_id`` rides the payload
        # so the runtime can attribute the server-originated turn.
        payload = {"api_user_id": api_user_id} if api_user_id else None
        turn_manager.submit(
            session["id"],
            "intake",
            prompt=prompt,
            profile_id=profile_id,
            project_id=project_id,
            worktree_id=worktree_id,
            overrides=overrides,
            payload=payload,
        )
        return True
    except Exception as e:  # noqa: BLE001
        logger.exception("Cloud intake failed to launch query: %s", e)
        return False


async def run_cloud_intake(
    body: CloudIntakeRequest,
    api_user_id: Optional[str],
) -> CloudIntakeResponse:
    """Shared intake worker: clone/link a project, start a session and run the
    first query. Invoked by the cluster-secret intake endpoint
    (``/api/v1/cluster/intake``). ``api_user_id`` is passed in rather than read
    here so the caller controls how the originating request was authenticated.

    Two execution paths, selected by ``body.use_worktree`` ("Cloud = Local on
    another device"):

    - **Worktree** (legacy default): fork a new worktree from the selected branch,
      or auto-name one from the prompt when no branch is pinned.
    - **Main checkout**: run a normal session directly in the project's checkout,
      first checking out / creating ``branch_name`` there — exactly like running
      locally on that device.
    """
    prompt = (body.prompt or "").strip()
    if not prompt:
        raise HTTPException(status_code=400, detail="Empty prompt")

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

    if not body.use_worktree:
        return await _run_cloud_intake_main_checkout(
            body, api_user_id, project, project_created, prompt
        )

    from app.core.worktree_manager import worktree_manager, WorktreeError
    from app.api.sessions_worktree import _migrate_uploads_sync

    # 2. Resolve the branch name. When the primary doesn't pin one, derive a
    #    fresh branch from the prompt (same slug scheme as the worktree flow). An
    #    explicit create_new_branch (named "New branch…" in the picker) forks a
    #    new branch even when a name is given; otherwise a given name is an
    #    existing branch to check out in the worktree.
    branch_name = (body.branch_name or "").strip()
    create_new_branch = bool(body.create_new_branch) or not branch_name
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
    query_started = await _finalize_intake_session(
        session=session,
        project_id=project_id,
        prompt=prompt,
        profile_id=profile_id,
        api_user_id=api_user_id,
        worktree_id=worktree["id"],
        overrides=body.overrides,
    )

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


async def _run_cloud_intake_main_checkout(
    body: CloudIntakeRequest,
    api_user_id: Optional[str],
    project: Dict[str, Any],
    project_created: bool,
    prompt: str,
) -> CloudIntakeResponse:
    """Non-worktree cloud path: run a normal session in the project's main
    checkout, after putting it on the requested branch — "Local on this device".
    """
    from app.api.sessions_worktree import _migrate_uploads_to_dir
    from app.core.project_utils import get_project_working_dir

    project_id = project["id"]
    working_dir = get_project_working_dir(project)

    # 1. Put the main checkout on the requested branch (create it if asked). An
    #    empty branch_name means "run on whatever is checked out" (the default
    #    the primary preselects from the remote's current branch). 409 on a dirty
    #    tree unless auto_stash — propagated to the primary so it can offer
    #    "Stash & switch".
    branch_name = (body.branch_name or "").strip()
    create_new_branch = bool(body.create_new_branch)
    if create_new_branch and not branch_name:
        # Defensive: a create request with no name — derive one like the
        # worktree flow rather than failing.
        from app.api.git_ai import slugify_branch, infer_prefix
        branch_name = slugify_branch(prompt, infer_prefix(prompt))

    try:
        effective_branch = await run_sync(
            _switch_or_create_branch_sync,
            working_dir,
            branch_name=branch_name,
            base_branch=body.base_branch,
            create_new_branch=create_new_branch,
            auto_stash=bool(body.auto_stash),
        )
    except HTTPException:
        raise
    except Exception as e:  # noqa: BLE001
        logger.exception("Cloud intake branch switch failed")
        raise HTTPException(status_code=500, detail=f"Branch switch failed: {e}")

    # 2. Create a normal (non-worktree) session owned by this install.
    try:
        session = await run_sync(_create_plain_session_sync, project, body.profile_id)
    except HTTPException:
        raise
    except Exception:
        logger.exception("Unexpected error creating cloud session")
        raise HTTPException(status_code=500, detail="Failed to create session")

    # 3. Migrate any uploaded files into the main checkout.
    upload_errors: Optional[List[str]] = None
    if body.pending_upload_paths:
        try:
            from pathlib import Path

            upload_errors = await run_sync(
                _migrate_uploads_to_dir,
                project_id,
                Path(working_dir),
                body.pending_upload_paths,
            ) or None
        except Exception as e:  # noqa: BLE001
            upload_errors = [str(e)]

    # 4. Persist the first message + title, then launch the query in the main
    #    checkout (worktree_id=None ⇒ resolve_working_dir uses the project dir).
    profile_id = body.profile_id or session.get("profile_id") or "claude-code"
    query_started = await _finalize_intake_session(
        session=session,
        project_id=project_id,
        prompt=prompt,
        profile_id=profile_id,
        api_user_id=api_user_id,
        worktree_id=None,
        overrides=body.overrides,
    )

    return CloudIntakeResponse(
        session_id=session["id"],
        project_id=project_id,
        worktree_id=None,
        branch_name=effective_branch or body.default_branch,
        base_branch=body.base_branch,
        worktree_path=None,
        status="active",
        query_started=query_started,
        project_created=project_created,
        upload_migration_errors=upload_errors,
    )
