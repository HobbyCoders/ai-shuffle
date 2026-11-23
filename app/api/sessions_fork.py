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
        # The lane: a chat-lane chat has no project by design; forked into the
        # code lane it would ask for one before it could take a file.
        "mode": original_session.get("mode"),
        "project_v2_id": original_session.get("project_v2_id"),
        "provider_id": original_session.get("provider_id"),
        "derive_provider_from_profile": False,
        "model": original_session.get("model"),
        "effort": original_session.get("effort"),
        "permission_mode": original_session.get("permission_mode"),
        "context_window": original_session.get("context_window"),
        # The parent's pair at the tier it runs (never a stale refinement).
        **stored_pair(prior=original_session),
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


async def _fork_fluid_session(
    *,
    session_id: str,
    original_session: dict,
    sdk_session_id: str,
    message_index,
    new_session_id: str,
    api_user_id: Optional[str],
    on_created: Callable[[str], Any],
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
        **_fork_inherited_fields(original_session),
    )
    # Copy the ancestry FIRST: if it fails there is no orphan session row to
    # clean up. Reseed for the prospective FORK's runtime row, not its parent:
    # parent_session_id / api_user_id / the absent mode can select another cwd.
    # Keep the creation fields shared so this cannot drift from create_session.
    from app.core.message_resolver import resolve_working_dir

    parent_branch = (original_session.get("branch") or "").strip()
    working_dir = resolve_working_dir({"id": new_session_id, "branch": parent_branch, **fork_fields})
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
        **fork_fields,
    )

    # The fork is the caller's chat: a paired device keeps it (#1877).
    await run_sync(on_created, new_session_id)

    # create_session cannot carry the logical v2 branch (no parameter) — a
    # branch-bound parent must hand it to the fork or the fork resolves its cwd
    # to the base checkout.
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
    # Get the original session (404 unknown, 403 outside the caller's scope).
    original_session = await require_transcript_session(request, session_id)

    api_user = get_api_user_from_request(request)
    api_user_id = api_user["id"] if api_user else None

    # A chat another worker runs is only a mirror here: its transcript (fluid
    # node tree or CLI JSONL) and its worktree live on the owner, so the copy
    # below would always fail. Fork it there, as rewind and delete do.
    from app.network import control

    owner = control.owner_of(original_session)
    if owner is not None:
        return await _fork_on_owner(
            request=request,
            session_id=session_id,
            original_session=original_session,
            owner=owner,
            message_index=body.message_index,
            api_user_id=api_user_id,
        )

    return await _fork_owned(
        session_id=session_id,
        original_session=original_session,
        message_index=body.message_index,
        new_session_id=f"ses-{uuid.uuid4().hex[:16]}",
        api_user_id=api_user_id,
        on_created=lambda sid: record_creator(sid, request),
    )


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
    import json
    from app.core.config import settings
    from app.core.jsonl_parser import get_session_jsonl_path, parse_jsonl_file

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
            session_id=session_id,
            original_session=original_session,
            sdk_session_id=sdk_session_id,
            message_index=message_index,
            new_session_id=new_session_id,
            api_user_id=api_user_id,
            on_created=on_created,
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
        **_fork_inherited_fields(original_session),
    )

    # The fork is the caller's chat: a paired device keeps it (#1877).
    await run_sync(on_created, new_session_id)

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
    from app.core.mobile_tool_identity import request_principal
    from app.network import control

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
    from app.network import catalog, control

    session_id = str(body.get("session_id") or "")
    new_session_id = body.get("new_session_id")
    if not isinstance(new_session_id, str) or not _FORK_ID.fullmatch(new_session_id):
        raise ValueError("new_session_id is required")
    message_index = body.get("message_index")
    if message_index is not None and (isinstance(message_index, bool) or not isinstance(message_index, int)):
        raise ValueError("message_index must be an integer")
    original_session = await run_sync(control._require_owned, session_id)

    existing = await run_sync(database.get_session, new_session_id)
    if existing is not None:
        if existing.get("parent_session_id") != session_id or control.owner_of(existing) is not None:
            raise ValueError("That session id is already in use")
        existing = await _completed_fork(new_session_id)
        fork_point = existing.get("fork_point_message_index") or 0
        return ForkSessionResponse(
            session_id=new_session_id, title=existing.get("title"), parent_session_id=session_id,
            fork_point_message_index=fork_point, message_count=fork_point + 1, status="success",
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
