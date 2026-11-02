"""Background-task chips: an agent suggests adjacent work, the owner starts it.

``suggest`` records a chip and starts nothing. ``start`` and ``dismiss`` run on
the worker that owns the chat; a mirror forwards them over the control channel.
The chat a chip starts is created by
:func:`app.core.session_orchestration.start_suggested_task`.

A chip's chat runs with the root chat's access, whatever the agent that
suggested it could do. The owner's click is the approval, so :func:`public`
says what that access is before the click, worked out on every read because the
root's mode can change, and a start refuses to run broader than the client
showed (:func:`refuse_if_widened`).

A subagent running on another worker has no root chat here. Its suggestion
travels as the signed ``task-chip-suggest`` action to the worker that sent it
(:func:`relay`, :func:`handle_relayed`), which records the chip on its own chat
and mirrors it back like any other. Only the owner writes chips.
"""
import asyncio
import logging
from typing import Any, Dict, Optional, Set

from fluid.tools.builtins.session_orchestration import EFFORT_PROPERTY

from app.core.session_modes import PERMISSION_MODE_STRICTNESS
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
_RELAY_VERSION = 1
_RELAY_KEYS = frozenset({
    "version", "worker_session_id", "sender_session_id", "request_id", "title", "summary", "prompt",
    "model", "effort", "project_v2_id", "origin_label", "origin_mode", "origin_tools_limited",
})
_RELAY_TIMEOUT = 30.0
_LIST_ACCESS_TIMEOUT = 5.0

# What each permission mode means, in the words a chip shows the owner.
_ACCESS_WORDS = {
    "bypassPermissions": "full permissions",
    "auto": "safe actions run without asking",
    "acceptEdits": "file edits run without asking, everything else asks",
    "default": "asks you before using tools",
}
# The same modes as a description of the narrower agent that suggested the chip.
_SOURCE_WORDS = {
    "auto": "runs only safe actions without asking",
    "acceptEdits": "asks before everything except file edits",
    "default": "asks before using tools",
}
_ACCESS_UNKNOWN = "Could not check what access this task would run with."


class ChipError(Exception):
    """A refused chip action; ``status`` is its HTTP status."""

    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


def _stamp(value: Any) -> Any:
    return value.isoformat() if hasattr(value, "isoformat") else value


def _rank(mode: Optional[str]) -> int:
    return PERMISSION_MODE_STRICTNESS.index(mode) if mode in PERMISSION_MODE_STRICTNESS else -1


def _access_scope(session_id: str) -> Dict[str, Any]:
    """The root chat's state every open chip's access is worked out from."""
    from app.core import session_orchestration as orch
    from app.db import sessions

    row = sessions.get_session(session_id)
    return {"row": row, "overrides": orch._context(session_id, allow_empty=True).get("overrides") or {}}


def _project_note(chip: Dict[str, Any], root: Dict[str, Any]) -> Optional[str]:
    from app.db import projects_v2

    project = chip.get("project_v2_id")
    if not project or project == root.get("project_v2_id"):
        return None
    name = (projects_v2.get_project_v2(project) or {}).get("name") or "another project"
    if root.get("project_v2_id"):
        return f"Runs in {name}, not this chat's project."
    return f"Runs in {name}; this chat has no project."


def _source_note(chip: Dict[str, Any], mode: str) -> Optional[str]:
    """How the chat would exceed the agent that suggested it, in plain words."""
    limits = []
    origin_mode = chip.get("origin_mode")
    if origin_mode and _rank(mode) > _rank(origin_mode):
        limits.append(_SOURCE_WORDS.get(origin_mode, _SOURCE_WORDS["default"]))
    if chip.get("origin_tools_limited"):
        limits.append("could use only some tools")
    if not limits:
        return None
    return "More than the subagent that suggested it, which " + " and ".join(limits) + "."


