"""Owner-routed live run settings; never interrupt or reconnect a turn."""
import asyncio
import logging
from typing import Literal, Optional
from weakref import WeakValueDictionary

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, StrictBool

from app.api.auth import require_auth
from app.api.sessions_helpers import require_session
from app.db import database
from app.db.async_database import run_sync
from app.network import control

router = APIRouter()
logger = logging.getLogger(__name__)
_locks = WeakValueDictionary()


class RunSettingsUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    effort: Optional[Literal["none", "minimal", "low", "medium", "high", "xhigh", "max", "ultra"]] = None
    fast: Optional[StrictBool] = None


async def apply_run_settings(session_id: str, body: RunSettingsUpdate) -> dict:
    from app.core.session_pool import get_session_pool
    from app.core.session_runtime import get_live_runtime
    from app.core.model_effort import normalize_effort
    from app.core.provider.harness_adapter import _decide_provider_kind
    from app.core.provider.types import SessionOptions
    from app.db.model_providers import get_model_provider

    from app.core.assistant_service import is_primary_session

    changes = body.model_dump(exclude_unset=True)
    if not changes or ("fast" in changes and changes["fast"] is None):
        raise ValueError("Provide effort and/or a boolean fast setting")
    lock = _locks.setdefault(session_id, asyncio.Lock())
    async with lock:
        session = await run_sync(control._require_owned, session_id)
        # The assistant's home always runs at its model's default effort.
        home = await run_sync(is_primary_session, session_id)
        if home and "effort" in changes:
            raise ValueError("The assistant always uses its model's default effort")
        state = get_session_pool().peek(session_id)
        runtime = get_live_runtime(session_id)
        connected = state is not None and state.is_connected
        if runtime is not None and not connected:
            raise ValueError("The session is still connecting. Try again in a moment.")
        options = getattr(state.adapter, "_options", None) if connected else None
        old = {"effort": options.effort if options else None if home else session.get("effort"),
               "fast": bool(options.fast if options else session.get("fast"))}
        selected = {**old, **changes}
        if connected:
            selected = await state.adapter.set_run_settings(**selected)
            if get_session_pool().peek(session_id) is not state or not state.is_connected:
                raise RuntimeError("Session connection changed before the setting was confirmed")
        else:
            model = session.get("model")
            provider_id = session.get("provider_id")
            if model and model.startswith("prov:"):
                _, provider_id, model = model.split(":", 2)
            provider = await run_sync(get_model_provider, provider_id) if provider_id else None
            route = await run_sync(_decide_provider_kind, SessionOptions(
                model=model, extra={"provider_id": provider_id} if provider_id else {}))
            effort = normalize_effort(selected["effort"], model, provider, route=route)
            if selected["effort"] is not None and effort is None:
                raise ValueError("This model does not support the selected effort level")
            if selected["fast"] and route not in ("delegated", "openai_responses"):
                raise ValueError("Fast mode is not supported by this provider")
            selected["effort"] = effort
        # The home never stores an effort; its turns carry none either.
        stored = {**selected, "effort": None} if home else selected
        try:
            await run_sync(database.set_session_run_binding, session_id, **stored,
                           clear_effort=stored["effort"] is None)
        except Exception:
            if connected:
                await state.adapter.set_run_settings(**old)
            raise
        runtime = get_live_runtime(session_id)
        if runtime is not None:
            turn_overrides = {**(runtime.turn.get("overrides") or {}), **selected}
            if home:
                turn_overrides.pop("effort", None)
            runtime.turn["overrides"] = turn_overrides
        if connected and state.connect_options is not None:
            state.connect_options.effort = selected["effort"]
            state.connect_options.aih_fast = selected["fast"]
        return selected


async def handle_run_settings(body: dict, from_device: str) -> dict:
    session_id = body.get("session_id")
    if not isinstance(session_id, str) or not session_id:
        raise ValueError("session_id is required")
    return await apply_run_settings(session_id, RunSettingsUpdate.model_validate(
        {key: value for key, value in body.items() if key != "session_id"}))


