"""Owner-routed permission changes; never interrupt or reconnect a live turn."""
import asyncio
import logging
from typing import Literal
from weakref import WeakValueDictionary

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from app.api.auth import require_auth
from app.api.sessions_helpers import require_session
from app.db import database
from app.db.async_database import run_sync
from app.network import control

router = APIRouter()
logger = logging.getLogger(__name__)
_locks = WeakValueDictionary()


class PermissionModeUpdate(BaseModel):
    permission_mode: Literal["default", "acceptEdits", "bypassPermissions", "auto"]


async def apply_permission_mode(session_id: str, mode: str) -> dict:
    from app.core.session_modes import permission_mode_for_session_mode
    from app.core.session_pool import get_session_pool
    from app.core.session_runtime import get_live_runtime
    from app.core.permission_handler import permission_handler

    lock = _locks.setdefault(session_id, asyncio.Lock())
    async with lock:
        session = await run_sync(control._require_owned, session_id)
        mode = permission_mode_for_session_mode(mode, session.get("mode"))
        state = get_session_pool().peek(session_id)
        runtime = get_live_runtime(session_id)
        if runtime and (not state or not state.is_connected):
            raise ValueError("The session is still connecting. Try changing permissions again in a moment.")
        connected = state is not None and state.is_connected
        old_mode = state.callback_refs.get("permission_mode", "default") if connected else None
        if connected:
            # Do not hold the turn's execution lock: it may be awaiting approval.
            await state.adapter.set_permission_mode(mode)
        try:
            await run_sync(database.set_session_run_binding, session_id, permission_mode=mode)
        except Exception:
            if connected:
                await state.adapter.set_permission_mode(old_mode)
            raise
        if runtime is not None:
            runtime.turn["overrides"] = {**(runtime.turn.get("overrides") or {}), "permission_mode": mode}
        if connected:
            state.callback_refs["permission_mode"] = mode
            if state.connect_options is not None:
                state.connect_options.permission_mode = mode
            try:
                await permission_handler.resolve_mode_permissions(session_id, state)
            except Exception:
                # The mode is applied. A failed prompt sweep must not make the
                # UI claim otherwise; remaining prompts can still be answered.
                logger.warning("Mode applied but pending prompts could not be released for %s",
                               session_id, exc_info=True)
        return {"permission_mode": mode}


async def handle(body: dict, from_device: str) -> dict:
    update = PermissionModeUpdate.model_validate(body)
    session_id = body.get("session_id")
    if not isinstance(session_id, str) or not session_id:
        raise ValueError("session_id is required")
    return await apply_permission_mode(session_id, update.permission_mode)


@router.put("/{session_id}/permission-mode")
async def update_permission_mode(
    request: Request, session_id: str, body: PermissionModeUpdate,
    token: str = Depends(require_auth),
):
    session = await require_session(request, session_id)
    owner = await run_sync(control.owner_of, session)
    try:
        if owner is not None:
            return await control.call(owner, "permission-mode", {
                "session_id": session_id, **body.model_dump(),
            })
        return await apply_permission_mode(session_id, body.permission_mode)
    except control.RunnerUnreachable as exc:
        raise HTTPException(503, "The session's device is unreachable; permissions were not confirmed.") from exc
    except (control.ControlRefused, ValueError) as exc:
        raise HTTPException(409, str(exc)) from exc
    except Exception as exc:
        logger.exception("Could not change permission mode for %s", session_id)
        raise HTTPException(503, "Could not confirm the permission change. Retry when the session is connected.") from exc
