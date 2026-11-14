"""Attach a project to a running no-project session (issue #371 step 4).

A session with no project can be given one later, which turns it into an
ordinary project session: the same conversation, now with the project's main
checkout, git panel and full tool surface. It is one-way: nothing detaches a
project again, and the database write itself refuses a row that already has
one (``attach_session_project``).

The live client, if any, still carries the no-project tool ceiling and cwd, so
it is closed with ``preserve_session=True``; the next turn rebuilds its
options from the updated row and resumes the same transcript.

Owner only (``require_admin``): the attach widens what the session can reach,
so API users, non-owner paired devices and agent previews are refused. The
same write puts the session back on Ask (see ``attach_session_project``).

The attach always runs on the worker that runs the session, because only it
can read its own busy state, release its own pooled client and check its own
checkout. Viewed from another of the owner's workers, the route checks the
caller here first, then relays to the owner over the peer control channel
(``attach-project``), naming the project by its shared identity (project V2
id and origin): legacy project ids are local to each worker. The owner runs
the same ``apply_attach_project`` as a local attach, resolving its OWN
checkout of that project, and refuses when it has none.

Busy sessions are refused, and the check is not a cached hint: this process's
live runtime, its turn executions and the pooled client are read first, then
the write itself re-checks queued and live turns under turn admission's
session lock. The pooled client is released only if it is still idle right
before the release, with no await in between.
"""

import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel

from app.api.auth import get_api_user_from_request, require_admin
from app.api.common import require_origin_access
from app.api.sessions_helpers import require_session
from app.db import database
from app.db.async_database import run_sync
from app.network import control

logger = logging.getLogger(__name__)

router = APIRouter()

ALREADY_HAS_PROJECT = "This session already has a project. Projects can't be changed or detached."
NOT_HERE = "This chat now runs on another worker. Reopen it and try again."
BUSY = "Wait for this session to finish its turn, then attach the project."
BUSY_BACKGROUND = (
    "This session's background work is still running. "
    "Wait for its running or background work to finish, then attach the project."
)
NEEDS_LOCAL_CHECKOUT = "This project isn't checked out on this worker."
SUBAGENT = "A subagent's chat can't be given a project."
ARCHIVED = "This chat is archived. Restore it first."


class AttachProjectRequest(BaseModel):
    """Name the project by exactly one of a local legacy id or its V2 id."""

    project_id: Optional[str] = None
    project_v2_id: Optional[str] = None


class AttachProjectResponse(BaseModel):
    session_id: str
    project_id: str
    project_v2_id: Optional[str] = None
    mode: str
    #: Always Ask: the attach resets Auto and Bypass (``attach_session_project``).
    permission_mode: str = "default"


def _owned_here(session: dict) -> bool:
    """Whether this install runs ``session`` (a mirrored remote row is not)."""
    from app.core.config import settings

    try:
        from app.network import runners
        my_device = runners.self_device_id()
    except Exception:  # noqa: BLE001 — no network context: this install only
        my_device = None
    owner_device = session.get("owner_device_id")
    if owner_device and my_device:
        return owner_device == my_device
    instance = session.get("instance_id")
    return not instance or instance in {settings.effective_instance_id, my_device}


def _live_here(session_id: str) -> bool:
    """Whether this process is running work on ``session_id`` right now.

    In-memory only and synchronous, so a caller can act on the answer before
    the event loop runs anything else: a turn this process executes, or a
    pooled client that is streaming, draining or waiting on the owner.
    """
    from app.core import session_runtime
    from app.core.session_pool import get_session_pool

    if session_runtime.get_live_runtime(session_id) is not None:
        return True
    if session_id in session_runtime.active_executions().values():
        return True
    pool = get_session_pool()
    state = pool.peek(session_id)
    if state is None:
        return False
    return bool(
        state.is_streaming
        or state.idle_reader_active
        or pool._is_awaiting_user_input(session_id)
    )


def _has_live_run(session_id: str) -> bool:
    """The uncached runs/turns read; a database error counts as busy."""
    from app.core import streaming_status

    try:
        return streaming_status._has_active_run_db(session_id)
    except Exception:  # noqa: BLE001 — can't tell, so don't attach
        logger.warning("Busy check failed for %s; refusing the attach", session_id, exc_info=True)
        return True


