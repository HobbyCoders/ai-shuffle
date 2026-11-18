"""Owner-routed live run settings; never interrupt or reconnect a turn."""
import asyncio
import logging
from typing import Any, Literal, Optional
from weakref import WeakValueDictionary

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field, StrictBool

from app.api.auth import require_auth
from app.api.sessions_helpers import require_session
from app.db import database
from app.db.async_database import run_sync
from app.network import control

router = APIRouter()
logger = logging.getLogger(__name__)
_FAST_WAIT_READ_SECONDS = 1.5
_locks = WeakValueDictionary()


def settings_lock(session_id: str) -> asyncio.Lock:
    """The chat's lock for saving a setting and announcing what was saved.

    Turn admission takes it too when a send saves a speed pick, so the chat's
    clients hear speed pairs in the order they were saved. Held weakly: it
    goes away once nothing holds or waits on it.
    """
    return _locks.setdefault(session_id, asyncio.Lock())
#: Running fast-mode hand-downs (strong references until they finish).
_fast_propagations: set = set()


class RunSettingsUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    effort: Optional[Literal["none", "minimal", "low", "medium", "high", "xhigh", "max", "ultra"]] = None
    fast: Optional[StrictBool] = None
    # The speed tier (app/core/model_speed.py). Wins over ``fast`` and sets it;
    # a request with only ``fast`` (an older client) keeps an Ultrafast tier
    # when it turns fast on, and turning it off is Standard.
    speed: Optional[Literal["standard", "fast", "ultrafast"]] = None
    context_window: Optional[int] = Field(None, strict=True, gt=0)


async def _store_live_overrides(session_id: str, runtime) -> None:
    """Write the live turn's new binding to its row as well as the runtime.

    An answer to the turn's question, a peer message or a wake copies the
    row's binding. Left stale, it differs from the running turn (one Fast mode
    toggle adds ``effort``), so ``SessionRuntime.steer_pending`` refuses it and
    it waits for the whole turn to end. The change is already applied, so a
    failed write is logged rather than undone.
    """
    from app.db import turns as turns_db

    turn_id = getattr(runtime, "turn_id", None)
    if not turn_id:
        return
    try:
        await run_sync(turns_db.set_live_turn_overrides, turn_id, runtime.turn.get("overrides") or {})
    except Exception:  # noqa: BLE001 — the live setting stands either way
        logger.warning("Could not store the changed run settings on turn %s of %s",
                       turn_id, session_id, exc_info=True)


#: What an owner older than inherited ``speed`` answers when handed one.
_FAST_ONLY_HAND_DOWN = "A parent hands down only fast mode"


def _session_model(session: dict) -> Optional[str]:
    """The model a subagent runs: its own, else its agent's. Blocking."""
    model = session.get("model") or session.get("launched_model")
    if not model and session.get("profile_id"):
        model = ((database.get_profile(session["profile_id"]) or {}).get("config") or {}).get("model")
    return model


def _inherited_fast_skip(session: dict) -> Optional[str]:
    """Why a parent's speed change leaves this subagent alone, or None.

    ``pinned``: the owner set this subagent's speed in its own chat.
    ``unsupported``: its model has no fast mode, so it keeps its own setting.
    Blocking.
    """
    from app.core.model_override import fast_mode_supported

    if session.get("fast_pinned"):
        return "pinned"
    if not fast_mode_supported(_session_model(session), session.get("provider_id")):
        return "unsupported"
    return None


def _inherited_tier(session: dict, tier: str) -> str:
    """A parent's tier fitted to this subagent, never above it. Blocking.

    Ultrafast stays Ultrafast only where the subagent's own row offers it
    for its model; anywhere else it runs on Fast.
    """
    from app.core.model_override import ultrafast_supported
    from app.core.model_speed import FAST, ULTRAFAST

    if tier == ULTRAFAST and not ultrafast_supported(_session_model(session), session.get("provider_id")):
        return FAST
    return tier