def _access(chip: Dict[str, Any], scope: Optional[Dict[str, Any]] = None) -> Optional[Dict[str, Any]]:
    """What starting this chip would run with now. None once it can no longer be started."""
    from app.core import session_orchestration as orch

    if chip["state"] not in (store.PENDING, store.FAILED):
        return None
    try:
        scope = scope or _access_scope(chip["session_id"])
        mode = orch.chip_permission_mode(chip, scope["row"], scope["overrides"])
        source_note = _source_note(chip, mode)
        return {"mode": mode, "summary": f"Runs with this chat's access: {_ACCESS_WORDS[mode]}",
                "elevated": mode == "bypassPermissions" or source_note is not None,
                "wider_than_source": source_note is not None, "source_note": source_note,
                "project_note": _project_note(chip, scope["row"])}
    except Exception:  # noqa: BLE001 — a chip that cannot say so must not read as safe
        log.warning("Could not work out the access of task chip %s", chip["id"], exc_info=True)
        return {"mode": None, "summary": _ACCESS_UNKNOWN, "elevated": True, "wider_than_source": False,
                "source_note": None, "project_note": None}


def refuse_if_widened(shown: Optional[str], mode: str) -> None:
    """The click approved the access the chip showed; never run with more than that."""
    if _rank(mode) > _rank(shown):
        raise ValueError(f"This task would now run with broader access than the chip showed: "
                         f"{_ACCESS_WORDS[mode]}. Nothing was started. Check the access it shows now "
                         "and start it again if that is what you want.")