def _check_attachable(session: dict) -> None:
    """The refusals that read only the row; both workers apply them."""
    from app.core import archive_guard
    from app.core.session_modes import session_mode_of

    if archive_guard.is_archived_row(session):
        raise HTTPException(status.HTTP_409_CONFLICT, ARCHIVED)
    if (
        session_mode_of(session) != "chat"
        or session.get("project_id")
        or session.get("project_v2_id")
    ):
        raise HTTPException(status.HTTP_409_CONFLICT, ALREADY_HAS_PROJECT)
    if session.get("is_worker") or session.get("parent_session_id"):
        raise HTTPException(status.HTTP_409_CONFLICT, SUBAGENT)


def _own_checkout(project_v2_id: Optional[str], origin: Optional[str]) -> tuple[dict, Optional[str]]:
    """This worker's project row and V2 id for a project named by shared identity.

    A relayed attach cannot use the viewing worker's legacy id. The V2 row is
    replicated, so its origin is read here when this worker has it; the sent
    origin is only a fallback, and either way only a checkout this worker
    registered itself, with its folder present, can match.
    """
    from app.core import project_resolver, project_setup

    pv2 = project_resolver.resolve_catalog_project(project_v2_id) if project_v2_id else None
    key = (pv2 or {}).get("origin") or origin
    project = project_setup._registered_row(key) if key else None
    if not project:
        name = (pv2 or {}).get("name") or "This project"
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            f"{name} isn't checked out on {_self_name()}, the worker that runs this chat. "
            "Add it there first, or pick a project it has.",
        )
    if not pv2:
        pv2 = project_resolver.resolve_project_v2({"project_id": project["id"]})
    return project, (pv2 or {}).get("id")


def _self_name() -> str:
    try:
        from app.network import engine
        ctx = engine.context()
        if ctx is not None:
            return control._names(ctx, ctx.device_id)[1]
    except Exception:  # noqa: BLE001 — the message must not fail
        pass
    return "this worker"


async def apply_attach_project(
    session_id: str,
    *,
    project_id: Optional[str] = None,
    project_v2_id: Optional[str] = None,
    origin: Optional[str] = None,
    api_user: Optional[dict] = None,
) -> AttachProjectResponse:
    """Attach on the worker that runs ``session_id``: the one set of rules.

    ``project_id`` is this worker's legacy id (a local attach); otherwise the
    project is named by ``project_v2_id``/``origin`` and resolved to this
    worker's own checkout. Raises ``HTTPException`` with the refusal.
    """
    session = await run_sync(database.get_session, session_id)
    if session is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Session not found")
    _check_attachable(session)
    if not _owned_here(session):
        raise HTTPException(status.HTTP_409_CONFLICT, NOT_HERE)
    if _live_here(session_id) or await run_sync(_has_live_run, session_id):
        raise HTTPException(status.HTTP_409_CONFLICT, BUSY)

    if project_id:
        access = await run_sync(require_origin_access, api_user=api_user, project_id=project_id)
        project = access.get("legacy")
        if not project:
            raise HTTPException(status.HTTP_404_NOT_FOUND, NEEDS_LOCAL_CHECKOUT)
        project_v2_id = (access.get("project_v2") or {}).get("id")
    elif project_v2_id or origin:
        project, project_v2_id = await run_sync(_own_checkout, project_v2_id, origin)
    else:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Pick a project to attach.")

    try:
        row = await run_sync(
            database.attach_session_project, session_id, project["id"], project_v2_id
        )
    except database.AttachBusy as busy:
        detail = BUSY_BACKGROUND if busy.background_only else BUSY
        raise HTTPException(status.HTTP_409_CONFLICT, detail) from None
    if not row:
        raise HTTPException(status.HTTP_409_CONFLICT, ALREADY_HAS_PROJECT)

    if not project_v2_id:
        # Same best-effort V2 bind the composer's create does.
        try:
            from app.core import project_resolver
            await run_sync(project_resolver.resolve_project_v2, row)
            refreshed = await run_sync(database.get_session, session_id)
            project_v2_id = (refreshed or {}).get("project_v2_id")
        except Exception:  # noqa: BLE001
            logger.debug("V2 project bind failed for %s", session_id, exc_info=True)

    # The pooled client was built with the no-project ceiling and cwd. Close it
    # without ending the conversation; the next turn rebuilds from the row.
    # A turn admitted after the write already built its options from the new
    # row, and the cwd change makes it replace this client itself
    # (``reconnect_option_names``), so a client in use is never cut off here.
    # ``remove`` pops the entry before its first await: nothing can start on
    # the client between this check and the release.
    try:
        from app.core.session_pool import get_session_pool
        if not _live_here(session_id):
            await get_session_pool().remove(session_id, preserve_session=True)
    except Exception:  # noqa: BLE001 — a stale client is refreshed on its next turn
        logger.warning("Could not release the live client for %s", session_id, exc_info=True)

    # Relayed to the user's other workers too, so a page open on the worker
    # that asked for a relayed attach updates without a reload.
    try:
        from app.core.sync_engine import sync_engine
        await sync_engine.broadcast_user_session_updated(
            user_id=session.get("api_user_id") or "admin",
            session_id=session_id,
            updates={"project_id": project["id"], "project_v2_id": project_v2_id, "mode": None,
                     "permission_mode": "default"},
            source_device_id=None,
        )
    except Exception:  # noqa: BLE001 — the write already committed
        logger.warning("Could not broadcast the attach for %s", session_id, exc_info=True)

    return AttachProjectResponse(
        session_id=session_id,
        project_id=project["id"],
        project_v2_id=project_v2_id,
        mode="code",
        permission_mode=row.get("permission_mode") or "default",
    )