async def _hand_down_remote(owner: Any, child_id: str, tier: str) -> Any:
    """Hand ``tier`` to a subagent another worker runs.

    Fast and Standard travel as ``fast`` alone, as every owner reads it. An
    Ultrafast tier adds ``speed``; an owner older than that refuses it, so it
    is asked again with ``fast`` alone and runs the subagent on Fast.
    """
    from app.core.model_speed import STANDARD, ULTRAFAST

    body = {"session_id": child_id, "fast": tier != STANDARD, "inherited": True}
    if tier != ULTRAFAST:
        return await control.call(owner, "run-settings", body)
    try:
        return await control.call(owner, "run-settings", {**body, "speed": tier})
    except (control.ControlRefused, ValueError) as exc:
        if not (_older_owner_refused_speed(exc) or _FAST_ONLY_HAND_DOWN in str(exc)):
            raise
        logger.info("Owner of %s predates inherited Ultrafast; handing down Fast", child_id)
        return await control.call(owner, "run-settings", body)


#: One hand-down at a time per parent (held weakly, like ``settings_lock``).
_propagation_locks = WeakValueDictionary()


def _current_tier(session_id: str) -> Optional[str]:
    """The tier ``session_id`` hands its subagents now. Blocking.

    Its live turn's, else its binding's (``provider_agent._caller_speed``). A
    worker whose model has no Fast hands down Standard, whatever its row says.
    """
    from app.core.model_override import fast_mode_supported
    from app.core.model_speed import STANDARD
    from app.core.provider_agent import _caller_speed

    session = database.get_session(session_id) or {}
    if session.get("is_worker") and not fast_mode_supported(
            _session_model(session), session.get("provider_id")):
        return STANDARD
    return _caller_speed(session, live_only=True)


async def _hand_down_remote_latest(session_id: str, owner: Any, child_id: str, fallback: str) -> None:
    """Send ``session_id``'s tier to a subagent another worker runs. Never raises.

    Runs outside the parent's lock, so a silent peer never holds up the
    parent's next hand-down to its local workers. Sends to one subagent run
    one at a time and read the parent's tier just before sending, so they go
    out in order and the last carries the parent's latest tier. A send that
    waited while the parent was deleted, or the subagent stopped being its
    direct worker, is dropped: the subagent is no longer the parent's to set.
    """
    async with _propagation_locks.setdefault(("remote", child_id), asyncio.Lock()):
        try:
            children = await run_sync(database.list_worker_children, session_id)
        except Exception:  # noqa: BLE001 — as the fan-out does when it cannot list
            logger.warning("Could not list the subagents of %s for speed", session_id, exc_info=True)
            return
        if not any(child.get("id") == child_id for child in children or ()):
            logger.info("Subagent %s is no longer a worker of %s; speed not handed down",
                        child_id, session_id)
            return
        try:
            tier = await run_sync(_current_tier, session_id)
        except Exception:  # noqa: BLE001 — the tier the fan-out read
            logger.debug("Could not read the speed of %s", session_id, exc_info=True)
            tier = None
        try:
            # Its owner hands it on to that subagent's own workers.
            await _hand_down_remote(owner, child_id, tier or fallback)
        except Exception:  # noqa: BLE001 — the subagent keeps its setting
            logger.info("Speed was not handed to subagent %s of %s", child_id, session_id,
                        exc_info=True)


