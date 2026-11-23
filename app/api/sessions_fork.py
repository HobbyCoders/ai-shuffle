"""
Session fork endpoint.

Extracted from sessions.py for maintainability. The sub-router is
included in the main sessions router for backward compatibility.
"""

import asyncio
import re
import uuid
from typing import Any, Callable, Optional

from fastapi import APIRouter, HTTPException, Depends, status, Request

from app.db import database
from app.db.async_database import run_sync
from app.core.mobile_tool_identity import record_creator
from app.core.model_speed import stored_pair
from app.api.auth import require_auth, get_api_user_from_request
from app.core.project_utils import get_project_working_dir
from app.api.sessions_helpers import (
    require_transcript_session,
    ForkSessionRequest,
    ForkSessionResponse,
)

import logging
logger = logging.getLogger(__name__)

# Sub-router — included by the main sessions router (no prefix needed)
fork_router = APIRouter(tags=["Sessions"])


def _fork_inherited_fields(original_session: dict, checkout=None) -> dict:
    """The parent-session fields a fork must carry over.

    The fork inherits the run binding (model / effort / permission_mode /
    context_window / fast / provider), or the composer would show registry
    defaults instead of what the parent was running. With ``checkout`` (the
    fork's own worktree, see ``_fork_checkout``) it is bound there; without
    one (no project, or a folder that is not a Git repository) it shares the
    parent's binding (worktree_id / legacy columns), or its cwd would resolve
    to the project's base checkout instead of the parent's folder.
    """
    if checkout is not None:
        own = {"project_v2_id": checkout.project_v2_id, "worktree_id": None,
               "worktree_branch": checkout.branch, "worktree_path": checkout.path}
    else:
        own = {"project_v2_id": original_session.get("project_v2_id"),
               "worktree_id": original_session.get("worktree_id"),
               "worktree_branch": original_session.get("worktree_branch"),
               "worktree_path": original_session.get("worktree_path")}
    return {
        # The chat lane: a chat-lane chat has no project by design; forked into
        # the code lane it would ask for one before it could take a file. A
        # voice row's fork is a normal conversation, with no call bound to it.
        "mode": "chat" if original_session.get("mode") == "chat" else None,
        "provider_id": original_session.get("provider_id"),
        "derive_provider_from_profile": False,
        "model": original_session.get("model"),
        "effort": original_session.get("effort"),
        "permission_mode": original_session.get("permission_mode"),
        "context_window": original_session.get("context_window"),
        # The parent's pair at the tier it runs (never a stale refinement).
        **stored_pair(prior=original_session),
        **own,
    }


#: A fork's own branch: ``chore/`` is a name an origin restricted to fix/,
#: feat/ and chore/ branches accepts.
FORK_BRANCH_PREFIX = "chore/fork-"

#: Untracked files carried into a fork's checkout, at most (count, bytes):
#: past either, none are copied, so a stray build folder can't stall a fork.
_CARRY_UNTRACKED_LIMITS = (2000, 200 * 1024 * 1024)


def fork_branch(new_session_id: str) -> str:
    return f"{FORK_BRANCH_PREFIX}{new_session_id.removeprefix('ses-')[:12]}"


def _fork_checkout(original_session: dict, new_session_id: str):
    """``(checkout, parent_path)``: the fork's own worktree and branch, or
    ``(None, None)`` when it has nothing to fork; ``parent_path`` is the
    folder whose uncommitted changes ``_carry_uncommitted`` copies over once
    the fork succeeded (while the checkout is clean, a failed fork can still
    remove it).

    Two chats in one folder switch and commit over each other, and a fork
    moved to another worker would push to its parent's branch from a second
    checkout. So a fork of a project chat gets a ``chore/fork-<id>`` worktree
    made from the parent's current commit, with its uncommitted changes
    carried over. A chat with no project, or one whose folder is not a Git
    repository, has no branch to fork: the fork shares its folder, as before.
    """
    if not (original_session.get("project_v2_id") or original_session.get("project_id")):
        return None, None
    from app.core import project_resolver
    from app.core.git_service import git_service
    from app.core.worker_checkout import WorkerCheckoutError, prepare_worker_checkout

    parent_path = None
    base = None
    try:
        parent_path = project_resolver.resolve_session_cwd(dict(original_session))
        if parent_path:
            head = git_service._run_git(parent_path, ["rev-parse", "HEAD"], timeout=5)
            if head.returncode == 0 and head.stdout.strip():
                base = head.stdout.strip()
    except Exception:  # provisioning finds the checkout itself
        logger.debug("Could not read the parent's commit for fork %s", new_session_id, exc_info=True)
    try:
        # Pinned to the parent's commit: the fork starts where its history
        # left the files, not at a newer default branch.
        checkout = prepare_worker_checkout(
            original_session, new_session_id, original_session.get("profile_id"),
            base_commit=base, share_read_only=False, branch=fork_branch(new_session_id),
        )
    except WorkerCheckoutError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT,
                            f"Could not give the fork its own checkout: {exc} Nothing was forked.") from exc
    return checkout, (parent_path if base else None)