async def handle(body: dict, from_device: str) -> dict:
    """The ``attach-project`` control action, run on the session's owner.

    The viewing worker already required the owner (``require_admin``) before
    relaying; the project arrives by shared identity, never a legacy id.
    """
    session_id = body.get("session_id")
    if not isinstance(session_id, str) or not session_id:
        raise ValueError("session_id is required")
    project_v2_id = body.get("project_v2_id")
    origin = body.get("origin")
    if not isinstance(project_v2_id, str):
        project_v2_id = None
    if not isinstance(origin, str):
        origin = None
    if not project_v2_id and not origin:
        raise ValueError("project_v2_id or origin is required")
    result = await apply_attach_project(session_id, project_v2_id=project_v2_id, origin=origin)
    return result.model_dump()


def _shared_identity(api_user: Optional[dict], body: AttachProjectRequest) -> dict:
    """The project's V2 id and origin, checked against the caller's access here."""
    access = require_origin_access(
        api_user=api_user, project_id=body.project_id, project_v2_id=body.project_v2_id
    )
    return {"project_v2_id": (access.get("project_v2") or {}).get("id"), "origin": access.get("origin")}


@router.post("/{session_id}/attach-project", response_model=AttachProjectResponse)
async def attach_project(
    request: Request,
    session_id: str,
    body: AttachProjectRequest,
    token: str = Depends(require_admin),
):
    """Give this no-project session a project. One-way, owner only.

    Refusals, in order: anyone but the owner (403); an archived chat (409);
    a session that is not a no-project top-level chat (409); one with a turn
    queued or running, or whose state can't be read (409); a project the
    caller cannot reach or that has no checkout on the worker that runs the
    chat (403/404 here, 409 relayed). A lost race with another attach also
    answers 409. For a chat another worker runs: 503 when that worker can't
    be reached, or when it may have applied the attach without confirming.
    """
    session = await require_session(request, session_id)
    _check_attachable(session)
    api_user = get_api_user_from_request(request)
    owner = await run_sync(control.owner_of, session)
    if owner is None:
        return await apply_attach_project(
            session_id, project_id=body.project_id, project_v2_id=body.project_v2_id,
            api_user=api_user,
        )

    identity = await run_sync(_shared_identity, api_user, body)
    try:
        result = await control.call(owner, "attach-project", {"session_id": session_id, **identity})
    except control.MaybeApplied as exc:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            f"{await _worker_name(owner)} didn't confirm the attach, and it may still have been "
            "applied. Reopen this chat to check before trying again.",
        ) from exc
    except control.RunnerUnreachable as exc:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            f"{await _worker_name(owner)} runs this chat and must be online to attach a project. {exc}",
        ) from exc
    except control.ControlRefused as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    return AttachProjectResponse.model_validate(result)


async def _worker_name(device_id: str) -> str:
    try:
        from app.network import engine
        ctx = engine.context()
        if ctx is not None:
            name = (await run_sync(control._names, ctx, device_id))[0]
            return name if name != "the other worker" else "The worker that runs this chat"
    except Exception:  # noqa: BLE001 — the message must not fail
        pass
    return "The worker that runs this chat"