async def propagate_fast(session_id: str, fast: bool = False, _seen: Optional[set] = None, *,
                         speed: Optional[str] = None) -> None:
    """Hand a speed change down to the subagents ``session_id`` dispatched.

    Each one takes ``session_id``'s tier as it stands when this hand-down
    holds the parent's lock (``fast``/``speed`` only if it cannot be read),
    fitted to what it can run (:func:`_inherited_tier`), except one the owner
    pinned in its own chat and one whose model has no fast mode. Then each
    local subagent hands its own tier on to the workers it dispatched, the
    same way. Hand-downs of one parent run one at a time and read its tier
    under the lock, so the last one to run hands down the last switch, and a
    worker is only ever written by the session that dispatched it.

    A worker in the middle of a turn finishes that turn at the speed it began
    with and says so in its own chat; its queued turns take the new tier when
    they start (``turns._session_bound_overrides``). A worker run by another
    worker gets it through that worker's ``run-settings`` action, which hands
    it on to its own subagents there; that send runs in its own task, outside
    the parent's lock (:func:`_hand_down_remote_latest`). Never raises: a
    subagent that cannot take the change keeps its setting.
    """
    from app.core.model_speed import normalize_speed, resolve_speed

    seen = _seen if _seen is not None else {session_id}
    local, remote = [], []
    async with _propagation_locks.setdefault(session_id, asyncio.Lock()):
        try:
            children = await run_sync(database.list_worker_children, session_id)
        except Exception:  # noqa: BLE001 — the parent's own change stands
            logger.warning("Could not list the subagents of %s for speed", session_id, exc_info=True)
            return
        if not children:
            return
        try:
            tier = await run_sync(_current_tier, session_id)
        except Exception:  # noqa: BLE001 — the tier the hand-down was asked for
            logger.debug("Could not read the speed of %s", session_id, exc_info=True)
            tier = None
        tier = tier or normalize_speed(speed) or resolve_speed(fast)
        for child in children:
            child_id = child.get("id")
            if not child_id or child_id in seen:
                continue
            seen.add(child_id)
            try:
                owner = await run_sync(control.owner_of, child)
                if owner is not None:
                    remote.append(_spawn(_hand_down_remote_latest(session_id, owner, child_id, tier)))
                    continue
                result = await apply_run_settings(
                    child_id, RunSettingsUpdate(fast=tier != "standard", speed=tier),
                    inherited=True, propagate=False)
                # Pinned or without Fast, it hands down what it runs.
                local.append((child_id, result.get("speed")
                              or resolve_speed(child.get("fast"), child.get("speed"))))
            except Exception:  # noqa: BLE001 — one subagent never blocks the rest
                logger.info("Speed was not handed to subagent %s of %s", child_id, session_id,
                            exc_info=True)
    # Outside this parent's lock: each reads its own tier under its own lock,
    # so the order these land in does not matter.
    for child_id, child_tier in local:
        await propagate_fast(child_id, _seen=seen, speed=child_tier)
    if remote:
        # Holds no lock; ``asyncio.wait`` leaves the sends running if this is cancelled.
        await asyncio.wait(remote)


def _spawn(coro) -> asyncio.Task:
    """Run ``coro`` as a hand-down task, held until it finishes."""
    task = asyncio.get_running_loop().create_task(coro)
    _fast_propagations.add(task)
    task.add_done_callback(_fast_propagations.discard)
    return task


def _start_propagation(session_id: str, fast: bool, speed: Optional[str] = None) -> None:
    _spawn(propagate_fast(session_id, fast, speed=speed))


async def apply_run_settings(session_id: str, body: RunSettingsUpdate, *, inherited: bool = False,
                             propagate: bool = True, announce: bool = False) -> dict:
    """Change a session's live run settings.

    A speed change is then handed down to the session's subagents in the
    background (:func:`propagate_fast`), unless ``propagate`` is False.
    ``inherited`` marks a change handed down from a parent: it carries only
    the speed (``fast``, and ``speed`` from a parent that knows the tier),
    fitted to this subagent (Ultrafast runs on Fast where its row has none).
    A subagent the owner pinned, or one whose model has no fast mode, answers
    ``{"skipped": reason}`` and is left unchanged. The owner's own change to
    a subagent pins it.

    ``fast_applies_turn`` in the answer names the running turn when that turn
    began under another Fast setting, so the new one takes effect with the next
    message. It is absent when the setting already holds: nothing is running,
    or the lane switches at its next request (see
    ``HarnessAdapter.set_run_settings``). ``GET /live`` reports the same, so a
    reload or another device can say so too. ``announce`` tells this worker's
    clients of the chat what was saved (:func:`_announce_run_settings`) before
    the chat's lock is let go.
    """
    result = await _apply_run_settings(session_id, body, inherited=inherited, announce=announce)
    if propagate and ({"fast", "speed"} & body.model_fields_set) and "skipped" not in result:
        _start_propagation(session_id, bool(result["fast"]), result.get("speed"))
    return result


def _checked_changes(body: RunSettingsUpdate) -> dict:
    """The changes ``body`` asks for, refused when it names none or a null pick."""
    changes = body.model_dump(exclude_unset=True)
    if not changes or ("fast" in changes and changes["fast"] is None) or (
            "speed" in changes and changes["speed"] is None):
        raise ValueError("Provide effort, a boolean fast setting, a speed, or a context_window")
    return changes


