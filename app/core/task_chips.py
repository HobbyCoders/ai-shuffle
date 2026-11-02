"""Background-task chips: an agent suggests adjacent work, the owner starts it.

``suggest`` records a chip and starts nothing. ``start`` and ``dismiss`` run on
the worker that owns the chat; a mirror forwards them over the control channel.
The chat a chip starts is created by
:func:`app.core.session_orchestration.start_suggested_task`.
"""
import asyncio
import logging
from typing import Any, Dict, Optional, Set

from fluid.tools.builtins.session_orchestration import EFFORT_PROPERTY

from app.db.async_database import run_sync
from app.db import task_chips as store

log = logging.getLogger(__name__)

_ID_LIMIT = 120
_TITLE_LIMIT = 80
_SUMMARY_LIMIT = 240
_PROMPT_LIMIT = 8000
_MAX_ANCESTRY = 12
BASES = ("default", "current")
FRAME_TYPE = "task_chips_changed"

_NOT_OFFERED = ("Suggested tasks are available only in the owner's own chats and their subagents "
                "on this worker. Nothing was recorded; mention the issue in your final report instead.")
_INTERRUPTED = "The app restarted while this task was starting. Nothing is running; try again."
_INTERNAL = "Something went wrong while starting this task. Nothing is running; try again."


class ChipError(Exception):
    """A refused chip action; ``status`` is its HTTP status."""

    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


def _stamp(value: Any) -> Any:
    return value.isoformat() if hasattr(value, "isoformat") else value


def public(chip: Dict[str, Any]) -> Dict[str, Any]:
    """The API shape of a chip."""
    return {
        "id": chip["id"], "session_id": chip["session_id"], "origin_session_id": chip["origin_session_id"],
        "origin_label": chip.get("origin_label"),
        "title": chip["title"], "summary": chip["summary"], "prompt": chip["prompt"],
        "model": chip["model"], "provider_id": chip.get("provider_id"), "model_label": chip.get("model_label"),
        "effort": chip.get("effort"),
        "base": chip["base"], "state": chip["state"], "error": chip.get("error"), "retryable": chip["state"] == store.FAILED,
        "started_session_id": chip.get("started_session_id"),
        "started_session_title": chip.get("started_title"),
        "created_at": _stamp(chip.get("created_at")), "updated_at": _stamp(chip.get("updated_at")),
    }


def _text(data: Dict[str, Any], key: str, limit: int) -> str:
    value = data.get(key)
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        raise ValueError(f"{key} must be a non-empty string of at most {limit} characters")
    return value.strip()