def _carry_uncommitted(parent_path: str, fork_path: str, new_session_id: str) -> None:
    """Copy the parent's uncommitted changes into the fork's new checkout.

    ``git stash create`` records tracked changes as a commit without touching
    the parent's files or index; applying it in the fork (a worktree of the
    same repository) reproduces them. Untracked, non-ignored files are copied
    as files. Best effort: the fork still works from the committed state.
    """
    import shutil
    from pathlib import Path

    from app.core.git_service import git_service

    try:
        stash = git_service._run_git(parent_path, ["stash", "create"], timeout=30)
        sha = stash.stdout.strip() if stash.returncode == 0 else ""
        if sha:
            applied = git_service._run_git(fork_path, ["stash", "apply", sha], timeout=30)
            if applied.returncode:
                logger.warning("Fork %s: could not apply the parent's uncommitted changes: %s",
                               new_session_id, (applied.stderr or "").strip()[:300])
        listed = git_service._run_git(parent_path, ["ls-files", "--others", "--exclude-standard", "-z"],
                                      timeout=30)
        if listed.returncode:
            return
        names = [name for name in listed.stdout.split("\0") if name]
        source, dest = Path(parent_path), Path(fork_path)
        files = [source / name for name in names if (source / name).is_file()]
        max_count, max_bytes = _CARRY_UNTRACKED_LIMITS
        if len(files) > max_count or sum(f.stat().st_size for f in files) > max_bytes:
            logger.warning("Fork %s: %d untracked files are too many to copy; left behind",
                           new_session_id, len(files))
            return
        for file in files:
            target = dest / file.relative_to(source)
            if target.exists():
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(file, target)
    except Exception:
        logger.warning("Fork %s: could not carry the parent's uncommitted changes", new_session_id, exc_info=True)


def _discard_fork_checkout(checkout) -> None:
    """Remove a fork's checkout when the fork itself failed."""
    if checkout is None:
        return
    from app.core.worker_checkout import discard_unused_worker_checkout

    try:
        discard_unused_worker_checkout(checkout)
    except Exception:  # keep the fork's own error
        logger.warning("Could not reclaim the unused fork checkout %s", checkout.path, exc_info=True)


def _fork_logical_branch(original_session: dict, checkout) -> str:
    """The fork's ``sessions.branch``: its own checkout's, else the parent's."""
    if checkout is not None:
        return checkout.branch
    return (original_session.get("branch") or "").strip()


def _restore_fork_branch(new_session_id: str, branch: str) -> None:
    """Re-apply the fork's logical ``sessions.branch`` after create_session.

    ``create_session`` has no ``branch`` parameter and stamps NULL; without
    this a fork of a v2 branch session would resolve to the base checkout. Same
    targeted write ``set_session_project``-style helpers use.
    """
    from app.db.base import get_cursor, get_db
    from app.db.compat import ph

    with get_db() as conn:
        cursor = get_cursor(conn)
        cursor.execute(
            f"UPDATE sessions SET branch = {ph()} WHERE id = {ph()}",
            (branch, new_session_id),
        )


def _seed_cli_fork_events(new_session_id: str, new_sdk_session_id: str, working_dir: str,
                          entries: list) -> None:
    """Build a CLI fork's app event log from the transcript lines it was given.

    A fork short of the head copies no events (see ``_copy_app_event_log``),
    and the read-path backfill cannot fill them in yet: the store holds
    nothing for the new transcript and heals from a JSONL file only once it
    has sat idle for a minute. A chat opened before then is empty, and a turn
    sent in it ends backfill eligibility for good. Parse the very lines that
    were written, through the store parser's own transform, instead.
    Non-fatal: on failure the fork stays usable and the old path still runs.
    """
    try:
        from app.core.history_backfill import convert_messages_to_events
        from app.core.jsonl_parser import parse_session_history_from_store
        from app.db import session_events as session_events_db

        # A copied log of cancellations only carries no conversation: seed it.
        if not session_events_db.can_backfill_events(new_session_id):
            return  # The whole-session copy already seeded it.
        messages, _usage = parse_session_history_from_store(
            new_sdk_session_id,
            working_dir,
            lambda _sid, subpath=None: list(entries) if subpath is None else [],
            use_cache=False,
            store_version_loader=lambda _sid: (len(entries), len(entries)),
        )
        events = convert_messages_to_events(new_session_id, messages)
        if events:
            session_events_db.append_backfill_events(new_session_id, events)
    except Exception:  # noqa: BLE001
        logger.warning("Could not seed the event log of fork %s", new_session_id, exc_info=True)