async def _apply_run_settings(session_id: str, body: RunSettingsUpdate, *, inherited: bool,
                              announce: bool = False) -> dict:
    from app.core.session_pool import get_session_pool
    from app.core.session_runtime import get_live_runtime
    from app.core.model_effort import normalize_effort
    from app.core.provider.harness_adapter import _decide_provider_kind
    from app.core.provider.types import SessionOptions
    from app.db.model_providers import resolve_model_provider

    from app.core.model_speed import resolve_speed, stored_pair

    changes = _checked_changes(body)
    picks_speed = "fast" in changes or "speed" in changes
    asked = {"effort_asked": "effort" in changes, "speed_asked": picks_speed}
    async with settings_lock(session_id):
        session = await run_sync(control._require_owned, session_id)
        if inherited:
            if not picks_speed or set(changes) - {"fast", "speed"}:
                raise ValueError("A parent hands down only its speed")
            skip = await run_sync(_inherited_fast_skip, session)
            if skip:
                return {"skipped": skip}
            # The parent's tier (``fast`` alone from an older parent: on is
            # Fast, never a tier the child's row still remembers), fitted to
            # what this subagent can run.
            tier = changes.get("speed") or resolve_speed(changes.get("fast"))
            tier = await run_sync(_inherited_tier, session, tier)
            changes.update(fast=tier != "standard", speed=tier)
        if "context_window" in changes:
            # Unlike effort, a window is connect-time state on BOTH legs. Save
            # it now, but never interrupt/reconfigure an in-flight request. The
            # next turn reads the binding and the pool reconnects if it changed.
            if len(changes) != 1:
                raise ValueError("Change the context window separately from effort/Fast mode")
            from app.core.model_registry import offered_context_window

            window = await run_sync(offered_context_window, session, changes["context_window"])
            if window is None:
                raise ValueError("This model does not support the selected context window")
            await run_sync(database.set_session_run_binding, session_id, context_window=window)
            if announce:
                await _announce_run_settings(session_id, {"context_window": window}, **asked)
            return {"context_window": window}
        state = get_session_pool().peek(session_id)
        runtime = get_live_runtime(session_id)
        connected = state is not None and state.is_connected
        if runtime is not None and not connected:
            raise ValueError("The session is still connecting. Try again in a moment.")
        options = getattr(state.adapter, "_options", None) if connected else None
        old = {"effort": options.effort if options else session.get("effort"),
               "fast": bool(options.fast if options else session.get("fast"))}
        old["speed"] = resolve_speed(old["fast"], getattr(options, "speed", None) if options else session.get("speed"))
        if picks_speed:
            # One rule for the stored pair: a speed pick sets fast; fast alone
            # (an older client) keeps Ultrafast only when it already runs it.
            changes.update(stored_pair(changes.get("fast"), changes.get("speed"), prior=old))
        selected = {**old, **changes}
        if connected:
            selected = {"speed": selected["speed"], **await state.adapter.set_run_settings(**selected)}
            if get_session_pool().peek(session_id) is not state or not state.is_connected:
                raise RuntimeError("Session connection changed before the setting was confirmed")
        else:
            model = session.get("model")
            provider_id = session.get("provider_id")
            if model and model.startswith("prov:"):
                _, provider_id, model = model.split(":", 2)
            provider = await run_sync(resolve_model_provider, provider_id) if provider_id else None
            route = await run_sync(_decide_provider_kind, SessionOptions(
                model=model, extra={"provider_id": provider_id} if provider_id else {}))
            effort = normalize_effort(selected["effort"], model, provider, route=route)
            if selected["effort"] is not None and effort is None:
                raise ValueError("This model does not support the selected effort level")
            if selected["fast"] and route not in ("delegated", "openai_responses"):
                raise ValueError("Fast mode is not supported by this provider")
            if selected["speed"] == "ultrafast":
                from app.core.model_speed import ultrafast_supported_for_row

                if route != "openai_responses" or not ultrafast_supported_for_row(provider, model):
                    raise ValueError("Ultrafast is not available for this model")
            selected["effort"] = effort
        stored = selected
        # The owner's own pick on a subagent pins it: a parent's later toggle
        # leaves it alone.
        pin = ({"fast_pinned": True}
               if not inherited and picks_speed and session.get("is_worker") else {})
        try:
            await run_sync(database.set_session_run_binding, session_id, **stored,
                           clear_effort=stored["effort"] is None, **pin)
        except Exception:
            if connected:
                await state.adapter.set_run_settings(**old)
            raise
        runtime = get_live_runtime(session_id)
        if runtime is not None:
            turn_overrides = {**(runtime.turn.get("overrides") or {}), **selected}
            runtime.turn["overrides"] = turn_overrides
            await _store_live_overrides(session_id, runtime)
        if connected and state.connect_options is not None:
            state.connect_options.effort = selected["effort"]
            state.connect_options.aih_fast = selected["fast"]
            state.connect_options.aih_speed = selected["speed"]
        waiting = fast_waiting_turn(session_id)
        if announce:
            # Under the lock, so a send admitted meanwhile announces after this.
            await _announce_run_settings(session_id, selected, **asked)
        return {**selected, "fast_applies_turn": waiting} if waiting else selected


