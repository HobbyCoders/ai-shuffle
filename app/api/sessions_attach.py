"""Attach a project to a running no-project session (issue #371 step 4).

A session with no project can be given one later, which turns it into an
ordinary project session: the same conversation, now with the project's main
checkout, git panel and full tool surface. It is one-way: nothing detaches a
project again, and the database write itself refuses a row that already has
one (``attach_session_project``).

The live client, if any, still carries the no-project tool ceiling and cwd, so
it is closed with ``preserve_session=True``; the next turn rebuilds its
options from the updated row and resumes the same transcript.
"""

import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel

from app.api.auth import get_api_user_from_request, require_auth
from app.api.common import require_origin_access
from app.api.sessions_helpers import refuse_assistant_home_mutation, require_session
from app.db import database
from app.db.async_database import run_sync

logger = logging.getLogger(__name__)

router = APIRouter()

ALREADY_HAS_PROJECT = "This session already has a project. Projects can't be changed or detached."
NOT_HERE = "Attach the project from the worker that runs this session."
BUSY = "Wait for this session to finish its turn, then attach the project."
NEEDS_LOCAL_CHECKOUT = "This project isn't checked out on this worker."


class AttachProjectRequest(BaseModel):
    project_id: str


class AttachProjectResponse(BaseModel):
    session_id: str
    project_id: str
    project_v2_id: Optional[str] = None
    mode: str


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


@router.post("/{session_id}/attach-project", response_model=AttachProjectResponse)
async def attach_project(
    request: Request,
    session_id: str,
    body: AttachProjectRequest,
    token: str = Depends(require_auth),
):
    """Give this no-project session a project. One-way.

    Refusals, in order: an archived chat or the assistant home (409); a
    session that is not a no-project top-level chat (409); one owned by
    another worker (409); one with a turn running (409); a project the caller
    cannot reach or that has no checkout on this worker (403/404 via origin
    access). A lost race with another attach also answers 409.
    """
    from app.core import archive_guard, streaming_status
    from app.core.session_modes import session_mode_of

    session = await require_session(request, session_id)
    if archive_guard.is_archived_row(session):
        raise HTTPException(status.HTTP_409_CONFLICT, "This chat is archived. Restore it first.")
    await refuse_assistant_home_mutation(session_id, "given a project")
    if (
        session_mode_of(session) != "chat"
        or session.get("project_id")
        or session.get("project_v2_id")
    ):
        raise HTTPException(status.HTTP_409_CONFLICT, ALREADY_HAS_PROJECT)
    if session.get("is_worker") or session.get("parent_session_id"):
        raise HTTPException(
            status.HTTP_409_CONFLICT, "A subagent's chat can't be given a project."
        )
    if not _owned_here(session):
        raise HTTPException(status.HTTP_409_CONFLICT, NOT_HERE)
    if await run_sync(streaming_status.has_active_run, session_id):
        raise HTTPException(status.HTTP_409_CONFLICT, BUSY)

    access = await run_sync(
        require_origin_access,
        api_user=get_api_user_from_request(request),
        project_id=body.project_id,
    )
    project = access.get("legacy")
    if not project:
        raise HTTPException(status.HTTP_404_NOT_FOUND, NEEDS_LOCAL_CHECKOUT)
    project_v2_id = (access.get("project_v2") or {}).get("id")

    row = await run_sync(
        database.attach_session_project, session_id, project["id"], project_v2_id
    )
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
    try:
        from app.core.session_pool import get_session_pool
        await get_session_pool().remove(session_id, preserve_session=True)
    except Exception:  # noqa: BLE001 — a stale client is refreshed on its next turn
        logger.warning("Could not release the live client for %s", session_id, exc_info=True)

    try:
        from app.core.sync_engine import sync_engine
        await sync_engine.broadcast_user_session_updated(
            user_id=session.get("api_user_id") or "admin",
            session_id=session_id,
            updates={"project_id": project["id"], "project_v2_id": project_v2_id, "mode": None},
            source_device_id=None,
        )
    except Exception:  # noqa: BLE001 — the write already committed
        logger.warning("Could not broadcast the attach for %s", session_id, exc_info=True)

    return AttachProjectResponse(
        session_id=session_id,
        project_id=project["id"],
        project_v2_id=project_v2_id,
        mode="code",
    )