def _copy_app_event_log(source_session_id: str, dest_session_id: str, *, at_head: bool) -> None:
    """Copy the V2 chat event log onto the fork when the fork is the whole session.

    Mid-session forks keep the existing backfill reconstruction (the live log
    has no node uuids to cut on). A failed copy is non-fatal: the fork stays
    driveable and backfill will seed a lossy single-turn log instead.
    """
    if not at_head:
        return
    try:
        from app.db import session_events as session_events_db

        copied = session_events_db.copy_session_events(source_session_id, dest_session_id)
        logger.debug(
            "Copied %d session_events from %s to fork %s",
            copied, source_session_id, dest_session_id,
        )
    except Exception:
        logger.exception(
            "Failed to copy session_events from %s to fork %s",
            source_session_id, dest_session_id,
        )


def _reseed_fork_cli(
    source_sdk_session_id: str,
    new_session_id: str,
    new_sdk_session_id: str,
    resolve_working_dir: Callable[[dict], str],
) -> None:
    """Materialize a Fluid fork's copied path as its own Claude Code transcript.

    Best effort, like the reseed after a rewind: the node copy is the source of
    truth, and a failed reseed only means the next delegated turn starts a new
    Claude Code conversation.
    """
    from app.core.provider.fluid_transcript import (
        delegated_window_for,
        get_cli_session_id,
        reseed_delegated_cli_session,
    )

    try:
        if not get_cli_session_id(source_sdk_session_id):
            return
        working_dir = resolve_working_dir(database.get_session(new_session_id))
        reseed = reseed_delegated_cli_session(
            new_sdk_session_id, working_dir, force=True,
            context_window=delegated_window_for(source_sdk_session_id),
        )
        if reseed.status == "failed":
            logger.warning(
                "fork CLI re-seed failed for %s (parent %s): %s",
                new_sdk_session_id, source_sdk_session_id, reseed.error,
            )
    except Exception:
        logger.exception("fork CLI re-seed failed for %s", new_session_id)


async def _fork_fluid_session(
    *,
    session_id: str,
    original_session: dict,
    sdk_session_id: str,
    message_index,
    new_session_id: str,
    api_user_id: Optional[str],
    on_created: Callable[[str], Any],
    checkout=None,
) -> ForkSessionResponse:
    """Fork a fluid-harness session through its node tree.

    Same product behavior as the JSONL fork below — a new session row whose
    transcript is the original's history up to the chosen message — expressed
    in fluid's model: the new fluid session's head is set to the fork node and the
    ancestry prefix is copied verbatim (same node ids, same parents, same
    messages, same event-log prefix), so the shared prefix stays byte-identical
    and prompt caching survives the fork. The event copy is what keeps
    compaction promotions and code-mode inner calls in the forked context.

    ``message_index`` keeps its existing meaning: an index into the session's
    display entries, which for fluid is exactly the on-path node list (the node
    tree has no meta/sidechain/queue-operation entries to filter out). ``None``
    means "fork the whole session" — the sessions-board Fork action.
    """
    from app.core.provider.fluid_transcript import (
        fork_fluid_session,
        fluid_transcript_entries,
    )

    entries = await run_sync(fluid_transcript_entries, sdk_session_id)
    if not entries:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Cannot fork an empty session",
        )

    if message_index is None:
        fork_message_index = len(entries) - 1
    else:
        if message_index < 0 or message_index >= len(entries):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=(
                    f"Invalid message_index: {message_index}. Session has "
                    f"{len(entries)} messages (0-{len(entries) - 1})"
                ),
            )
        fork_message_index = message_index

    fork_node_id = entries[fork_message_index]["uuid"]

    original_title = original_session.get("title") or "Untitled"
    fork_title = f"Fork of {original_title}"

    fork_fields = dict(
        profile_id=original_session.get("profile_id"),
        project_id=original_session.get("project_id"),
        title=fork_title,
        api_user_id=api_user_id,
        parent_session_id=session_id,
        fork_point_message_index=fork_message_index,
        is_active=True,
        **_fork_inherited_fields(original_session, checkout),
    )
    from app.core.message_resolver import resolve_working_dir

    # Copy the node tree BEFORE the row exists: a visible fork could take a
    # turn mid-copy, and that turn's event log would block the inherited one
    # for good.
    try:
        new_sdk_session_id, node_count = await run_sync(
            fork_fluid_session, sdk_session_id, fork_node_id,
        )
    except Exception:
        logger.exception("Failed to fork fluid node tree for session %s", session_id)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to create forked session",
        )

    await run_sync(
        database.create_session,
        session_id=new_session_id,
        **fork_fields,
    )
    await run_sync(
        database.update_session,
        session_id=new_session_id,
        sdk_session_id=new_sdk_session_id,
    )

    # The fork is the caller's chat: a paired device keeps it (#1877).
    await run_sync(on_created, new_session_id)

    # create_session cannot carry the logical v2 branch (no parameter) — a
    # branch-bound parent must hand it to the fork or the fork resolves its cwd
    # to the base checkout.
    fork_branch_name = _fork_logical_branch(original_session, checkout)
    if fork_branch_name:
        await run_sync(_restore_fork_branch, new_session_id, fork_branch_name)

    await run_sync(
        _copy_app_event_log,
        session_id,
        new_session_id,
        at_head=(fork_message_index == len(entries) - 1),
    )

    # Reseed where the FORK's runtime resumes, read from its written row (its
    # lane, parent, branch and the creator just recorded each pick the cwd).
    await run_sync(_reseed_fork_cli, sdk_session_id, new_session_id, new_sdk_session_id, resolve_working_dir)

    logger.debug(
        "Forked fluid session %s -> %s (fluid %s, %d nodes) at message index %d",
        session_id, new_session_id, new_sdk_session_id, node_count,
        fork_message_index,
    )

    return ForkSessionResponse(
        session_id=new_session_id,
        title=fork_title,
        parent_session_id=session_id,
        fork_point_message_index=fork_message_index,
        message_count=node_count,
        status="success",
    )