def _owner_root(row: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """The owner's top-level chat at the root of ``row``'s ancestry on this worker, else raises.

    An API user, automation, voice or hidden chat, and a remote subagent (its
    ancestry ends on another worker), are refused however the call arrived.
    """
    from app.core import session_orchestration as orch
    from app.db import sessions

    for _ in range(_MAX_ANCESTRY):
        if (row is None or row.get("api_user_id") or row.get("automation_id")
                or row.get("mode") == "voice"):
            raise PermissionError(_NOT_OFFERED)
        if not row.get("is_worker"):
            break
        row = sessions.get_session(row["parent_session_id"]) if row.get("parent_session_id") else None
    else:
        raise PermissionError(_NOT_OFFERED)
    if not orch.is_owner_session(row) or not orch._local_top_level(row, allow_settled=True):
        if row.get("status") == "archived":
            raise PermissionError("This chat is archived, so it cannot suggest tasks.")
        raise PermissionError(_NOT_OFFERED)
    return row


def row_offers(row: Optional[Dict[str, Any]]) -> bool:
    """Whether a chat with this row lists SuggestTask. Never raises.

    Decides what the model sees, not what is allowed: a row not written yet, or
    any doubt, offers the tool, and :func:`_root_chat` still decides each call.
    """
    try:
        if row is not None:
            _owner_root(row)
        return True
    except PermissionError:
        return False
    except Exception:  # noqa: BLE001
        log.debug("row_offers failed for %s", row.get("id") if row else None, exc_info=True)
        return True


def offered_to(session_id: Optional[str]) -> bool:
    """:func:`row_offers` for a session id. Never raises."""
    from app.db import sessions

    try:
        row = sessions.get_session(session_id) if session_id else None
    except Exception:  # noqa: BLE001
        log.debug("offered_to failed for %s", session_id, exc_info=True)
        return True
    return row_offers(row)


def _root_chat(sender_id: str) -> tuple:
    """``(origin_row, root_row)`` for a caller allowed to suggest, else raises."""
    from app.core import session_orchestration as orch
    from app.core.task_registry import task_registry
    from app.db import sessions

    origin = sessions.get_session(sender_id) if sender_id else None
    row = _owner_root(origin)
    if task_registry.is_user_stopped(row["id"]) or task_registry.is_user_stopped(sender_id):
        raise PermissionError("The user stopped this chat, so it cannot suggest tasks.")
    context = orch._context(sender_id, allow_empty=True)
    if ((context.get("payload") or {}).get("_assistant_proactive") is not None
            or (context.get("overrides") or {}).get("_assistant_proactive") is not None):
        raise PermissionError(_NOT_OFFERED)
    return origin, row


def _origin_label(origin: Dict[str, Any], root: Dict[str, Any]) -> Optional[str]:
    """Which agent raised the chip, so the root chat can say it. None for the root itself."""
    if origin["id"] == root["id"]:
        return None
    return (origin.get("title") or "").strip() or "Subagent"


def _chosen_model(data: Dict[str, Any], origin: Dict[str, Any], root: Dict[str, Any]) -> Dict[str, Any]:
    """The eligible model and effort the agent chose for the new chat, else raises with real choices."""
    from app.core import model_override, session_orchestration as orch
    from app.core.model_effort import resolve_child_effort

    effort = data.get("effort")
    if effort is not None and effort not in EFFORT_PROPERTY["enum"]:
        raise ValueError(f"effort must be one of: {', '.join(EFFORT_PROPERTY['enum'])}. Nothing was recorded.")
    try:
        choice = model_override.resolve(data.get("model"), origin.get("provider_id"), runs_here=True)
    except ValueError as exc:
        raise ValueError(f"{exc} Nothing was recorded.") from None
    overrides = orch._context(root["id"], allow_empty=True).get("overrides") or {}
    return {"model": choice.model, "provider_id": choice.provider_id, "model_label": model_override.label(choice.model),
            "effort": resolve_child_effort(choice.model, choice.provider_id, explicit=effort,
                                           parent=overrides.get("effort", root.get("effort")), caller=root)}


def _record(sender_id: str, data: Dict[str, Any]) -> Dict[str, Any]:
    from app.core.worker_checkout import WorkerCheckoutError, origin_head_commit
    from app.db import session_events

    title = _text(data, "title", _TITLE_LIMIT)
    summary = _text(data, "summary", _SUMMARY_LIMIT)
    prompt = _text(data, "prompt", _PROMPT_LIMIT)
    request_id = _text(data, "request_id", _ID_LIMIT)
    base = data.get("base") or "default"
    if base not in BASES:
        raise ValueError("base must be \"default\" or \"current\"")
    origin, root = _root_chat(sender_id)
    chosen = _chosen_model(data, origin, root)
    base_commit = None
    if base == "current":
        try:
            base_commit = origin_head_commit(origin)
        except WorkerCheckoutError as exc:
            raise ValueError(f"Could not read this chat's committed HEAD: {exc} Use base=\"default\" "
                             "or mention the issue in your final report.") from exc
    result = store.create(
        root["id"], origin_session_id=sender_id, origin_seq=session_events.get_max_seq(root["id"]),
        request_id=request_id, title=title, summary=summary, prompt=prompt, base=base,
        base_commit=base_commit, project_v2_id=origin.get("project_v2_id") or root.get("project_v2_id"),
        origin_label=_origin_label(origin, root), **chosen)
    return result


async def suggest(sender_id: str, data: Dict[str, Any]) -> Dict[str, Any]:
    """SuggestTask's host. Records a chip; the caller's turn carries straight on."""
    result = await run_sync(_record, sender_id, data)
    if result.get("limit"):
        raise ValueError(f"This chat already has {store.MAX_OPEN} open suggestions. Nothing was recorded; "
                         "mention this issue in your final report instead, and carry on with your task.")
    chip = result["chip"]
    if result["created"]:
        await run_sync(notify_changed, chip["session_id"])
    return {
        "chip_id": chip["id"], "state": chip["state"], "created": result["created"],
        "model": chip["model"], "effort": chip.get("effort"),
        "note": ("The owner sees this suggestion as a chip" + ("" if result["created"] else " already")
                 + " and can start it in a separate chat. Do not fix it here. Carry on with your task."),
    }


def notify_changed(session_id: str) -> None:
    """After a commit: tell open clients to re-read, and publish to the network. Never raises.

    The frame carries no chip text; clients render only the authoritative read.
    """
    from app.db.session_events import notify_session_write

    notify_session_write(session_id, "session_task_chips")
    _schedule_broadcast(session_id)


def notify_mirrored(chip_id: str) -> None:
    """A chip arrived from the worker that owns the chat: tell clients here. Never raises."""
    try:
        chip = store.get_any(chip_id)
        if chip is not None:
            _schedule_broadcast(chip["session_id"])
    except Exception:  # noqa: BLE001 — the next read still shows it
        log.debug("Mirrored task chip notification failed for %s", chip_id, exc_info=True)


def _schedule_broadcast(session_id: str) -> None:
    from app.core.global_events import global_events

    loop = getattr(global_events, "_loop", None)
    if loop is None or loop.is_closed():
        return

    def schedule():
        task = loop.create_task(_broadcast(session_id))
        _background.add(task)
        task.add_done_callback(_finished)

    try:
        loop.call_soon_threadsafe(schedule)
    except RuntimeError:
        log.debug("Task chip notification skipped for %s: loop closed", session_id)


_background: Set[asyncio.Task] = set()


def _finished(task: asyncio.Task) -> None:
    _background.discard(task)
    if not task.cancelled() and task.exception() is not None:
        log.warning("Task chip notification failed", exc_info=task.exception())


async def _broadcast(session_id: str) -> None:
    from app.core.sync_engine import SyncEvent, sync_engine

    await sync_engine.broadcast_event(SyncEvent(
        event_type="stream_frame", session_id=session_id,
        data={"type": FRAME_TYPE, "session_id": session_id},
    ))


def list_chips(session_id: str) -> Dict[str, Any]:
    """A chat's chips, plus the chip that started this chat when one did."""
    source = store.source_of(session_id)
    return {"chips": [public(chip) for chip in store.list_for_session(session_id)],
            "source": source and {"chip_id": source["chip_id"], "session_id": source["session_id"],
                                  "title": source.get("title")}}


def fail_interrupted_starts() -> None:
    """Boot: a chip left ``starting`` shows a reason and a retry, not a spinner."""
    from app.core.config import settings
    from app.network.runners import self_device_id
    try:
        sessions = store.fail_interrupted(self_device_id(), settings.effective_instance_id, _INTERRUPTED)
    except Exception:  # noqa: BLE001 — a stale claim also expires by itself
        log.warning("Could not settle interrupted task chips", exc_info=True)
        return
    for session_id in set(sessions):
        notify_changed(session_id)


# One start per chip in this process: a double click's second request waits and
# reads the chat the first made. ``store.claim`` decides the winner durably.
_locks: Dict[str, asyncio.Lock] = {}


async def _after_start(session_id: str, new_id: str) -> None:
    """The chat is committed: run it and show it. A failure here is not a failed start."""
    from app.core.session_orchestration import _wake_peer
    from app.core.sync_engine import sync_engine
    from app.db import sessions

    try:
        await _wake_peer(new_id)
    except Exception:  # noqa: BLE001 — the queued turn is durable; a boot sweep runs it
        log.exception("Task chip chat %s is queued, but waking it failed", new_id)
    try:
        row = await run_sync(sessions.get_session, new_id)
        await sync_engine.broadcast_user_session_created(row.get("api_user_id") or "admin", row["id"])
    except Exception:  # noqa: BLE001
        log.exception("Task chip chat %s is queued, but the sidebar broadcast failed", new_id)


async def _run_start(session_id: str, chip_id: str) -> Dict[str, Any]:
    from app.core.session_orchestration import start_suggested_task

    lock = _locks.setdefault(chip_id, asyncio.Lock())
    try:
        async with lock:
            chip = await run_sync(store.claim, session_id, chip_id)
            if chip is None:
                current = await run_sync(store.get, session_id, chip_id)
                if current is None:
                    raise ChipError(404, "This suggestion no longer exists.")
                if current["state"] == store.DISMISSED:
                    raise ChipError(409, "This suggestion was dismissed.")
                return public(current)  # Started already, or another worker's claim.
            await run_sync(notify_changed, session_id)
            failure: Optional[str] = None
            new_id = None
            try:
                new_id = await asyncio.to_thread(start_suggested_task, chip)
            except (ValueError, PermissionError, LookupError) as exc:
                failure = str(exc) or _INTERNAL
            except Exception:  # noqa: BLE001 — every failure must reach the chip
                log.exception("Starting task chip %s failed", chip_id)
                failure = _INTERNAL
            if failure is not None:
                await run_sync(store.mark_failed, chip_id, chip["attempt"], failure)
            else:
                await _after_start(session_id, new_id)
            await run_sync(notify_changed, session_id)
            return public(await run_sync(store.get, session_id, chip_id))
    finally:
        if not lock.locked() and not getattr(lock, "_waiters", None):
            _locks.pop(chip_id, None)


async def start(session_id: str, chip_id: str) -> Dict[str, Any]:
    """Idempotent. Runs to completion even if the requesting client goes away."""
    task = asyncio.ensure_future(_run_start(session_id, chip_id))
    _background.add(task)
    task.add_done_callback(_background.discard)
    return await asyncio.shield(task)


async def dismiss(session_id: str, chip_id: str) -> Dict[str, Any]:
    chip = await run_sync(store.dismiss, session_id, chip_id)
    if chip is None:
        current = await run_sync(store.get, session_id, chip_id)
        if current is None:
            raise ChipError(404, "This suggestion no longer exists.")
        if current["state"] == store.DISMISSED:
            return public(current)
        raise ChipError(409, "This suggestion has already been started.")
    await run_sync(notify_changed, session_id)
    return public(chip)


async def act(session: Dict[str, Any], chip_id: str, action: str) -> Dict[str, Any]:
    """Run ``action`` where the chat runs, as other session actions are routed."""
    from app.network import control

    owner = await run_sync(control.owner_of, session)
    if owner is not None:
        try:
            return await control.call(owner, "task-chip", {
                "session_id": session["id"], "chip_id": chip_id, "action": action}, timeout=120.0)
        except control.RunnerUnreachable as exc:
            raise ChipError(503, "The worker that owns this chat is offline. Nothing was changed; "
                                 "try again when it is back.") from exc
        except control.ControlRefused as exc:
            raise ChipError(409, "The worker that owns this chat refused this: " + str(exc)) from exc
    return await (start if action == "start" else dismiss)(session["id"], chip_id)


async def handle_control(body: Dict[str, Any], from_device: str) -> Dict[str, Any]:
    """Owner side of :func:`act`: device auth is checked by /peer/control."""
    from app.network import control

    session_id = str(body.get("session_id") or "")
    chip_id = str(body.get("chip_id") or "")
    action = body.get("action")
    if action not in ("start", "dismiss"):
        raise ValueError("Unsupported task chip action")
    await run_sync(control._require_owned, session_id)
    try:
        return await (start if action == "start" else dismiss)(session_id, chip_id)
    except ChipError as exc:
        raise ValueError(exc.message) from exc