def public(chip: Dict[str, Any], scope: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """The API shape of a chip."""
    return {
        "id": chip["id"], "session_id": chip["session_id"], "origin_session_id": chip["origin_session_id"],
        "origin_label": chip.get("origin_label"),
        "title": chip["title"], "summary": chip["summary"], "prompt": chip["prompt"],
        "model": chip["model"], "provider_id": chip.get("provider_id"), "model_label": chip.get("model_label"),
        "effort": chip.get("effort"), "access": _access(chip, scope),
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


def _remote_chain(row: Optional[Dict[str, Any]]) -> Optional[tuple]:
    """``(child_id, parent)`` when ``row`` is, or works under, a Task child another worker started.

    ``parent`` is :func:`app.core.remote_task.remote_parent`'s answer. None for
    any other chain, including one a local refusal applies to on the way up.
    """
    from app.core.remote_task import remote_parent
    from app.db import sessions

    for _ in range(_MAX_ANCESTRY):
        if (row is None or not row.get("is_worker") or row.get("api_user_id") or row.get("automation_id")
                or row.get("mode") == "voice"):
            return None
        parent = remote_parent(row["id"])
        if parent:
            return row["id"], parent
        row = sessions.get_session(row["parent_session_id"]) if row.get("parent_session_id") else None
    return None


def row_offers(row: Optional[Dict[str, Any]]) -> bool:
    """Whether a chat with this row lists SuggestTask. Never raises.

    Decides what the model sees, not what is allowed: a row not written yet, or
    any doubt, offers the tool, and :func:`_root_chat` still decides each call.
    """
    try:
        if row is not None and _remote_chain(row) is None:
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


def _refuse_stopped_or_proactive(sender_id: str, *others: str) -> None:
    from app.core import session_orchestration as orch
    from app.core.task_registry import task_registry

    if any(task_registry.is_user_stopped(session_id) for session_id in (sender_id, *others)):
        raise PermissionError("The user stopped this chat, so it cannot suggest tasks.")
    context = orch._context(sender_id, allow_empty=True)
    if ((context.get("payload") or {}).get("_assistant_proactive") is not None
            or (context.get("overrides") or {}).get("_assistant_proactive") is not None):
        raise PermissionError(_NOT_OFFERED)


def _root_chat(sender_id: str) -> tuple:
    """``(origin_row, root_row)`` for a caller allowed to suggest, else raises."""
    from app.db import sessions

    origin = sessions.get_session(sender_id) if sender_id else None
    row = _owner_root(origin)
    _refuse_stopped_or_proactive(sender_id, row["id"])
    return origin, row


def _origin_label(origin: Dict[str, Any], root: Dict[str, Any]) -> Optional[str]:
    """Which agent raised the chip, so the root chat can say it. None for the root itself."""
    if origin["id"] == root["id"]:
        return None
    return (origin.get("title") or "").strip() or "Subagent"


def _origin_limits(origin: Dict[str, Any], root: Dict[str, Any]) -> Dict[str, Any]:
    """What the agent that suggested this could do itself, kept for the chip's disclosure.

    Recorded once: the agent may be long gone, or on another worker, when the
    owner reads the chip. The chat itself has no limits of its own to report.
    """
    from app.core import session_orchestration as orch
    from app.core.session_modes import turn_permission_mode
    from app.core.worker_checkout import _tool_surface
    from app.db import database

    if origin["id"] == root["id"]:
        return {"origin_mode": None, "origin_tools_limited": False}
    overrides = orch._context(origin["id"], allow_empty=True).get("overrides") or {}
    profile = database.get_profile(origin.get("profile_id")) if origin.get("profile_id") else None
    surface = _tool_surface(origin.get("profile_id"), overrides.get("tool_allowlist"))
    root_surface = _tool_surface(root.get("profile_id"), None)
    narrower = bool(surface) and (not root_surface or bool(set(root_surface) - set(surface)))
    return {"origin_mode": turn_permission_mode(overrides, origin, (profile or {}).get("config") if profile else None),
            "origin_tools_limited": narrower or bool(overrides.get("tool_denylist"))}


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
    refusal = orch.chip_lane_refusal(choice.model, choice.provider_id)
    if refusal:
        raise ValueError(f"{model_override.label(choice.model)} cannot run on the chat's worker now: {refusal} "
                         "Nothing was recorded; choose a model that can run there, or mention the issue "
                         "in your final report instead.")
    overrides = orch._context(root["id"], allow_empty=True).get("overrides") or {}
    return {"model": choice.model, "provider_id": choice.provider_id, "model_label": model_override.label(choice.model),
            "effort": resolve_child_effort(choice.model, choice.provider_id, explicit=effort,
                                           parent=overrides.get("effort", root.get("effort")), caller=root)}


def _brief(data: Dict[str, Any]) -> Dict[str, str]:
    return {"title": _text(data, "title", _TITLE_LIMIT), "summary": _text(data, "summary", _SUMMARY_LIMIT),
            "prompt": _text(data, "prompt", _PROMPT_LIMIT), "request_id": _text(data, "request_id", _ID_LIMIT)}


def _base(data: Dict[str, Any]) -> str:
    base = data.get("base") or "default"
    if base not in BASES:
        raise ValueError("base must be \"default\" or \"current\"")
    return base


def _record(sender_id: str, data: Dict[str, Any]) -> Dict[str, Any]:
    from app.core.worker_checkout import WorkerCheckoutError, origin_head_commit
    from app.db import session_events

    brief = _brief(data)
    base = _base(data)
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
        base=base, base_commit=base_commit, project_v2_id=origin.get("project_v2_id") or root.get("project_v2_id"),
        origin_label=_origin_label(origin, root), **brief, **chosen, **_origin_limits(origin, root))
    return result


async def _recorded(result: Dict[str, Any]) -> Dict[str, Any]:
    """Tell clients about a new chip, and what the agent that suggested it reads."""
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


async def suggest(sender_id: str, data: Dict[str, Any]) -> Dict[str, Any]:
    """SuggestTask's host. Records a chip; the caller's turn carries straight on."""
    from app.db import sessions

    origin = await run_sync(sessions.get_session, sender_id) if sender_id else None
    remote = await run_sync(_remote_chain, origin)
    if remote is not None:
        return await relay(sender_id, origin, data, *remote)
    return await _recorded(await run_sync(_record, sender_id, data))


def _relay_body(sender_id: str, origin: Dict[str, Any], data: Dict[str, Any], worker_id: str) -> Dict[str, Any]:
    from app.core import session_orchestration as orch

    if _base(data) != "default":
        raise ValueError("Use base=\"default\": this subagent runs on another worker, so the commit it is on "
                         "is not available to the chat that would start the task. Nothing was recorded.")
    effort = data.get("effort")
    if effort is not None and effort not in EFFORT_PROPERTY["enum"]:
        raise ValueError(f"effort must be one of: {', '.join(EFFORT_PROPERTY['enum'])}. Nothing was recorded.")
    if not isinstance(data.get("model"), str) or not data["model"].strip():
        raise ValueError("model must name the model the new chat runs on. Nothing was recorded.")
    _refuse_stopped_or_proactive(sender_id, worker_id)
    limits = _origin_limits(origin, {"id": None})
    return {"version": _RELAY_VERSION, "worker_session_id": worker_id, "sender_session_id": sender_id,
            **_brief(data), "model": data["model"], "effort": effort,
            "project_v2_id": origin.get("project_v2_id"),
            "origin_label": _origin_label(origin, {"id": None}), **limits}


async def relay(sender_id: str, origin: Dict[str, Any], data: Dict[str, Any], worker_id: str,
                parent: Dict[str, Any]) -> Dict[str, Any]:
    """A subagent another worker started: ask that worker to record the chip on its chat.

    Nothing is written here; the chip arrives through the owner's mirror. The
    other worker keeps one chip per request id, so a resend after a lost answer
    is safe.
    """
    from app.core.remote_task import MAY_HAVE_APPLIED
    from app.network import control

    body = await run_sync(_relay_body, sender_id, origin, data, worker_id)
    where = parent["worker_name"]
    try:
        try:
            answer = await control.call(parent["device_id"], "task-chip-suggest", body, timeout=_RELAY_TIMEOUT)
        except control.RunnerUnreachable as exc:
            if MAY_HAVE_APPLIED not in str(exc):
                raise
            answer = await control.call(parent["device_id"], "task-chip-suggest", body, timeout=_RELAY_TIMEOUT)
    except control.RunnerUnreachable as exc:
        raise ValueError(f"Could not reach {where}, the worker running the chat this suggestion belongs to. "
                         "Nothing was recorded; mention the issue in your final report instead.") from exc
    except control.ControlRefused as exc:
        if "Unknown control action" in str(exc):
            raise ValueError(f"{where}, the worker running the chat this suggestion belongs to, needs an update "
                             "before it can take suggestions from other workers. Nothing was recorded; mention "
                             "the issue in your final report instead.") from exc
        raise ValueError(str(exc)) from exc
    if not isinstance(answer, dict) or not answer.get("chip_id"):
        raise ValueError(f"{where} gave an unexpected answer. Nothing was recorded for certain; mention the "
                         "issue in your final report instead.")
    return answer


def _record_relayed(body: Dict[str, Any], from_device: str) -> Dict[str, Any]:
    """Owner side of :func:`relay`: only for a subagent this worker sent to the asking worker."""
    from app.core.remote_task import _require_worker_member
    from app.db import projects_v2, remote_tasks, session_events

    _require_worker_member(from_device)
    if not isinstance(body, dict) or body.get("version") != _RELAY_VERSION:
        raise ValueError("Unsupported suggestion version; update this worker.")
    unknown = set(body) - _RELAY_KEYS
    if unknown:
        raise ValueError(f"Unknown suggestion fields: {', '.join(sorted(unknown))}")
    worker_id = _text(body, "worker_session_id", 64)
    dispatch = remote_tasks.get_dispatch(worker_id)
    if dispatch is None or dispatch["target_device_id"] != from_device:
        raise PermissionError("No subagent with this id was sent to the asking worker.")
    if dispatch.get("stop_requested_at"):
        raise PermissionError("The user stopped this subagent, so it cannot suggest tasks.")
    sender_id = _text(body, "sender_session_id", 200)
    origin, root = _root_chat(dispatch["owner_session_id"])
    brief = _brief(body)
    chosen = _chosen_model(body, origin, root)
    project = body.get("project_v2_id")
    if project is not None and not isinstance(project, str):
        raise ValueError("project_v2_id must be a string")
    if project and project != root.get("project_v2_id") and not projects_v2.get_project_v2(project):
        raise ValueError("The project this subagent works in is not on the worker running its chat, so the task "
                         "could not start there. Nothing was recorded; mention the issue in your final report "
                         "instead.")
    mode = body.get("origin_mode")
    label = body.get("origin_label")
    label = label.strip()[:_TITLE_LIMIT] if isinstance(label, str) else ""
    return store.create(
        root["id"], origin_session_id=sender_id, origin_seq=session_events.get_max_seq(root["id"]),
        base="default", base_commit=None, project_v2_id=project or root.get("project_v2_id"),
        origin_label=label or "Subagent",
        origin_mode=mode if mode in PERMISSION_MODE_STRICTNESS else "default",
        origin_tools_limited=bool(body.get("origin_tools_limited")), **brief, **chosen)


async def handle_relayed(body: Dict[str, Any], from_device: str) -> Dict[str, Any]:
    """``task-chip-suggest`` control action; device auth is checked by /peer/control."""
    return await _recorded(await run_sync(_record_relayed, body, from_device))


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
    rows = store.list_for_session(session_id)
    scope = _access_scope(session_id) if any(row["state"] in (store.PENDING, store.FAILED) for row in rows) else None
    return {"chips": [public(chip, scope) for chip in rows],
            "source": source and {"chip_id": source["chip_id"], "session_id": source["session_id"],
                                  "title": source.get("title")}}


def _owner_access(session_id: str) -> Dict[str, Any]:
    """Each open chip's access as this worker, which owns the chat, works it out."""
    return {chip["id"]: chip["access"] for chip in list_chips(session_id)["chips"] if chip["access"]}


async def list_view(session: Dict[str, Any]) -> Dict[str, Any]:
    """:func:`list_chips` for a client: a mirror asks the owner what an open chip would run with.

    Only the owner knows its turn context and its agent settings. An owner that
    cannot answer leaves the mirror's own reading of its rows on show; a start
    needs that owner anyway, and it refuses to run broader than what was shown.
    """
    from app.network import control

    view = await run_sync(list_chips, session["id"])
    if not any(chip["access"] for chip in view["chips"]):
        return view
    owner = await run_sync(control.owner_of, session)
    if owner is None:
        return view
    try:
        answer = await control.call(owner, "task-chip", {"session_id": session["id"], "action": "access"},
                                    timeout=_LIST_ACCESS_TIMEOUT)
    except (control.RunnerUnreachable, control.ControlRefused) as exc:
        log.debug("Could not ask the owner of %s for chip access: %s", session["id"], exc)
        return view
    access = (answer or {}).get("access") or {}
    for chip in view["chips"]:
        chip["access"] = access.get(chip["id"], chip["access"]) if chip["access"] else None
    return view


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


async def _run_start(session_id: str, chip_id: str, shown_access: Optional[str]) -> Dict[str, Any]:
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
                new_id = await asyncio.to_thread(start_suggested_task, chip, shown_access)
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


async def start(session_id: str, chip_id: str, shown_access: Optional[str]) -> Dict[str, Any]:
    """Idempotent. Runs to completion even if the requesting client goes away.

    ``shown_access`` is the ``access.mode`` the client showed when the owner
    clicked; a caller that showed none has approved no access at all.
    """
    task = asyncio.ensure_future(_run_start(session_id, chip_id, shown_access))
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


async def act(session: Dict[str, Any], chip_id: str, action: str,
              shown_access: Optional[str] = None) -> Dict[str, Any]:
    """Run ``action`` where the chat runs, as other session actions are routed."""
    from app.network import control

    owner = await run_sync(control.owner_of, session)
    if owner is not None:
        try:
            return await control.call(owner, "task-chip", {
                "session_id": session["id"], "chip_id": chip_id, "action": action,
                "shown_access": shown_access}, timeout=120.0)
        except control.RunnerUnreachable as exc:
            raise ChipError(503, "The worker that owns this chat is offline. Nothing was changed; "
                                 "try again when it is back.") from exc
        except control.ControlRefused as exc:
            raise ChipError(409, "The worker that owns this chat refused this: " + str(exc)) from exc
    if action == "start":
        return await start(session["id"], chip_id, shown_access)
    return await dismiss(session["id"], chip_id)


async def handle_control(body: Dict[str, Any], from_device: str) -> Dict[str, Any]:
    """Owner side of :func:`act`: device auth is checked by /peer/control."""
    from app.network import control

    session_id = str(body.get("session_id") or "")
    chip_id = str(body.get("chip_id") or "")
    action = body.get("action")
    if action not in ("start", "dismiss", "access"):
        raise ValueError("Unsupported task chip action")
    await run_sync(control._require_owned, session_id)
    if action == "access":
        return {"access": await run_sync(_owner_access, session_id)}
    try:
        if action == "start":
            return await start(session_id, chip_id, body.get("shown_access"))
        return await dismiss(session_id, chip_id)
    except ChipError as exc:
        raise ValueError(exc.message) from exc