@fork_router.post("/{session_id}/fork", response_model=ForkSessionResponse)
async def fork_session(
    request: Request,
    session_id: str,
    body: ForkSessionRequest = ForkSessionRequest(),
    token: str = Depends(require_auth)
):
    """
    Fork a session from a specific message index.

    Creates a new session that contains messages up to and including the
    specified message index. The new session references the original as its parent.

    Args:
        session_id: The session ID to fork from
        body: Request body containing message_index (0-based index to fork after)

    Returns:
        Information about the forked session
    """
    # Get the original session (404 unknown, 403 outside the caller's scope).
    original_session = await require_transcript_session(request, session_id)

    api_user = get_api_user_from_request(request)
    api_user_id = api_user["id"] if api_user else None

    # A chat another worker runs is only a mirror here: its transcript (fluid
    # node tree or CLI JSONL) and its worktree live on the owner, so the copy
    # below would always fail. Fork it there, as rewind and delete do.
    from app.network import control

    target = (body.target_instance_id or "").strip() or None
    if target is not None:
        await _check_fork_target(request, original_session, target)

    owner = control.owner_of(original_session)
    if owner is not None:
        result = await _fork_on_owner(
            request=request,
            session_id=session_id,
            original_session=original_session,
            owner=owner,
            message_index=body.message_index,
            api_user_id=api_user_id,
        )
    else:
        result = await _fork_owned(
            session_id=session_id,
            original_session=original_session,
            message_index=body.message_index,
            new_session_id=f"ses-{uuid.uuid4().hex[:16]}",
            api_user_id=api_user_id,
            on_created=lambda sid: record_creator(sid, request),
        )
    if target is not None:
        result = await _move_fork(result, target)
    return result


#: How long a fork to another worker waits for that worker to receive the
#: fork's row before moving it there (the catalog replicates it).
_FORK_MOVE_WAIT = 45.0
_FORK_MOVE_POLL_SECONDS = 1.5


async def _check_fork_target(request: Request, original_session: dict, target: str) -> None:
    """Refuse a fork to a worker the fork could not move to, before forking.

    The move itself checks all of this again (and the project checkout); this
    only spares the user a fork left behind on the wrong worker.
    """
    from app.api.git_owner import refuse_subagent_git
    from app.core.placement import capable_runners

    # Moving a chat commits and pushes its branch. A preview link may move a
    # chat this worker runs, so it may fork one to another worker too; forking
    # a chat another worker runs is refused by ``_fork_on_owner`` (#641).
    refuse_subagent_git(original_session)
    install = await run_sync(database.get_install, target)
    if not install:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"Unknown device: {target}")
    device = install.get("device_name") or target
    if target not in await run_sync(capable_runners, None, {}):
        raise HTTPException(status.HTTP_409_CONFLICT,
                            f"{device} is offline. Bring it online or pick another worker. Nothing was forked.")


async def _move_fork(result: ForkSessionResponse, target: str) -> ForkSessionResponse:
    """Move a fork just made to ``target``, as "Move to another worker" does.

    The fork already exists: a refused move leaves it where it was made and
    says why in ``move_error``, and never deletes it.
    """
    from app.api.sessions import move_session_to
    from app.network import catalog, control, engine

    new_id = result.session_id
    loop = asyncio.get_running_loop()
    deadline = loop.time() + _FORK_MOVE_WAIT
    # The destination reads the fork from its own copy of the catalog: send
    # the row now instead of at the next periodic scan.
    if control.owner_of(await run_sync(database.get_session, new_id) or {}) is None:
        try:
            await run_sync(catalog.publish_owned, session_ids=[new_id])
            engine.kick()
        except Exception:  # the periodic scan publishes it anyway
            logger.warning("Could not publish fork %s before moving it", new_id, exc_info=True)
    while True:
        row = await run_sync(database.get_session, new_id)
        if row is None:
            return result.model_copy(update={"move_error": "The fork was removed before it could move."})
        try:
            await move_session_to(new_id, row, target)
            return result
        except HTTPException as exc:
            detail = str(exc.detail)
            # Not there yet: the destination has not received the fork's row.
            if "Unknown session" in detail and loop.time() < deadline:
                await asyncio.sleep(_FORK_MOVE_POLL_SECONDS)
                continue
            logger.info("Fork %s stays where it was made; moving it to %s failed: %s", new_id, target, detail)
            return result.model_copy(update={"move_error": detail})


