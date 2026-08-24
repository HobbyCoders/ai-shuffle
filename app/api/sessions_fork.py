"""
Session fork endpoint.

Extracted from sessions.py for maintainability. The sub-router is
included in the main sessions router for backward compatibility.
"""

import uuid

from fastapi import APIRouter, HTTPException, Depends, status, Request

from app.db import database
from app.db.async_database import run_sync
from app.api.auth import require_auth, get_api_user_from_request
from app.core.project_utils import get_project_working_dir
from app.api.sessions_helpers import (
    require_session,
    ForkSessionRequest,
    ForkSessionResponse,
)

import logging
logger = logging.getLogger(__name__)

# Sub-router — included by the main sessions router (no prefix needed)
fork_router = APIRouter(tags=["Sessions"])


def _fork_inherited_fields(original_session: dict) -> dict:
    """The parent-session fields a fork must carry over.

    A fork is a second driver for the SAME working context, so it inherits the
    worktree binding (worktree_id / legacy columns / logical v2 branch) and the
    run binding (model / effort / permission_mode / context_window / fast /
    provider). Without this the fork row resolves its cwd to the project's BASE
    checkout — sending a message in it then runs against the main checkout's
    branch instead of the parent's worktree — and the composer shows registry
    defaults instead of what the parent was running.
    """
    return {
        "project_v2_id": original_session.get("project_v2_id"),
        "provider_id": original_session.get("provider_id"),
        "derive_provider_from_profile": False,
        "model": original_session.get("model"),
        "effort": original_session.get("effort"),
        "permission_mode": original_session.get("permission_mode"),
        "context_window": original_session.get("context_window"),
        "fast": original_session.get("fast"),
        "worktree_id": original_session.get("worktree_id"),
        "worktree_branch": original_session.get("worktree_branch"),
        "worktree_path": original_session.get("worktree_path"),
    }


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


async def _fork_fluid_session(
    *,
    request: Request,
    session_id: str,
    original_session: dict,
    sdk_session_id: str,
    message_index,
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

    new_session_id = f"ses-{uuid.uuid4().hex[:16]}"
    api_user = get_api_user_from_request(request)
    api_user_id = api_user["id"] if api_user else None
    original_title = original_session.get("title") or "Untitled"
    fork_title = f"Fork of {original_title}"

    # Copy the ancestry FIRST: if it fails there is no orphan session row to
    # clean up (the JSONL fork below has to delete the row it already created).
    # working_dir lets a delegated parent mint a fresh CLI transcript from the
    # copied path so the fork's next --resume is not an empty conversation.
    from app.core.message_resolver import resolve_working_dir

    working_dir = resolve_working_dir(original_session)
    try:
        new_sdk_session_id, node_count = await run_sync(
            fork_fluid_session,
            sdk_session_id,
            fork_node_id,
            working_dir=working_dir,
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
        profile_id=original_session.get("profile_id"),
        project_id=original_session.get("project_id"),
        title=fork_title,
        api_user_id=api_user_id,
        parent_session_id=session_id,
        fork_point_message_index=fork_message_index,
        is_active=True,
        **_fork_inherited_fields(original_session),
    )

    # create_session cannot carry the logical v2 branch (no parameter) — a
    # branch-bound parent must hand it to the fork or the fork resolves its cwd
    # to the base checkout.
    parent_branch = (original_session.get("branch") or "").strip()
    if parent_branch:
        await run_sync(_restore_fork_branch, new_session_id, parent_branch)

    await run_sync(
        database.update_session,
        session_id=new_session_id,
        sdk_session_id=new_sdk_session_id,
    )

    await run_sync(
        _copy_app_event_log,
        session_id,
        new_session_id,
        at_head=(fork_message_index == len(entries) - 1),
    )

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
    import uuid
    import json
    from app.core.config import settings
    from app.core.jsonl_parser import get_session_jsonl_path, parse_jsonl_file

    # Get the original session
    original_session = await require_session(request, session_id)

    sdk_session_id = original_session.get("sdk_session_id")
    if not sdk_session_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Cannot fork session without SDK session ID"
        )

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
            request=request,
            session_id=session_id,
            original_session=original_session,
            sdk_session_id=sdk_session_id,
            message_index=body.message_index,
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
    if body.message_index is None:
        if not display_entries:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Cannot fork an empty session"
            )
        fork_message_index = len(display_entries) - 1
    else:
        if body.message_index < 0 or body.message_index >= len(display_entries):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Invalid message_index: {body.message_index}. Session has {len(display_entries)} messages (0-{len(display_entries)-1})"
            )
        fork_message_index = body.message_index

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

    # Generate new IDs
    new_session_id = f"ses-{uuid.uuid4().hex[:16]}"
    new_sdk_session_id = str(uuid.uuid4())

    # Get API user if applicable
    api_user = get_api_user_from_request(request)
    api_user_id = api_user['id'] if api_user else None

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
        **_fork_inherited_fields(original_session),
    )

    # Same branch hand-off as the fluid path above.
    parent_branch = (original_session.get("branch") or "").strip()
    if parent_branch:
        await run_sync(_restore_fork_branch, new_session_id, parent_branch)

    # Update with SDK session ID
    await run_sync(
        database.update_session,
        session_id=new_session_id,
        sdk_session_id=new_sdk_session_id
    )

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
        with open(new_jsonl_path, 'w', encoding='utf-8') as f:
            for i, entry in enumerate(original_entries):
                if i > fork_entry_index:
                    break
                payload = dict(entry)
                payload["sessionId"] = new_sdk_session_id
                f.write(json.dumps(payload) + '\n')

        logger.debug(f"Created forked JSONL file: {new_jsonl_path} with {fork_entry_index + 1} entries")

    except Exception as e:
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

    logger.debug(f"Forked session {session_id} to {new_session_id} at message index {fork_message_index}")

    return ForkSessionResponse(
        session_id=new_session_id,
        title=fork_title,
        parent_session_id=session_id,
        fork_point_message_index=fork_message_index,
        message_count=fork_entry_index + 1,
        status="success"
    )