def fast_waiting_turn(session_id: str) -> Optional[str]:
    """The running turn a Fast change is waiting out on this worker, or None.

    A change has already been applied when this is asked and a chat can be
    opened while it runs, so no adapter or runtime that cannot say may fail
    either: it just has nothing to report.
    """
    from app.core.session_pool import get_session_pool
    from app.core.session_runtime import get_live_runtime

    try:
        state = get_session_pool().peek(session_id)
        runtime = get_live_runtime(session_id)
        if state is None or not state.is_connected or runtime is None:
            return None
        waits = getattr(state.adapter, "fast_waits_for_next_turn", None)
        if waits is None or waits() is not True:
            return None
        turn_id = getattr(runtime, "turn_id", None)
        return turn_id if isinstance(turn_id, str) and turn_id else None
    except Exception:  # noqa: BLE001 - a notice is never worth a failed request
        logger.warning("Could not read the Fast wait of %s", session_id, exc_info=True)
        return None


async def handle_fast_wait(body: dict, from_device: str) -> dict:
    """Peer read of :func:`fast_waiting_turn` for a chat this worker runs."""
    session_id = body.get("session_id")
    if not isinstance(session_id, str) or not session_id:
        raise ValueError("session_id is required")
    await run_sync(control._require_owned, session_id)
    return {"fast_applies_turn": fast_waiting_turn(session_id)}


async def read_fast_waiting_turn(session: dict, session_id: str) -> Optional[str]:
    """:func:`fast_waiting_turn` wherever the chat runs. Bounded, and never raises:
    an owner that is slow, unreachable or too old to answer means no notice."""
    try:
        owner = await run_sync(control.owner_of, session)
        if owner is None:
            return fast_waiting_turn(session_id)
        answer = await asyncio.wait_for(
            control.call(owner, "fast-wait", {"session_id": session_id}), _FAST_WAIT_READ_SECONDS)
        turn_id = answer.get("fast_applies_turn") if isinstance(answer, dict) else None
        return turn_id if isinstance(turn_id, str) and turn_id else None
    except Exception:  # noqa: BLE001 - see the docstring
        logger.debug("Could not read the Fast wait of %s", session_id, exc_info=True)
        return None


async def _announce_run_settings(session_id: str, result: dict, *, effort_asked: bool,
                                 speed_asked: bool = False) -> None:
    """Tell this worker's open clients of the chat about a saved setting pick.

    Without it another tab, the phone or the other worker's browser keeps its
    older window or effort until a reload, and its next send carries it and
    pins it again (#1895, #1579). Effort goes out only when the request set it,
    an explicit Auto (``None``) included; ``fast`` and ``speed`` likewise, as a
    pair. Every socket gets it, the picker's own too: the REST call names no device, and the value is the one the picker
    already applied. A client that does not know a field reloads the row.
    """
    if not isinstance(result, dict):
        return
    updates: dict = {}
    if result.get("context_window"):
        updates["context_window"] = result["context_window"]
    if effort_asked and "effort" in result:
        updates["effort"] = result["effort"]
    if speed_asked and ("fast" in result or "speed" in result):
        # The tier the chat runs on: a cleared pair is Standard, and an owner
        # older than ``speed`` answers without it (Fast at most).
        from app.core.model_speed import STANDARD, resolve_speed

        updates["speed"] = resolve_speed(result.get("fast"), result.get("speed"))
        updates["fast"] = updates["speed"] != STANDARD
    if not updates:
        return
    try:
        from app.core.sync_engine import sync_engine
        await sync_engine.broadcast_session_updated(
            session_id=session_id, updates=updates, source_device_id=None)
    except Exception:  # noqa: BLE001 — the pick is saved either way
        logger.warning("Could not announce the run settings of %s", session_id, exc_info=True)