async def _fork_owned(
    *,
    session_id: str,
    original_session: dict,
    message_index: Optional[int],
    new_session_id: str,
    api_user_id: Optional[str],
    on_created: Callable[[str], Any],
) -> ForkSessionResponse:
    """Fork a session this worker runs, under ``new_session_id``.

    ``on_created(new_session_id)`` runs (blocking) right after the fork's row
    is written: it records who created the chat.
    """
    sdk_session_id = original_session.get("sdk_session_id")
    if not sdk_session_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Cannot fork session without SDK session ID"
        )

    checkout, parent_path = await run_sync(_fork_checkout, original_session, new_session_id)
    try:
        result = await _fork_owned_copy(
            session_id=session_id, original_session=original_session, sdk_session_id=sdk_session_id,
            message_index=message_index, new_session_id=new_session_id, api_user_id=api_user_id,
            on_created=on_created, checkout=checkout,
        )
    except BaseException:
        await run_sync(_discard_fork_checkout, checkout)
        raise
    if checkout is not None:
        if parent_path:
            await run_sync(_carry_uncommitted, parent_path, checkout.path, new_session_id)
        result.branch = checkout.branch
    return result


async def _fork_owned_copy(
    *,
    session_id: str,
    original_session: dict,
    sdk_session_id: str,
    message_index: Optional[int],
    new_session_id: str,
    api_user_id: Optional[str],
    on_created: Callable[[str], Any],
    checkout,
) -> ForkSessionResponse:
    """The transcript copy and row of ``_fork_owned``, bound to ``checkout``."""
    import json
    from app.core.config import settings
    from app.core.jsonl_parser import get_session_jsonl_path, parse_jsonl_file

    # Get working directory
    working_dir = "/workspace"
    project_id = original_session.get("project_id")
    if project_id:
        project = await run_sync(database.get_project, project_id)
        if project:
            working_dir = get_project_working_dir(project)

    # fluid (flag-on) sessions keep their transcript in fluid's node tree, not in
    # a CLI JSONL — the lookup below would fail with "JSONL file not found" and
    # the fork never happens. Fork them through the node tree instead.
    from app.core.provider.fluid_transcript import is_fluid_session

    if await run_sync(is_fluid_session, sdk_session_id):
        return await _fork_fluid_session(
            session_id=session_id,
            original_session=original_session,
            sdk_session_id=sdk_session_id,
            message_index=message_index,
            new_session_id=new_session_id,
            api_user_id=api_user_id,
            on_created=on_created,
            checkout=checkout,
        )

    # Find the original JSONL file
    original_jsonl_path = get_session_jsonl_path(sdk_session_id, working_dir)
    if not original_jsonl_path:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Cannot fork session: JSONL file not found"
        )

    # Parse original JSONL to get messages up to fork point
    original_entries = list(parse_jsonl_file(original_jsonl_path))

    # Count actual display messages (filter out queue-operation, file-history-snapshot, etc.)
    display_entries = []
    for entry in original_entries:
        entry_type = entry.get("type")
        # Skip non-message entries
        if entry_type in ("queue-operation", "file-history-snapshot"):
            continue
        # Skip meta messages
        if entry.get("isMeta"):
            continue
        # Skip sidechain messages
        if entry.get("isSidechain"):
            continue
        display_entries.append(entry)

    # Resolve the fork point. A null/omitted message_index means "fork the whole
    # session" (the sessions-board Fork action) → the last display message.
    if message_index is None:
        if not display_entries:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Cannot fork an empty session"
            )
        fork_message_index = len(display_entries) - 1
    else:
        if message_index < 0 or message_index >= len(display_entries):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Invalid message_index: {message_index}. Session has {len(display_entries)} messages (0-{len(display_entries)-1})"
            )
        fork_message_index = message_index

    # Find the actual index in original_entries that corresponds to the fork_message_index in display_entries
    display_count = 0
    fork_entry_index = 0
    for i, entry in enumerate(original_entries):
        entry_type = entry.get("type")
        if entry_type in ("queue-operation", "file-history-snapshot"):
            continue
        if entry.get("isMeta"):
            continue
        if entry.get("isSidechain"):
            continue

        if display_count == fork_message_index:
            fork_entry_index = i
            break
        display_count += 1

    new_sdk_session_id = str(uuid.uuid4())

    # Create title for fork
    original_title = original_session.get('title') or 'Untitled'
    fork_title = f"Fork of {original_title}"

    # Create the forked session in database - result used implicitly
    await run_sync(
        database.create_session,
        session_id=new_session_id,
        profile_id=original_session.get('profile_id'),
        project_id=original_session.get('project_id'),
        title=fork_title,
        api_user_id=api_user_id,
        parent_session_id=session_id,
        fork_point_message_index=fork_message_index,
        is_active=True,
        **_fork_inherited_fields(original_session, checkout),
    )

    # The fork is the caller's chat: a paired device keeps it (#1877).
    await run_sync(on_created, new_session_id)

    # Same branch hand-off as the fluid path above.
    fork_branch_name = _fork_logical_branch(original_session, checkout)
    if fork_branch_name:
        await run_sync(_restore_fork_branch, new_session_id, fork_branch_name)

    # Update with SDK session ID
    await run_sync(
        database.update_session,
        session_id=new_session_id,
        sdk_session_id=new_sdk_session_id
    )

    # The CLI resumes a transcript from the folder the chat runs in: the
    # fork's own checkout when it has one.
    if checkout is not None:
        working_dir = checkout.path

    # Create new JSONL file with entries up to and including fork point
    try:
        from app.core.jsonl_parser import get_project_dir_name

        # Get Claude projects directory
        claude_projects_dir = settings.get_claude_projects_dir

        # Convert working dir to Claude's project directory format
        project_dir_name = get_project_dir_name(working_dir)
        project_dir = claude_projects_dir / project_dir_name

        # Ensure project directory exists
        project_dir.mkdir(parents=True, exist_ok=True)

        new_jsonl_path = project_dir / f"{new_sdk_session_id}.jsonl"

        # Write entries up to and including fork point. Stamp sessionId to the
        # NEW sdk id — the CLI keys resume on the filename and cross-checks the
        # field, so leaving the parent's id makes --resume reject the file.
        written_entries = []
        with open(new_jsonl_path, 'w', encoding='utf-8') as f:
            for i, entry in enumerate(original_entries):
                if i > fork_entry_index:
                    break
                payload = dict(entry)
                payload["sessionId"] = new_sdk_session_id
                f.write(json.dumps(payload) + '\n')
                written_entries.append(payload)

        logger.debug(f"Created forked JSONL file: {new_jsonl_path} with {fork_entry_index + 1} entries")

    except Exception:
        logger.exception("Failed to create JSONL file for forked session")
        # Delete the session we just created since fork failed
        await run_sync(database.delete_session, new_session_id)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to create forked session"
        )

    await run_sync(
        _copy_app_event_log,
        session_id,
        new_session_id,
        at_head=(fork_message_index == len(display_entries) - 1),
    )
    await run_sync(_seed_cli_fork_events, new_session_id, new_sdk_session_id, working_dir, written_entries)

    logger.debug(f"Forked session {session_id} to {new_session_id} at message index {fork_message_index}")

    return ForkSessionResponse(
        session_id=new_session_id,
        title=fork_title,
        parent_session_id=session_id,
        fork_point_message_index=fork_message_index,
        message_count=fork_entry_index + 1,
        status="success"
    )