@router.put("/{session_id}/run-settings")
async def update_run_settings(
    request: Request, session_id: str, body: RunSettingsUpdate,
    token: str = Depends(require_auth),
):
    session = await require_session(request, session_id)
    owner = await run_sync(control.owner_of, session)
    try:
        if owner is not None:
            return await control.call(owner, "run-settings", {
                "session_id": session_id, **body.model_dump(exclude_unset=True),
            })
        return await apply_run_settings(session_id, body)
    except (control.ControlRefused, ValueError) as exc:
        raise HTTPException(409, str(exc)) from exc
    except Exception as exc:
        logger.exception("Could not change run settings for %s", session_id)
        raise HTTPException(503, "Could not confirm the setting change. Retry when the session is connected.") from exc


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
        try:
            from app.core.chat_access import broker
            await broker.resolve_mode_actions(session_id)
        except Exception:
            logger.warning("Mode applied but private-access confirmations could not be released for %s",
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


class AwayModeUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    away: StrictBool


async def apply_away_mode(session_id: str, away: bool) -> dict:
    """Persist Away mode on the owning worker; read per question, so no reconnect.

    Turning it on also answers questions already waiting on the owner with their
    recommended options, so a session parked before the owner left resumes.
    """
    await run_sync(control._require_owned, session_id)
    await run_sync(database.set_session_run_binding, session_id, away_mode=away)
    released = 0
    if away:
        try:
            from app.core.away_mode import release_pending_questions
            released = await release_pending_questions(session_id)
        except Exception:
            logger.warning("Away mode set but pending questions were not released for %s",
                           session_id, exc_info=True)
    return {"away": away, "released_questions": released}


async def handle_away_mode(body: dict, from_device: str) -> dict:
    session_id = body.get("session_id")
    if not isinstance(session_id, str) or not session_id:
        raise ValueError("session_id is required")
    return await apply_away_mode(session_id, AwayModeUpdate.model_validate(
        {key: value for key, value in body.items() if key != "session_id"}).away)


@router.put("/{session_id}/away-mode")
async def update_away_mode(
    request: Request, session_id: str, body: AwayModeUpdate,
    token: str = Depends(require_auth),
):
    session = await require_session(request, session_id)
    owner = await run_sync(control.owner_of, session)
    try:
        if owner is not None:
            return await control.call(owner, "away-mode", {"session_id": session_id, "away": body.away})
        return await apply_away_mode(session_id, body.away)
    except control.RunnerUnreachable as exc:
        raise HTTPException(503, "The session's device is unreachable; Away mode was not changed.") from exc
    except (control.ControlRefused, ValueError) as exc:
        raise HTTPException(409, str(exc)) from exc


class AutoMergeUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    enabled: StrictBool
    method: Literal["merge", "squash", "rebase"] = "squash"


@router.get("/{session_id}/auto-merge")
async def get_auto_merge(request: Request, session_id: str, token: str = Depends(require_auth)):
    """Whether this worker is watching the session's PR to merge it when green."""
    await require_session(request, session_id)
    from app.core import auto_merge
    return auto_merge.status(session_id)


@router.put("/{session_id}/auto-merge")
async def update_auto_merge(request: Request, session_id: str, body: AutoMergeUpdate,
                            token: str = Depends(require_auth)):
    """Turn 'Merge when green' on or off for the session's open PR."""
    session = await require_session(request, session_id)
    if body.enabled:
        # A PR action. Turning it off stays possible so nothing is stranded on.
        from app.api.git_owner import refuse_subagent_git
        refuse_subagent_git(session)
    from app.core import auto_merge
    try:
        if body.enabled:
            return await auto_merge.enable(session_id, body.method)
        return await auto_merge.disable(session_id)
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