async def handle_run_settings(body: dict, from_device: str) -> dict:
    session_id = body.get("session_id")
    if not isinstance(session_id, str) or not session_id:
        raise ValueError("session_id is required")
    result = await apply_run_settings(session_id, RunSettingsUpdate.model_validate(
        {key: value for key, value in body.items() if key not in ("session_id", "inherited")}),
        inherited=body.get("inherited") is True, announce=True)
    return result


def _older_owner_refused_speed(exc: Exception) -> bool:
    """Did an owner that predates ``speed`` refuse it as an unknown field?

    Its ``RunSettingsUpdate`` forbids extra fields, so pydantic names the field
    on one line and "Extra inputs are not permitted" on the next.
    """
    lines = [line.strip().lower() for line in str(exc).splitlines()]
    return any(line == "speed" and "extra inputs are not permitted" in following
               for line, following in zip(lines, lines[1:]))


async def _call_owner_run_settings(owner: Any, session_id: str, changes: dict) -> dict:
    """``run-settings`` on the worker that runs the chat.

    A ``speed`` pick also sends the ``fast`` it implies. An owner older than
    ``speed`` refuses the field, so it is asked again with ``fast`` alone: it
    runs an Ultrafast pick on Fast rather than failing the change.
    """
    if changes.get("speed") is not None:
        changes = {**changes, "fast": changes["speed"] != "standard"}
    try:
        return await control.call(owner, "run-settings", {"session_id": session_id, **changes})
    except (control.ControlRefused, ValueError) as exc:
        if "speed" not in changes or not _older_owner_refused_speed(exc):
            raise
        logger.info("Owner of %s predates the speed setting; sending fast only", session_id)
        legacy = {key: value for key, value in changes.items() if key != "speed"}
        return await control.call(owner, "run-settings", {"session_id": session_id, **legacy})


@router.put("/{session_id}/run-settings")
async def update_run_settings(
    request: Request, session_id: str, body: RunSettingsUpdate,
    token: str = Depends(require_auth),
):
    session = await require_session(request, session_id)
    owner = await run_sync(control.owner_of, session)
    try:
        if owner is not None:
            result = await _call_owner_run_settings(owner, session_id, _checked_changes(body))
        else:
            return await apply_run_settings(session_id, body, announce=True)
    except (control.ControlRefused, ValueError) as exc:
        raise HTTPException(409, str(exc)) from exc
    except Exception as exc:
        logger.exception("Could not change run settings for %s", session_id)
        raise HTTPException(503, "Could not confirm the setting change. Retry when the session is connected.") from exc
    # Another worker runs the chat and announced it there; tell this one's clients.
    await _announce_run_settings(session_id, result, effort_asked="effort" in body.model_fields_set,
                                 speed_asked=bool({"fast", "speed"} & body.model_fields_set))
    return result


class PermissionModeUpdate(BaseModel):
    permission_mode: Literal["default", "acceptEdits", "bypassPermissions", "auto"]


async def apply_permission_mode(session_id: str, mode: str) -> dict:
    from app.core.session_modes import (
        permission_ceiling, permission_mode_for_session_mode, strictest_permission_mode,
    )
    from app.core.session_pool import get_session_pool
    from app.core.session_runtime import get_live_runtime
    from app.core.permission_handler import permission_handler

    async with settings_lock(session_id):
        session = await run_sync(control._require_owned, session_id)
        mode = permission_mode_for_session_mode(mode, session.get("mode"))
        state = get_session_pool().peek(session_id)
        runtime = get_live_runtime(session_id)
        context = runtime.turn if runtime else None
        if context is None and session.get("is_worker"):
            from app.db import turns
            context = await run_sync(turns.get_latest_run_turn, session_id)
        ceiling = permission_ceiling((context or {}).get("overrides"), session)
        if ceiling:
            mode = strictest_permission_mode(mode, ceiling)
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
            runtime.turn["overrides"] = {**(runtime.turn.get("overrides") or {}), "permission_mode": mode,
                                         "_permission_explicit": False}
            await _store_live_overrides(session_id, runtime)
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