# ---------------------------------------------------------------------------
# A chat another worker runs: fork it on that worker
# ---------------------------------------------------------------------------

#: The owner copies the whole transcript (node tree, event log) before it
#: answers; a long chat takes a while.
_OWNER_FORK_TIMEOUT = 120.0

#: As ``sessions_create.PREVIEW_ELSEWHERE`` for starting a chat there.
PREVIEW_FORK_ELSEWHERE = "The preview link can't fork a chat another worker runs."

_FORK_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}")


async def _fork_on_owner(
    *,
    request: Request,
    session_id: str,
    original_session: dict,
    owner: str,
    message_index: Optional[int],
    api_user_id: Optional[str],
) -> ForkSessionResponse:
    """Fork a mirrored chat on the worker that runs it.

    Same shape as creating a chat on another device (``_create_on_owner``):
    this worker first writes its own copy of the fork, stamped with the
    owner, so the UI can open it at once and its turns go to the owner; the
    owner then forks under the same id and publishes the real row over it.
    When the owner refuses or was never reached, the copy is removed again.
    """
    from app.core import preview_access
    from app.core.mobile_tool_identity import request_principal
    from app.network import control

    # The worker that runs the chat cannot see the preview cookie's principal,
    # so the fork would lose the ceiling there (#641): refuse, as starting a
    # chat on another worker does.
    if preview_access.grant(request) is not None:
        raise HTTPException(status.HTTP_403_FORBIDDEN, PREVIEW_FORK_ELSEWHERE)

    new_session_id = f"ses-{uuid.uuid4().hex[:16]}"
    fork_title = f"Fork of {original_session.get('title') or 'Untitled'}"

    def _create_copy() -> None:
        from app.db.base import get_cursor, transaction

        with transaction() as txn:
            database.create_session(
                session_id=new_session_id,
                profile_id=original_session.get("profile_id"),
                project_id=original_session.get("project_id"),
                title=fork_title,
                api_user_id=api_user_id,
                parent_session_id=session_id,
                fork_point_message_index=message_index,
                is_active=True,
                target_instance_id=owner,
                _conn=txn,
                **_fork_inherited_fields(original_session),
            )
            # create_session stamps the owner only for a runner it can find;
            # the copy must name it regardless, or a send would run it here.
            # instance_id stays NULL: a foreign-owned row with an instance_id
            # and no sync record counts as a legacy held session, which the
            # catalog scan would publish to the owner while it is still making
            # the real fork. The owner's published row fills it in.
            get_cursor(txn).execute(
                "UPDATE sessions SET owner_device_id = %s, instance_id = NULL WHERE id = %s",
                (owner, new_session_id),
            )
            # The fork is the caller's chat: a paired device keeps it (#1877).
            record_creator(new_session_id, request, conn=txn)

    await run_sync(_create_copy)
    principal = await run_sync(request_principal, request)
    payload = {
        "session_id": session_id,
        "new_session_id": new_session_id,
        "message_index": message_index,
        "api_user_id": api_user_id,
        # Who forked it; the worker that runs it labels it (#1877).
        "provenance_owner": (principal or {}).get("owner"),
    }
    reached = False  # Whether an attempt may have forked it there.
    try:
        try:
            result = await control.call(owner, "session-fork", payload, timeout=_OWNER_FORK_TIMEOUT)
        except control.MaybeApplied:
            # The owner's fork is keyed on new_session_id: asking again returns
            # the fork it already made instead of making a second one.
            reached = True
            logger.info("Fork %s of %s: no answer from its owner; asking once more", new_session_id, session_id)
            result = await control.call(owner, "session-fork", payload, timeout=_OWNER_FORK_TIMEOUT)
    except control.ControlRefused as exc:
        await run_sync(database.delete_session, new_session_id)
        raise HTTPException(409, "The worker that runs this chat could not fork it. "
                                 f"Check that it is online and updated. {exc}") from exc
    except control.RunnerUnreachable as exc:
        if not (reached or isinstance(exc, control.MaybeApplied)):
            await run_sync(database.delete_session, new_session_id)
            raise HTTPException(503, f"{exc} Forking runs on the worker that runs this chat; "
                                     "try again once it is reachable.") from exc
        # It may exist there: the copy stays, and the owner's row replaces it
        # if it does. If it never forked, the copy does not respond.
        raise HTTPException(503, f"{exc} The fork may or may not have been made on that worker. "
                                 "If it doesn't respond, delete it and fork again.") from exc

    if result.get("session_id") != new_session_id:
        raise HTTPException(502, "The worker that runs this chat answered for a different fork")
    branch = result.get("branch") if isinstance(result.get("branch"), str) else None
    if branch:
        # The copy was bound like its parent; the fork has its own branch
        # there. Checkout paths are per worker, so none is recorded here.
        await run_sync(_bind_copy_to_branch, new_session_id, branch)
    try:
        fork_point = int(result.get("fork_point_message_index"))
    except (TypeError, ValueError):
        fork_point = message_index if message_index is not None else 0
    # A whole-session fork's copy has no fork point yet; the owner's published
    # row (which replicates fork_point_message_index) fills it in.
    return ForkSessionResponse(
        session_id=new_session_id,
        title=result.get("title") or fork_title,
        parent_session_id=session_id,
        fork_point_message_index=fork_point,
        message_count=int(result.get("message_count") or 0),
        status="success",
        branch=branch,
    )


def _bind_copy_to_branch(new_session_id: str, branch: str) -> None:
    from app.db.base import get_cursor, get_db

    with get_db() as conn:
        get_cursor(conn).execute(
            "UPDATE sessions SET branch = %s, worktree_branch = %s, worktree_path = NULL, worktree_id = NULL "
            "WHERE id = %s",
            (branch, branch, new_session_id),
        )


#: How long a repeated request waits for the first one to finish the copy.
_FORK_IN_PROGRESS_WAIT = 90.0
_FORK_POLL_SECONDS = 0.5


async def _completed_fork(new_session_id: str) -> dict:
    """The fork's row once its transcript exists, for a repeated request.

    Both fork paths set ``sdk_session_id`` only after the transcript is
    copied (and delete the row if copying fails), so a row without one is a
    fork still being made, or a row this worker never forked: acknowledging
    it would hand back a chat with no transcript.
    """
    loop = asyncio.get_running_loop()
    deadline = loop.time() + _FORK_IN_PROGRESS_WAIT
    while True:
        row = await run_sync(database.get_session, new_session_id)
        if row is None:
            raise ValueError("Forking this chat failed on the worker that runs it")
        if row.get("sdk_session_id"):
            return row
        if loop.time() >= deadline:
            raise ValueError("The fork is still being made on the worker that runs this chat")
        await asyncio.sleep(_FORK_POLL_SECONDS)


async def handle_fork(body: dict, from_device: str) -> dict:
    """Peer control ``session-fork``: fork a chat this worker runs for a mirror.

    Device-token authentication is enforced by /peer/control, and the
    requesting worker already checked its caller may read the transcript.
    Ownership is checked again here: a peer must not fork a chat this
    worker only mirrors. Keyed on ``new_session_id``, so a repeated request
    after a lost answer returns the fork already made.
    """
    from app.network import control

    session_id = str(body.get("session_id") or "")
    new_session_id = body.get("new_session_id")
    if not isinstance(new_session_id, str) or not _FORK_ID.fullmatch(new_session_id):
        raise ValueError("new_session_id is required")
    message_index = body.get("message_index")
    if message_index is not None and (isinstance(message_index, bool) or not isinstance(message_index, int)):
        raise ValueError("message_index must be an integer")
    original_session = await run_sync(control._require_owned, session_id)

    # The fork's row appears only once its history is copied, so a repeat
    # (the caller asks again when an answer is lost) waits on the fork still
    # being made instead of copying a second time. No await between the check
    # and the registration; the shield keeps a dropped request from cancelling it.
    inflight = _INFLIGHT_FORKS.get(new_session_id)
    if inflight is None:
        task = asyncio.ensure_future(_fork_for_peer(
            body, session_id, new_session_id, message_index, original_session, from_device,
        ))
        inflight = _INFLIGHT_FORKS[new_session_id] = (session_id, task)

        def _done(finished: asyncio.Future) -> None:
            _INFLIGHT_FORKS.pop(new_session_id, None)
            if not finished.cancelled():
                finished.exception()  # Retrieved: a dropped first request awaits nothing.

        task.add_done_callback(_done)
    elif inflight[0] != session_id:
        raise ValueError("That session id is already in use")
    return await asyncio.shield(inflight[1])


#: Peer forks still being made, by new_session_id: (parent id, task).
_INFLIGHT_FORKS: dict[str, tuple[str, "asyncio.Future[dict]"]] = {}


async def _fork_for_peer(
    body: dict, session_id: str, new_session_id: str, message_index, original_session: dict, from_device: str,
) -> dict:
    """The fork ``handle_fork`` makes (or the one it already made) for a peer."""
    from app.network import catalog, control

    existing = await run_sync(database.get_session, new_session_id)
    if existing is not None:
        if existing.get("parent_session_id") != session_id or control.owner_of(existing) is not None:
            raise ValueError("That session id is already in use")
        existing = await _completed_fork(new_session_id)
        fork_point = existing.get("fork_point_message_index") or 0
        # A repeat must name the fork's own branch too, or the caller's copy
        # stays bound to the parent's.
        own_branch = fork_branch(new_session_id)
        return ForkSessionResponse(
            session_id=new_session_id, title=existing.get("title"), parent_session_id=session_id,
            fork_point_message_index=fork_point, message_count=fork_point + 1, status="success",
            branch=own_branch if existing.get("worktree_branch") == own_branch else None,
        ).model_dump()

    # The fork keeps the API key that made it when this worker knows the key
    # (api_users replicate); otherwise it is the owner's, as for a new chat.
    requested = body.get("api_user_id")
    api_user_id = None
    if isinstance(requested, str) and requested:
        api_user_id = requested if await run_sync(database.get_api_user, requested) is not None else None
    label = body.get("provenance_owner")

    try:
        result = await _fork_owned(
            session_id=session_id,
            original_session=original_session,
            message_index=message_index,
            new_session_id=new_session_id,
            api_user_id=api_user_id,
            on_created=lambda sid: control.stamp_provenance(sid, label),
        )
    except HTTPException as exc:
        # The requesting worker shows the reason; never ask it to retry.
        raise ValueError(str(exc.detail)) from exc
    # A fork short of the head copies no app events, and the mirror cannot
    # build them (it has no transcript): build them here so it opens with its
    # history. Never raises.
    from app.core.history_backfill import ensure_backfilled

    await ensure_backfilled(new_session_id)
    try:
        await run_sync(catalog.publish_owned, session_ids=[new_session_id])
    except Exception:  # noqa: BLE001 — the periodic scan publishes it anyway
        logger.warning("Fork %s made for %s; publishing it is delayed", new_session_id, from_device, exc_info=True)
    return result.model_dump()
