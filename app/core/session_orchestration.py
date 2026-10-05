"""Owner-scoped top-level session coordination and sidebar management.

Coordination queues peer-authored turns, never user commands. Sidebar tools only
change membership. Identity, permissions and ancestry come from the host.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

from fluid.context.peer import format_peer_message
from fluid.tools.hardening import defang_harness_markers
from fluid.tools.registry import ToolOutcome

log = logging.getLogger(__name__)
MAX_ACTIONS = 32
MAX_STARTS = 4
MAX_HOPS = 8
# A StartSession on another worker is recorded here as a settled receipt turn
# under the chain's origin, so dedupe and the chain budget stay on the caller.
# The prefix keeps the row off every real session (including the new chat's
# local mirror, which has the bare id).
REMOTE_RECEIPT_PREFIX = "orchestration-remote:"
REMOTE_ACTION = "orchestration-start"
# A chat the owner's assistant started on another worker sends its report back
# to the assistant's worker with this action when it comes to rest.
REPORT_ACTION = "orchestration-report"
# The destination answers after one local transaction; a slow tunnel still fits.
REMOTE_TIMEOUT = 20
_COMMAND_RE = re.compile(r"orchestration:start:[0-9a-f]{64}")
REPORT_STATUSES = ("completed", "failed", "stopped")
MAX_NEEDS_OWNER = 10
# Turn sources that begin work in a chat. Continuations (unpark, auto-continue,
# failover) belong to the most recent of these.
_ORIGIN_SOURCES = ("user", "queue", "agent_message")


def _text(data, key, limit):
    value = data.get(key)
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        raise ValueError(f"{key} must be a non-empty string of at most {limit} characters")
    return value.strip()


def _local_top_level(row, *, allow_settled=False):
    from app.core.config import settings
    from app.network.runners import self_device_id
    from app.core.assistant_service import is_primary_session
    # Assistant homes are list-hidden, not command/delegated sessions.
    visible = row and (not row.get("hidden") or is_primary_session(row["id"]))
    return bool(visible and row.get("instance_id") == settings.effective_instance_id
                and row.get("owner_device_id") in (None, self_device_id())
                and (allow_settled or row.get("is_active"))
                and not row.get("is_worker") and not row.get("parent_session_id")
                and row.get("status") != "archived" and row.get("mode") != "voice")


def _principal(session_id):
    from app.db import mobile_tool_identity
    return mobile_tool_identity.read(session_id)


def _owner(row):
    principal = _principal(row["id"])
    # Legacy install-local chats remain usable without acquiring mobile grants.
    return (row.get("api_user_id"), (principal or {}).get("owner", "install-admin"))


def _caller(session_id, *, allow_settled=False):
    from app.db import sessions
    from app.core import mobile_tool_identity
    from app.core.task_registry import task_registry
    row = sessions.get_session(session_id) if session_id else None
    if not _local_top_level(row, allow_settled=allow_settled):
        raise ValueError("Top-level orchestration requires an active, local, visible top-level chat.")
    if task_registry.is_user_stopped(session_id):
        raise ValueError("The user stopped this session; only the user may continue it.")
    principal = _principal(session_id)
    mobile_tool_identity.validate(principal)
    return row


def _target(caller, session_id, *, allow_stopped=False):
    from app.db import sessions
    from app.core.task_registry import task_registry
    row = sessions.get_session(session_id)
    if (row and session_id != caller["id"] and row.get("status") == "archived"
            and _owner(row) == _owner(caller)):
        # Named, unlike the generic refusal below: the caller may have found it
        # through ListSessions(state='archived') and needs the reason (#1822).
        from app.core import archive_guard
        label = f"Session {session_id}" + (f" ({row['title']})" if row.get("title") else "")
        raise ValueError(archive_guard.refusal(label, "receive messages"))
    if (session_id == caller["id"] or not _local_top_level(row)
            or _owner(row) != _owner(caller)):
        raise ValueError("Session is not an eligible peer; use ListSessions.")
    if not allow_stopped and task_registry.is_user_stopped(session_id):
        raise ValueError("The receiver was stopped by the user; it will not be restarted.")
    from app.core import mobile_tool_identity
    mobile_tool_identity.validate(_principal(session_id))
    return row


def list_sessions(session_id, data):
    from app.db import sessions
    from app.core.task_registry import task_registry
    caller = _caller(session_id, allow_settled=True)
    scope = data.get("scope", "project")
    state = data.get("state", "active")
    if state not in ("active", "settled", "archived", "all"):
        raise ValueError("state must be active, settled, archived or all")
    if scope not in ("project", "all"):
        raise ValueError("scope must be project or all")
    offset = data.get("offset", 0)
    if type(offset) is not int or offset < 0:
        raise ValueError("offset must be a non-negative integer")
    rows = sessions.get_sessions(api_user_id=caller.get("api_user_id") or "",
                                 active_only=state == "active", limit=100, offset=offset)
    result = []
    caller_owner = _owner(caller)
    from app.core.session_management import visible_session
    for row in rows:
        if state == "active":
            eligible = _local_top_level(row) and not task_registry.is_user_stopped(row["id"])
        else:
            eligible = visible_session(row)
        if row["id"] == session_id or not eligible or _owner(row) != caller_owner:
            continue
        if state == "settled" and (row.get("is_active") or row.get("status") == "archived"):
            continue
        if state == "archived" and row.get("status") != "archived":
            continue
        if scope == "project" and (
            row.get("project_v2_id") or row.get("project_id")
        ) != (caller.get("project_v2_id") or caller.get("project_id")):
            continue
        entry = {key: row.get(key) for key in (
            "id", "title", "status", "profile_id", "project_id", "project_v2_id",
            "branch", "model", "updated_at", "is_active", "owner_device_id")}
        stamp = entry.get("updated_at")
        if hasattr(stamp, "isoformat"):
            entry["updated_at"] = stamp.isoformat()
        result.append(entry)
    return {"sessions": result, "next_offset": offset + len(rows) if len(rows) == 100 else None,
            "note": "Active means open in the sidebar, not necessarily generating. No transcripts or credentials included."}


def _context(session_id, *, allow_empty=False):
    from app.core import turn_scheduler
    from app.db import sessions, turns
    # A new user/peer turn can be running while an older turn remains parked.
    # Its ancestry and permissions take precedence over the parked context.
    context = turns.get_live_turn(session_id) or turn_scheduler._latest_context_turn(session_id)
    if not context and allow_empty:
        row = sessions.get_session(session_id)
        return {"id": None, "payload": {}, "worktree_id": row.get("worktree_id"),
                "overrides": {key: row[key] for key in (
                    "model", "permission_mode", "effort", "context_window", "fast"
                ) if row.get(key) is not None}}
    if not context:
        raise ValueError("The session has no saved turn context.")
    return context


def _delivery_status(turn):
    """Bounded durable receipt, not an acknowledgement or automatic peer reply."""
    from app.core.task_registry import task_registry
    if turn.get("state") == "aborted" or task_registry.is_user_stopped(turn["session_id"]):
        return "stopped"
    if turn.get("state") == "queued":
        return "queued"
    if turn.get("state") in ("completed", "parked"):
        return "consumed"
    return "begun"


def _remote_worker(value, listing=None):
    """The StartSession ``worker`` pick: None for this worker, else its directory row.

    Only an online, active worker of this account network qualifies. Anything
    else is refused by name; a bad pick never falls back to starting here.
    """
    if value is None:
        return None
    from app.network import runners
    row = runners.resolve_worker(value, instead="omit worker to start here",
                                 refused="nothing was started", listing=listing)
    if row["is_self"]:
        return None
    return {"device_id": row["device_id"], "device_name": row["device_name"]}


def _start_turn(session_id):
    """The turn StartSession queued to begin ``session_id``, or None."""
    from app.db import turns
    from app.db.base import get_db
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM turns WHERE session_id=%s AND command_id LIKE 'orchestration:start:%%' "
                       "ORDER BY created_at ASC LIMIT 1", (session_id,))
        return turns._row(cursor.fetchone())


def _home_of_start(session_id, start):
    from app.core import assistant_service
    payload = (start or {}).get("payload") or {}
    home = assistant_service.get_primary_session_id()
    if (not home or home == session_id or payload.get("orchestration_sender") != home
            or payload.get("orchestration_remote_origin")):
        return None
    return home if assistant_service.is_primary_session(home) else None


def started_by_home(session_id):
    """The owner's assistant home that started this local chat, else None.

    Durable: read from the chat's StartSession turn, whose sender and command id
    the host wrote. Never raises; any doubt answers None (ordinary behaviour).
    """
    if not session_id:
        return None
    try:
        from app.core import assistant_service
        if not assistant_service.get_primary_session_id():
            return None
        return _home_of_start(session_id, _start_turn(session_id))
    except Exception:  # noqa: BLE001 — absent marker means an ordinary chat
        log.debug("started_by_home failed for %s", session_id, exc_info=True)
        return None


def _resolve_project(value):
    """A project the owner registered, by id, name or local checkout path."""
    if not isinstance(value, str) or not value.strip() or len(value) > 500:
        raise ValueError("project must be a project name, id or path of at most 500 characters")
    wanted = value.strip()
    from app.db import checkouts, projects_v2
    rows = projects_v2.list_projects_v2()
    matches = [row for row in rows if row["id"] == wanted]
    if not matches:
        matches = [row for row in rows if (row.get("name") or "").casefold() == wanted.casefold()]
    if not matches:
        checkout = checkouts.get_checkout_by_path(wanted)
        if checkout:
            matches = [row for row in rows if row["id"] == checkout.get("project_id")]
    if len(matches) > 1:
        raise ValueError(f"More than one project is named {wanted!r}; pass its id instead: "
                         + ", ".join(row["id"] for row in matches) + ".")
    if not matches:
        names = sorted({row.get("name") or row["id"] for row in rows}, key=str.casefold)
        listed = ", ".join(names[:40]) + (f" and {len(names) - 40} more" if len(names) > 40 else "")
        raise ValueError(f"Unknown project {wanted!r}; nothing was started. "
                         f"Registered projects: {listed or 'none'}.")
    return matches[0]


def _checked_out_here(project_v2_id):
    from app.core.config import settings
    from app.db import checkouts
    return checkouts.has_active_base_checkout(project_v2_id, settings.effective_instance_id)


async def _project_placement(sender_id, data):
    """Where StartSession's ``project`` runs: the project and None (this worker)
    or the online worker that has it checked out.

    Only the owner's assistant home may name a project. The name is resolved
    against registered projects only and grants nothing: the chat uses the
    checkout and grants the chosen worker already has.
    """
    from app.core.assistant_service import is_primary_session
    if not await asyncio.to_thread(is_primary_session, sender_id):
        raise ValueError("project is available only in the owner's assistant conversation; omit it.")
    project = await asyncio.to_thread(_resolve_project, data.get("project"))
    name = project.get("name") or project["id"]
    if data.get("worker") is not None:
        remote = await asyncio.to_thread(_remote_worker, data.get("worker"))
        if remote is None and not await asyncio.to_thread(_checked_out_here, project["id"]):
            raise ValueError(f"{name} is not checked out on this worker; nothing was started.")
        # A chosen remote worker re-checks its own checkout and refuses by name.
        return {"project": project, "remote": remote}
    if await asyncio.to_thread(_checked_out_here, project["id"]):
        return {"project": project, "remote": None}
    from app.network import engine, git as network_git, runners
    if engine.context() is None:
        raise ValueError(f"{name} is not checked out on this worker; nothing was started. "
                         "Ask the owner to open or clone it here first.")
    checked = ["this worker (not checked out)"]
    listing = await asyncio.to_thread(runners.directory)
    for row in listing["runners"]:
        if row["is_self"]:
            continue
        if not row["online"]:
            checked.append(f"{row['device_name']} (offline)")
            continue
        try:
            remote = await asyncio.to_thread(_remote_worker, row["device_id"], listing)
            eligible = remote is not None and await network_git.eligible(row["device_id"], project["id"])
        except Exception:  # noqa: BLE001 — one unreachable worker never blocks the others
            log.info("Could not check %s for project %s", row["device_id"], project["id"], exc_info=True)
            checked.append(f"{row['device_name']} (could not be checked)")
            continue
        if eligible:
            return {"project": project, "remote": remote}
        checked.append(f"{row['device_name']} (not checked out)")
    raise ValueError(f"No online worker has {name} checked out, so nothing was started. "
                     f"Checked: {', '.join(checked)}. Ask the owner to open or clone it on a worker first.")


def start_session_spec():
    """StartSession's spec; its ``model`` names the owner's subagent default
    as an example. Any eligible model is accepted and checked (#1830)."""
    from fluid.tools.builtins.session_orchestration import start_session_spec as build
    try:
        from app.core import subagent_default
        examples = tuple(filter(None, (subagent_default.effective().model,)))
    except Exception:  # noqa: BLE001 — the examples are advisory
        log.debug("Could not read the subagent default for StartSession", exc_info=True)
        examples = ()
    return build(examples)


def _inherited_model(sender, overrides):
    """The app-format model a new chat inherits from ``sender`` (None = unbound)."""
    from app.core import model_override
    from app.core.query_options import decode_provider_model, is_native_claude_model
    value = overrides.get("model") or sender.get("model")
    provider_id = sender.get("provider_id")
    if not value or decode_provider_model(value) is not None:
        return model_override.app_id(None, value)
    if provider_id and is_native_claude_model(value):
        # A native Claude pick on a provider chat runs natively unless that
        # provider itself serves the id (query_options' rebind rule).
        from app.core.provider_agent import curated_model_ids
        from app.db.model_providers import get_model_provider
        if value not in curated_model_ids(get_model_provider(provider_id)):
            provider_id = None
    return model_override.app_id(provider_id, value)


def _start_model_plan(sender_id, data, remote):
    """What StartSession's ``model`` asks for, before the owner is consulted.

    None when it names the model the chat inherits anyway (no check needed).
    Otherwise ``{model, provider_id, model_id, baseline, profile_id, seen}``;
    ``seen`` is True when this request_id already has a receipt, so a retry
    reads it back without asking the owner again. Raises for a model that is
    not eligible (on this worker when the chat runs here).
    """
    from app.core import model_override
    from app.db.base import get_db
    sender = _caller(sender_id)
    context = _context(sender_id)
    overrides = context.get("overrides") or {}
    baseline = _inherited_model(sender, overrides)
    try:
        choice = model_override.resolve(data.get("model"), sender.get("provider_id"),
                                        runs_here=remote is None)
    except ValueError as exc:
        raise ValueError(f"{exc} Nothing was started.") from None
    if choice.model == baseline:
        return None
    command_id = "orchestration:start:" + hashlib.sha256(
        (sender_id + ":" + str(data.get("request_id") or "")).encode()).hexdigest()
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT 1 FROM turns WHERE command_id=%s LIMIT 1", (command_id,))
        seen = cursor.fetchone() is not None
    return {"model": choice.model, "provider_id": choice.provider_id, "model_id": choice.model_id,
            "baseline": baseline, "profile_id": sender.get("profile_id"), "seen": seen}


async def _confirm_start_model(sender_id, data, placement):
    """Check StartSession's ``model`` and, when it switches models, that the
    user asked for it (named it, or the owner approved). Returns the plan for
    :func:`_queue`, False when the model is the inherited one, or None when
    no model was passed. Raises PermissionError with the refusal."""
    if data.get("model") is None:
        return None
    from app.core import model_override
    if placement is not None:
        remote = placement["remote"]
    else:
        remote = await asyncio.to_thread(_remote_worker, data.get("worker"))
    plan = await asyncio.to_thread(_start_model_plan, sender_id, data, remote)
    if plan is None:
        return False
    if plan["seen"]:
        plan["source"] = None
        return plan
    title = str(data.get("title") or "").strip()[:80] or "new chat"
    action = (f"start the chat “{title}” on {model_override.label(plan['model'])} instead of "
              f"{model_override.label(plan['baseline']) if plan['baseline'] else 'your model'}")
    plan["source"] = await model_override.confirm(
        sender_id, plan["profile_id"], model=plan["model"], baseline=plan["baseline"], action=action)
    return plan


def _start_overrides(overrides, plan, mapped):
    """The new chat's first-turn overrides for a switched model: the caller's,
    with model and run settings replaced so the turn cannot rebind back."""
    result = {key: value for key, value in overrides.items()
              if key not in ("model", "provider", "effort", "context_window", "fast")}
    result["model"] = plan["model"]
    result.update({key: value for key, value in mapped.items() if value is not None})
    return result


def _override_record(plan):
    """What the chat and the receipt record about a switched model."""
    return {"model": plan["model"], "provider_id": plan["provider_id"],
            "source": plan.get("source"), "instead_of": plan.get("baseline")}


def _remote_session_id(command_id):
    # Deterministic, so a retried request lands on the same chat on the destination.
    return str(uuid5(NAMESPACE_URL, "ai-shuffle:" + command_id))


def _started_permission_mode(sender, overrides):
    """The permission binding a chat started by ``sender`` is created with.

    The sender's CURRENT binding, never the permission mode copied onto its
    continuation's overrides: after the owner tightens the sender, that copy
    still says the old, looser mode, and a new session's binding is exactly
    what every one of its turns resolves from. A ceiling the sender's turn
    carries (a connector's) caps it. ``None`` (the sender has no binding)
    leaves the new chat unbound too, so it follows the same agent's mode.
    """
    from app.core.session_modes import permission_ceiling, turn_permission_mode

    ceiling = permission_ceiling({"_permission_ceiling": (overrides or {}).get("_permission_ceiling")},
                                 {**sender, "is_worker": False})
    if not ceiling:
        return sender.get("permission_mode")
    from app.db import database
    profile = database.get_profile(sender.get("profile_id")) if sender.get("profile_id") else None
    return turn_permission_mode({"_permission_ceiling": ceiling}, sender,
                                (profile or {}).get("config") if profile else None)


def _remote_body(sender, data, overrides, payload, prompt, command_id, fingerprint, *,
                 project_v2_id=None, assistant=False, model_plan=None, permission_mode=None):
    """What the destination needs to make the chat. No path, worktree, branch,
    credential or tool identity: those belong to this install.

    ``assistant``: the owner's assistant asked, so the chat reports back here
    and runs with Away handling there (approvals cannot travel back).
    ``model_plan``: a model the user asked for (#1830); the destination
    re-checks that it can run there and maps the run settings again.
    ``permission_mode``: :func:`_started_permission_mode`, the new chat's
    binding (the destination caps it again with its own agent and policy)."""
    body = {
        "session_id": _remote_session_id(command_id), "command_id": command_id,
        "fingerprint": fingerprint, "title": _text(data, "title", 160), "prompt": prompt,
        "peer_message_content": payload["peer_message_content"],
        "sender_session_id": payload["orchestration_sender"],
        "sender_label": payload["peer_sender_label"],
        "depth": payload["orchestration_depth"],
        "profile_id": sender["profile_id"],
        "project_v2_id": project_v2_id or sender.get("project_v2_id"),
        "provider_id": sender.get("provider_id"),
        "model": overrides.get("model") or sender.get("model"),
        "effort": overrides.get("effort", sender.get("effort")),
        "permission_mode": permission_mode,
        "context_window": sender.get("context_window"), "fast": sender.get("fast"),
    }
    if model_plan:
        from app.core import model_override
        mapped = model_override.settings_for_model(
            model_plan["model"], model_plan["provider_id"], effort=body["effort"],
            context_window=body["context_window"], fast=body["fast"])
        body.update(model=model_plan["model"], provider_id=model_plan["provider_id"], **mapped)
        body["model_choice"] = _override_record(model_plan)
    if assistant:
        body["report_back"] = True
    return body


def _remote_text(body, key, limit, *, required=True):
    value = body.get(key)
    if value is None and not required:
        return None
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        raise ValueError(f"{key} must be a non-empty string of at most {limit} characters")
    return value


def accept_remote_start(body, from_device):
    """Destination side of a StartSession aimed at this worker (synchronous).

    The caller already applied its permission gates, dedupe and chain budget.
    This worker re-checks everything it owns: the asking device, the agent and
    the project checkout, and makes the chat idempotent on its id.
    """
    from app.core.config import settings
    from app.db import checkouts, database, model_providers, sessions, turns
    from app.db.base import get_db, transaction
    from app.network import control, engine, membership

    ctx = engine.context()
    member = ctx.member(from_device) if ctx else None
    if (ctx is None or from_device == ctx.device_id or member is None
            or member.kind != membership.KIND_WORKER or member.status != membership.STATUS_ACTIVE):
        raise PermissionError("Only active workers of this account may start chats here.")
    session_id = _remote_text(body, "session_id", 64)
    try:
        UUID(session_id)
    except ValueError:
        raise ValueError("session_id must be a UUID") from None
    command_id = _remote_text(body, "command_id", 120)
    if not _COMMAND_RE.fullmatch(command_id) or _remote_session_id(command_id) != session_id:
        raise ValueError("Invalid StartSession request identity")
    fingerprint = _remote_text(body, "fingerprint", 64)
    title = _remote_text(body, "title", 160).strip()
    prompt = _remote_text(body, "prompt", 20000)
    content = _remote_text(body, "peer_message_content", 8000)
    sender_session = _remote_text(body, "sender_session_id", 200)
    sender_label = _remote_text(body, "sender_label", 400)
    depth = body.get("depth")
    if type(depth) is not int or not 1 <= depth <= MAX_HOPS:
        raise ValueError("Invalid peer-chain depth")
    profile_id = _remote_text(body, "profile_id", 200)
    project_v2_id = _remote_text(body, "project_v2_id", 200, required=False)
    model = _remote_text(body, "model", 300, required=False)
    effort = _remote_text(body, "effort", 40, required=False)
    permission_mode = control._valid_permission_mode(body.get("permission_mode"))
    context_window = body.get("context_window")
    if context_window is not None and (type(context_window) is not int or context_window <= 0):
        raise ValueError("Invalid context_window")
    fast = body.get("fast")
    if fast is not None and not isinstance(fast, bool):
        raise ValueError("Invalid fast")
    provider_id = body.get("provider_id") if isinstance(body.get("provider_id"), str) else None
    worker_name = member.name or from_device

    report_back = body.get("report_back") is True
    choice = body.get("model_choice") if isinstance(body.get("model_choice"), dict) else None

    def receipt(turn, created):
        row = sessions.get_session(session_id) or {}
        return {"session_id": session_id, "turn_id": turn["id"], "created": created,
                "status": "queued" if created else "already_queued",
                "delivery_status": _delivery_status(turn),
                "report_back": bool((turn.get("payload") or {}).get("orchestration_report_device")),
                "model": row.get("model"), "provider_id": row.get("provider_id")}

    if sessions.get_session(session_id) is not None:
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM turns WHERE session_id=%s AND command_id=%s", (session_id, command_id))
            turn = turns._row(cursor.fetchone())
        if turn is None:
            raise ValueError("A different chat already uses this id; nothing was started.")
        if (turn.get("payload") or {}).get("orchestration_fingerprint") != fingerprint:
            raise ValueError("request_id was already used with different arguments.")
        return receipt(turn, False)
    if database.get_profile(profile_id) is None:
        raise ValueError(f"The agent {profile_id!r} does not exist on this worker; nothing was started.")
    if project_v2_id and not checkouts.has_active_base_checkout(project_v2_id, settings.effective_instance_id):
        raise ValueError("This project is not checked out on this worker; open or clone it here first. "
                         "Nothing was started.")
    if choice is not None:
        # A model the user asked for on the calling worker (#1830): it must run
        # HERE, never quietly on something else, and the inherited settings are
        # mapped against this worker's provider rows.
        from app.core import model_override
        try:
            picked = model_override.resolve(choice.get("model"), runs_here=True)
        except ValueError as exc:
            raise ValueError(f"{exc} Nothing was started.") from None
        provider_id, model = picked.provider_id, picked.model
        mapped = model_override.settings_for_model(model, provider_id, effort=effort,
                                                   context_window=context_window, fast=fast)
        effort, context_window, fast = mapped["effort"], mapped["context_window"], mapped["fast"]
    elif provider_id and model_providers.get_model_provider(provider_id) is None:
        provider_id = None
    payload = {
        "orchestration_depth": depth, "orchestration_sender": sender_session,
        "orchestration_fingerprint": fingerprint, "orchestration_remote_origin": from_device,
        "peer_message_content": content,
        "peer_sender_label": f"{sender_label} (on {worker_name})",
        "forwarded_by_device": from_device,
    }
    if choice is not None:
        payload["model_override"] = {
            "model": model, "provider_id": provider_id,
            "source": choice.get("source") if choice.get("source") in ("named_by_user", "approved_by_owner") else None,
            "instead_of": choice.get("instead_of") if isinstance(choice.get("instead_of"), str) else None}
    if report_back:
        # The owner's assistant asked: report back to its worker when done.
        payload["orchestration_report_device"] = from_device
    overrides = {key: value for key, value in (("model", model), ("effort", effort),
                                                ("permission_mode", permission_mode)) if value}
    with transaction() as conn:
        sessions.create_session(
            session_id=session_id, title=title, profile_id=profile_id, project_v2_id=project_v2_id,
            is_active=True, provider_id=provider_id,
            derive_provider_from_profile=provider_id is None and choice is None,
            model=model, effort=effort, permission_mode=permission_mode,
            context_window=context_window, fast=fast, _conn=conn)
        if report_back:
            # Approvals cannot travel back to the assistant's worker, so Away
            # mode applies as is: questions get the recommended option and
            # approvals are deferred (never granted) and listed in the report.
            conn.cursor().execute("UPDATE sessions SET away_mode=TRUE WHERE id=%s", (session_id,))
        turn = turns.enqueue_turn(session_id, "agent_message", prompt=prompt, profile_id=profile_id,
                                  overrides=overrides, payload=payload, command_id=command_id, _conn=conn)
    log.info("StartSession from worker %s created chat %s", from_device, session_id)
    return receipt(turn, True)


async def handle_remote_start(body, from_device):
    """``orchestration-start`` control action: make the chat, then wake it."""
    from app.core import turn_scheduler
    from app.db.async_database import run_sync
    result = await run_sync(accept_remote_start, body, from_device)
    if result["created"]:
        turn_scheduler.kick(result["session_id"])
        try:
            from app.core.sync_engine import sync_engine
            from app.network import catalog
            await run_sync(catalog.publish_owned)
            await sync_engine.broadcast_user_session_created("admin", result["session_id"])
        except Exception:
            log.exception("Remote StartSession queued, but publishing the new chat failed")
    from app.network import engine
    ctx = engine.context()
    result["owner_device_id"] = ctx.device_id if ctx else None
    return result


def _queue(sender_id, data, action, placement=None, model_plan=None):
    """``placement`` is :func:`_project_placement`'s answer for a StartSession
    that named a project (the owner's assistant only). ``model_plan`` is
    :func:`_confirm_start_model`'s answer when the call passed ``model``: a
    plan for a switched model the user asked for, or False for the model the
    chat inherits anyway."""
    from app.db import sessions, turns, mobile_tool_identity
    from app.db.base import transaction
    from app.core import turn_scheduler
    from app.core.assistant_service import is_primary_session
    from app.core.task_registry import task_registry

    sender = _caller(sender_id)
    context = _context(sender_id)
    if ((context.get("payload") or {}).get("_assistant_proactive") is not None
            or (context.get("overrides") or {}).get("_assistant_proactive") is not None):
        raise PermissionError("Background observation cannot start or message sessions.")
    from app.core import assistant_limits
    assistant_limits.assert_can_start(sender_id)  # The owner's daily spending limit.
    request_id = _text(data, "request_id", 120)
    text = _text(data, "prompt" if action == "start" else "message", 8000)
    # Human input starts a fresh budget; all automated descendants share it.
    ancestry = context.get("payload") or {}
    root_id = ancestry.get("orchestration_root") or context["id"]
    depth = int(ancestry.get("orchestration_depth") or 0)
    if depth >= MAX_HOPS:
        raise ValueError("Peer-chain limit reached (8 hops); ask the user to continue.")
    # A chain begun by another worker's StartSession has its budget over there;
    # it may message local peers but never fan out into more chats.
    remote_origin = ancestry.get("orchestration_remote_origin")
    if action == "start" and remote_origin:
        raise ValueError("This chat was started from another worker and cannot start more chats; "
                         "ask the user to continue.")
    if action != "start" and data.get("worker") is not None:
        raise ValueError("worker applies only to StartSession.")
    if action != "start" and data.get("project") is not None:
        raise ValueError("project applies only to StartSession.")
    if action != "start" and data.get("model") is not None:
        raise ValueError("model applies only to StartSession.")
    if action == "start" and data.get("model") is not None and model_plan is None:
        raise ValueError("model was not checked; nothing was started.")
    plan = model_plan or None
    if placement is None and data.get("project") is not None:
        raise ValueError("project is available only in the owner's assistant conversation; omit it.")
    if placement is not None:
        remote = placement["remote"]
    else:
        remote = _remote_worker(data.get("worker")) if action == "start" else None
    # Chats the owner's assistant starts report back to it and route their
    # approvals to its conversation; every other caller is unchanged.
    assistant = action == "start" and is_primary_session(sender_id)
    project_v2_id = placement["project"]["id"] if placement is not None else None
    if remote is not None:
        principal = _principal(sender_id)
        # A tool identity is a credential of THIS install and cannot travel; the
        # destination runs the chat as its owner, so only owner chats may ask.
        if sender.get("api_user_id") or (principal and principal.get("kind") != "admin"):
            raise PermissionError("Only the owner's chats can start chats on another worker.")
    command_id = "orchestration:" + action + ":" + hashlib.sha256(
        (sender_id + ":" + request_id).encode()).hexdigest()
    fingerprint = hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()
    target = sender if action == "start" else _target(sender, _text(data, "session_id", 200), allow_stopped=True)
    target_context = context if action == "start" else _context(target["id"], allow_empty=True)
    principals = {sid: _principal(sid) for sid in {sender_id, target["id"]}}
    # Never retry a rejected peer's work by waking it through a different tool.
    terminal = turn_scheduler._turn_terminal_payload(target["id"], target_context["id"])
    errors = str(terminal.get("errors") or "").casefold()
    payload = turn_scheduler._context_payload(target_context)
    # Keep transport attribution from the RECEIVER, never the sender.
    payload.update(orchestration_root=root_id, orchestration_depth=depth + 1,
                   orchestration_sender=sender_id, orchestration_fingerprint=fingerprint,
                   peer_message_content=defang_harness_markers(text),
                   peer_sender_label=sender.get("title") or sender_id)
    if remote_origin:
        payload["orchestration_remote_origin"] = remote_origin
    if target.get("api_user_id"):
        payload["api_user_id"] = target["api_user_id"]
    prompt = format_peer_message(sender_id, defang_harness_markers(text)).strip()
    prompt = prompt.replace("another agent in this session", "another top-level session", 1)
    overrides = target_context.get("overrides") or {}
    # Read before the chain lock below: it may look up the sender's agent.
    started_mode = _started_permission_mode(sender, overrides) if action == "start" else None
    with transaction() as conn:
        cursor = conn.cursor()
        # One database lock serializes all fanout in this autonomous chain.
        cursor.execute("SELECT id FROM turns WHERE id=%s FOR UPDATE", (root_id,))
        if not cursor.fetchone():
            raise ValueError("Orchestration origin no longer exists.")
        cursor.execute("SELECT * FROM turns WHERE parent_turn_id=%s AND command_id=%s",
                       (root_id, command_id))
        previous = turns._row(cursor.fetchone())
        # Recheck eligibility using THIS connection. Nested pool checkouts
        # while holding the origin lock can deadlock under concurrent fanout.
        # No model input can turn an archived/foreign/worker chat into a peer.
        for snapshot in (sender, target):
            cursor.execute("SELECT * FROM sessions WHERE id=%s", (snapshot["id"],))
            current = cursor.fetchone()
            if not _local_top_level(current) or any(
                current.get(key) != snapshot.get(key) for key in (
                    "api_user_id", "instance_id", "owner_device_id", "profile_id",
                    "provider_id", "model", "permission_mode", "project_id", "project_v2_id",
                )
            ):
                raise ValueError("Session scope or permissions changed; rediscover the peer.")
            cursor.execute("SELECT * FROM mobile_tool_identity WHERE session_id=%s", (snapshot["id"],))
            identity = cursor.fetchone()
            if (dict(identity) if identity else None) != principals[snapshot["id"]]:
                raise ValueError("Session owner changed; rediscover the peer.")
        if previous:
            if previous["payload"].get("orchestration_fingerprint") != fingerprint:
                raise ValueError("request_id was already used with different arguments.")
            made_remotely = previous["session_id"].startswith(REMOTE_RECEIPT_PREFIX)
            if made_remotely and (remote is None or remote["device_id"] != previous["payload"].get("remote_device_id")):
                # An automatic worker pick may differ on retry; the chat stays
                # where the first request made it.
                device = previous["payload"].get("remote_device_id")
                remote = {"device_id": device, "device_name": _device_name(device)}
            if remote is not None and made_remotely:
                # Re-ask the destination: it answers with the chat it already made.
                return {"remote": remote, "receipt_id": previous["id"], "reserved": False,
                        "body": _remote_body(sender, data, overrides, payload, prompt, command_id, fingerprint,
                                             project_v2_id=project_v2_id, assistant=assistant,
                                             model_plan=plan, permission_mode=started_mode)}
            return {"session_id": previous["session_id"], "turn_id": previous["id"],
                    "status": "already_queued", "delivery_status": _delivery_status(previous),
                    "created": False,
                    "note": "Existing request receipt. No automatic reply; retry the same request_id to read delivery status."}
        cursor.execute("SELECT command_id FROM turns WHERE parent_turn_id=%s AND command_id LIKE 'orchestration:%%'", (root_id,))
        commands = [row["command_id"] for row in cursor.fetchall()]
        if len(commands) >= MAX_ACTIONS or (action == "start" and sum(c.startswith("orchestration:start:") for c in commands) >= MAX_STARTS):
            raise ValueError("Orchestration budget reached (32 actions / 4 new sessions per chain); ask the user to continue.")
        # A receipt may be read after Stop, but fresh work must still fail closed.
        if context.get("state") == "aborted" or target_context.get("state") == "aborted":
            raise ValueError("A participating session was aborted; the user must continue it first.")
        if any(word in errors for word in ("cyber_policy", "content_policy", "policy rejection", "policy violation", "safety policy")):
            raise ValueError("The target has a policy rejection; ask the user rather than replaying it.")
        if task_registry.is_user_stopped(sender_id) or task_registry.is_user_stopped(target["id"]):
            raise ValueError("The user stopped a participating session.")
        if remote is not None:
            body = _remote_body(sender, data, overrides, payload, prompt, command_id, fingerprint,
                                project_v2_id=project_v2_id, assistant=assistant, model_plan=plan,
                                permission_mode=started_mode)
            receipt_payload = {"orchestration_fingerprint": fingerprint, "remote_device_id": remote["device_id"],
                               "remote_session_id": body["session_id"]}
            if assistant:
                # Where that chat's report is delivered, and the chain it belongs to.
                receipt_payload.update(report_to=sender_id, orchestration_depth=depth + 1)
            receipt = turns.enqueue_turn(
                REMOTE_RECEIPT_PREFIX + body["session_id"], "agent_message",
                profile_id=sender["profile_id"], command_id=command_id, parent_turn_id=root_id,
                payload=receipt_payload, _conn=conn)
            # Settled at birth: a receipt is accounting, never runnable work.
            cursor.execute("UPDATE turns SET state='completed', started_at=NOW(), ended_at=NOW(), "
                           "stop_reason='remote_start' WHERE id=%s", (receipt["id"],))
            return {"remote": remote, "receipt_id": receipt["id"], "reserved": True, "body": body}
        if action == "start":
            title = _text(data, "title", 160)
            new_id = str(uuid4())
            # A named project runs in that project's checkout here, never in
            # the caller's workspace.
            workspace = {} if placement is not None else {
                "project_id": sender.get("project_id"), "project_v2_id": sender.get("project_v2_id"),
                "worktree_id": sender.get("worktree_id"), "worktree_branch": sender.get("worktree_branch"),
                "worktree_path": sender.get("worktree_path")}
            if placement is not None:
                workspace["project_v2_id"] = project_v2_id
            run = {"provider_id": sender.get("provider_id"),
                   "model": overrides.get("model") or sender.get("model"),
                   "effort": overrides.get("effort", sender.get("effort")),
                   "context_window": sender.get("context_window"), "fast": sender.get("fast")}
            if plan:
                # A model the user asked for (#1830): its provider, and only
                # the inherited settings that still apply to it.
                from app.core import model_override
                mapped = model_override.settings_for_model(
                    plan["model"], plan["provider_id"], effort=run["effort"],
                    context_window=run["context_window"], fast=run["fast"])
                run.update(provider_id=plan["provider_id"], model=plan["model"], **mapped)
                overrides = _start_overrides(overrides, plan, mapped)
                payload["model_override"] = _override_record(plan)
            target = sessions.create_session(
                session_id=new_id, title=title, profile_id=sender["profile_id"],
                api_user_id=sender.get("api_user_id"), is_active=True,
                provider_id=run["provider_id"], derive_provider_from_profile=False,
                model=run["model"], effort=run["effort"],
                permission_mode=started_mode,
                context_window=run["context_window"], fast=run["fast"],
                mode=sender.get("mode"), _conn=conn, **workspace,
            )
            if sender.get("branch") and placement is None:
                cursor.execute("UPDATE sessions SET branch=%s WHERE id=%s", (sender["branch"], new_id))
            mobile_tool_identity.bind(new_id, principals[sender_id], new=True, conn=conn)
        turn = turns.enqueue_turn(
            target["id"], "agent_message", prompt=prompt,
            profile_id=target["profile_id"], project_id=target.get("project_id"),
            worktree_id=None if placement is not None else (
                target_context.get("worktree_id") or target.get("worktree_id")),
            overrides=overrides, payload=payload, command_id=command_id,
            parent_turn_id=root_id, _conn=conn,
        )
    from app.core.assistant_service import home_for_session, link_job
    home = home_for_session(sender_id)
    if home and target["id"] != home:
        link_job(home, target["id"])
    result = {"session_id": target["id"], "turn_id": turn["id"], "status": "queued",
              "delivery_status": "queued", "created": action == "start", "url": "/?session=" + target["id"],
              "note": "Queued is not delivered. Retry the same request_id to read queued/begun/consumed/stopped status. No automatic reply or completion notification; peers must use MessageSession. New sessions share the caller's workspace; coordinate edits or choose a separate branch."}
    if action == "start":
        result.update(_model_receipt(target, plan))
    if assistant:
        project = placement["project"] if placement is not None else None
        result["note"] = (
            ("Runs in " + (project.get("name") or project["id"]) + " on this worker. " if project else "")
            + "It reports back to you automatically when it finishes or fails; do not ask it to use "
            "MessageSession. Its questions get the recommended option; approvals it needs appear to the "
            "owner in this conversation. Queued is not delivered.")
        if project:
            result["project"] = {"id": project["id"], "name": project.get("name")}
    return result


def _model_receipt(row, plan, *, model=None, provider_id=None):
    """The provider and model a started chat actually runs on, for the receipt."""
    from app.core import model_override
    model = model or (row or {}).get("model")
    provider_id = provider_id if row is None else (row or {}).get("provider_id")
    out = {"model": model, "provider": {"id": provider_id, "name": model_override.provider_label(provider_id)}}
    if plan:
        out["model_override"] = {"source": plan.get("source"), "instead_of": plan.get("baseline")}
    return out


def _device_name(device_id):
    try:
        from app.network import runners
        for row in runners.directory()["runners"]:
            if row["device_id"] == device_id:
                return row["device_name"]
    except Exception:  # noqa: BLE001 — a label only
        log.debug("worker name lookup failed for %s", device_id, exc_info=True)
    return device_id


async def _start_remote(plan, sender_id, sender_generation):
    """Ask the chosen worker to make the chat ``_queue`` reserved a receipt for."""
    from app.core.task_registry import task_registry
    from app.network import control
    remote = plan["remote"]
    name = remote["device_name"]

    async def release():
        # Only a request this call reserved, and only when nothing was made, is
        # given back; a retry of the same request_id stays safe either way.
        if plan["reserved"]:
            await asyncio.to_thread(_delete_receipt, plan["receipt_id"])

    if task_registry.stop_invalidated(sender_id, sender_generation):
        await release()
        raise ValueError("The user stopped this session; nothing was started.")
    try:
        answer = await control.call(remote["device_id"], REMOTE_ACTION, plan["body"], timeout=REMOTE_TIMEOUT)
    except control.ControlRefused as exc:
        await release()
        reason = str(exc)
        if "Unknown control action" in reason:
            reason = "it runs an older release; update it first"
        raise ValueError(f"{name} refused the new chat: {reason}") from exc
    except control.RunnerUnreachable as exc:
        # It may have been made: keep the receipt so a retry with the same
        # request_id reads it back instead of making a second chat.
        raise ValueError(f"{name} could not be reached: {exc} Retry with the same request_id; "
                         "it never makes a second chat.") from exc
    if not isinstance(answer, dict) or answer.get("session_id") != plan["body"]["session_id"]:
        raise ValueError(f"{name} gave an invalid answer; retry with the same request_id.")
    owner = answer.get("owner_device_id") or remote["device_id"]
    await asyncio.to_thread(_link_remote_chat, sender_id, answer["session_id"], owner, plan["body"].get("title"))
    result = {"session_id": answer["session_id"], "turn_id": answer.get("turn_id"),
              "status": answer.get("status") or "queued",
              "delivery_status": answer.get("delivery_status") or "queued",
              "created": bool(answer.get("created")),
              "worker": {"device_id": owner, "name": name},
              "url": "/?session=" + answer["session_id"],
              "note": (f"Runs on {name}, in this project's checkout there (not your worktree), with no "
                       "private-access grants. Queued is not delivered; retry the same request_id to read "
                       "its status. It cannot start chats or MessageSession you back; the user sees it "
                       "in the sidebar once the worker publishes it.")}
    # What the destination actually made it on; an older release does not
    # say, so the request's own model stands in.
    choice = plan["body"].get("model_choice")
    result.update(_model_receipt(
        None, {"source": choice.get("source"), "baseline": choice.get("instead_of")} if choice else None,
        model=answer.get("model") or plan["body"].get("model"),
        provider_id=answer.get("provider_id") if "model" in answer else plan["body"].get("provider_id")))
    if plan["body"].get("report_back"):
        if answer.get("report_back"):
            result["note"] = (f"Runs on {name}, in the project's checkout there, with no private-access "
                              "grants. It reports back to you automatically when it finishes or fails. It runs "
                              "in Away mode there: questions get the recommended option and approvals are "
                              "held back for the owner and listed in its report.")
        else:
            result["note"] += (f" {name} runs an older release, so this chat will not report back on "
                               "its own; tell the owner to check it there.")
    return result


def _link_remote_chat(sender_id, session_id, device_id, title):
    """Count a chat made on another worker against the assistant's limits when
    the assistant (its home or one of its jobs) asked for it. Never raises."""
    try:
        from app.core.assistant_service import link_remote_job
        link_remote_job(sender_id, session_id, device_id, "chat", title)
    except Exception:  # noqa: BLE001 — the chat exists either way
        log.warning("Could not link remote chat %s to the assistant's limits", session_id, exc_info=True)


def _delete_receipt(receipt_id):
    from app.db.base import get_db
    with get_db() as conn:
        conn.cursor().execute("DELETE FROM turns WHERE id=%s AND session_id LIKE %s",
                              (receipt_id, REMOTE_RECEIPT_PREFIX + "%"))


# ---------------------------------------------------------------------------
# Report-back: a chat the owner's assistant started tells the assistant when
# it comes to rest, through the same wake path Task completions use.
# ---------------------------------------------------------------------------

def _origin_turn(session_id, settled):
    """The turn that began the work ``settled`` finished (itself, or the most
    recent user/queue/peer turn before a continuation)."""
    from app.db import turns
    from app.db.base import get_db
    if settled.get("source") in _ORIGIN_SOURCES:
        return settled
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM turns WHERE session_id=%s AND source IN ('user','queue','agent_message') "
                       "AND state IN ('completed','aborted') AND position <= %s "
                       "ORDER BY position DESC LIMIT 1", (session_id, settled.get("position") or 0))
        return turns._row(cursor.fetchone())


def _turn_report(session_id, turn_id, outcome, origin_id):
    """``(status, report text, needs-owner items)`` for one settled turn.

    Needs-owner items are approvals deferred since ``origin_id`` began."""
    from app.core import provider_agent
    from app.core.away_mode import DECISION_SUBTYPE
    from app.core.task_registry import task_registry
    events = provider_agent._load_session_events(session_id)
    text = ""
    failed = outcome == "aborted"
    for event in events:
        if event.get("turn_id") != turn_id:
            continue
        payload = event.get("payload") or {}
        kind = event.get("event_type")
        if kind == "tool.result":
            text = ""
        elif kind == "assistant.text_delta":
            text += payload.get("text") or payload.get("delta") or ""
        elif kind == "message.assistant" and payload.get("kind") in (None, "text"):
            text += payload.get("content") or ""
        elif kind == "turn.aborted":
            failed = True
            text = payload.get("message") or "The chat was interrupted before it finished."
        elif kind == "turn.completed" and payload.get("is_error"):
            failed = True
            text = str(payload.get("errors") or "The chat failed.")
    since = min((event.get("seq") or 0 for event in events if event.get("turn_id") == origin_id), default=0)
    needs = []
    for event in events:
        payload = event.get("payload") or {}
        data = payload.get("data") or {}
        if (event.get("event_type") == "system.note" and payload.get("subtype") == DECISION_SUBTYPE
                and data.get("kind") == "deferred" and (event.get("seq") or 0) >= since):
            needs.append(str(data.get("action") or "an action that needs approval")[:300])
    if task_registry.is_user_stopped(session_id):
        status = "stopped"
    else:
        status = "failed" if failed else "completed"
    report, _ = provider_agent.bound_report(text or "The chat ended without a written report.")
    return status, report, list(dict.fromkeys(needs))[:MAX_NEEDS_OWNER]


def build_started_report(session_id, settled, outcome):
    """The report frame for a settled chat the owner's assistant started, with
    where it goes: ``(home session id or None, origin device or None, frame)``.
    None when nothing is owed (an ordinary chat, work the owner began in that
    chat themselves, a parked turn). Blocking."""
    if outcome not in ("completed", "aborted"):
        return None
    start = _start_turn(session_id)
    if not start:
        return None
    start_payload = start.get("payload") or {}
    home = _home_of_start(session_id, start)
    device = start_payload.get("orchestration_report_device")
    if not home and not device:
        return None
    origin = _origin_turn(session_id, settled)
    origin_payload = (origin or {}).get("payload") or {}
    # Work the owner typed into that chat is theirs, not a job to report.
    if (not origin or origin.get("source") != "agent_message"
            or origin_payload.get("orchestration_sender") != start_payload.get("orchestration_sender")):
        return None
    from app.db import sessions
    status, report, needs = _turn_report(session_id, settled["id"], outcome, origin["id"])
    row = sessions.get_session(session_id) or {}
    frame = {"task_id": session_id, "status": status, "summary": defang_harness_markers(report),
             "agent_message_turn_id": settled["id"], "kind": "session",
             "title": defang_harness_markers(str(row.get("title") or "")[:160])}
    if needs:
        frame["needs_owner"] = needs
    if home:
        # The wake joins the chain that started this chat, so reports never
        # grant a fresh StartSession budget; only owner input does.
        frame["orchestration_root"] = origin_payload.get("orchestration_root")
        frame["orchestration_depth"] = origin_payload.get("orchestration_depth")
    return home, (None if home else device), frame


async def report_settled_start(session_id, settled, outcome):
    """Settlement hook: send a started chat's report to the assistant. Never raises."""
    try:
        built = await asyncio.to_thread(build_started_report, session_id, settled, outcome)
        if not built:
            return
        home, device, frame = built
        from app.core import turn_scheduler
        if home:
            await turn_scheduler.on_started_session_terminal(home, frame)
            return
        from app.network import control
        await control.call(device, REPORT_ACTION, {"session_id": session_id, "report": frame},
                           timeout=REMOTE_TIMEOUT)
    except Exception:  # noqa: BLE001 — a report never breaks settlement
        log.warning("Could not report started chat %s back to the assistant", session_id, exc_info=True)


def accept_remote_report(body, from_device):
    """Assistant side of a report from a chat it started on another worker.

    Only the worker that holds the chat may report it, only for a StartSession
    receipt the assistant made there, and only into the current home.
    """
    from app.core.assistant_service import is_primary_session
    from app.db import turns
    from app.db.base import get_db
    from app.network import engine, membership
    ctx = engine.context()
    member = ctx.member(from_device) if ctx else None
    if (ctx is None or from_device == ctx.device_id or member is None
            or member.kind != membership.KIND_WORKER or member.status != membership.STATUS_ACTIVE):
        raise PermissionError("Only active workers of this account may report chats here.")
    session_id = _remote_text(body, "session_id", 64)
    report = body.get("report")
    if not isinstance(report, dict):
        raise ValueError("report must be an object")
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM turns WHERE session_id=%s", (REMOTE_RECEIPT_PREFIX + session_id,))
        receipt = turns._row(cursor.fetchone())
    payload = (receipt or {}).get("payload") or {}
    home = payload.get("report_to")
    if not receipt or payload.get("remote_device_id") != from_device or not home:
        raise PermissionError("No report was requested for this chat.")
    if not is_primary_session(home):
        raise ValueError("The assistant conversation that started this chat is gone.")
    status = report.get("status")
    if status not in REPORT_STATUSES:
        raise ValueError("Invalid report status")
    turn_id = _remote_text(report, "agent_message_turn_id", 64)
    summary = report.get("summary") if isinstance(report.get("summary"), str) else ""
    from app.core.provider_agent import bound_report
    needs = report.get("needs_owner") or []
    if not isinstance(needs, list):
        raise ValueError("Invalid needs_owner")
    title = report.get("title") if isinstance(report.get("title"), str) else ""
    frame = {"task_id": session_id, "status": status, "agent_message_turn_id": turn_id,
             "summary": bound_report(defang_harness_markers(summary) or "The chat ended without a written report.")[0],
             "kind": "session", "title": defang_harness_markers(title[:160]),
             "worker": member.name or from_device,
             "orchestration_root": receipt.get("parent_turn_id"),
             "orchestration_depth": payload.get("orchestration_depth")}
    needs = [defang_harness_markers(str(item)[:300]) for item in needs[:MAX_NEEDS_OWNER] if str(item).strip()]
    if needs:
        frame["needs_owner"] = needs
    return home, frame


async def handle_remote_report(body, from_device):
    """``orchestration-report`` control action: wake the assistant with the report."""
    from app.core import turn_scheduler
    from app.db.async_database import run_sync
    home, frame = await run_sync(accept_remote_report, body, from_device)
    await turn_scheduler.on_started_session_terminal(home, frame)
    return {"delivered": True}


def make_handler(supplier):
    async def handle(action, data, ctx):
        from app.core import turn_scheduler
        from app.core.task_registry import task_registry
        from app.db import turns
        from app.network import control, session_active
        try:
            sender_id = supplier()
            if action == "list":
                result = await asyncio.to_thread(list_sessions, sender_id, data)
            elif action in ("settle", "unsettle", "archive"):
                from app.core.session_management import manage
                result = await manage(sender_id, data, action)
            else:
                target_id = sender_id if action == "start" else data.get("session_id")
                sender_generation = task_registry.stop_generation(sender_id)
                target_generation = task_registry.stop_generation(target_id)
                # Worker checks may take a network round trip: before the lock.
                placement = (await _project_placement(sender_id, data)
                             if action == "start" and data.get("project") is not None else None)
                # A model choice may wait for the owner's answer: also before the lock.
                model_plan = (await _confirm_start_model(sender_id, data, placement)
                              if action == "start" else None)
                async with turn_scheduler.message_resume_lock(target_id):
                    result = await asyncio.to_thread(_queue, sender_id, data, action, placement, model_plan)
                    if "remote" in result:
                        # The destination kicks and publishes its own chat;
                        # nothing here is local work to wake or broadcast.
                        result = await _start_remote(result, sender_id, sender_generation)
                        return ToolOutcome(text=json.dumps(result), ui=result)
                    if result.get("delivery_status") == "queued" and (
                            task_registry.stop_invalidated(sender_id, sender_generation)
                            or task_registry.stop_invalidated(target_id, target_generation)):
                        await asyncio.to_thread(turns.remove_queued_turn, result["turn_id"])
                        raise ValueError("The user stopped a participating session; queued work was cancelled.")
                    if result.get("delivery_status") == "queued":
                        from app.core.session_runtime import try_steer_pending
                        await try_steer_pending(result["session_id"])
                        turn_scheduler.note_wake_pending(result["session_id"])
                        turn_scheduler.kick(result["session_id"])
                        try:
                            from app.api.chat_ws_handler import _v2_queue_view, _v2_peer_queue_view
                            from app.core.sync_engine import sync_engine
                            queue = await asyncio.to_thread(_v2_queue_view, result["session_id"])
                            peers = await asyncio.to_thread(_v2_peer_queue_view, result["session_id"])
                            await sync_engine.broadcast_queue_updated(result["session_id"], queue, pending_peer_messages=peers)
                        except Exception:
                            log.exception("Peer message queued, but queue broadcast failed")
                if result.get("created"):
                    try:
                        from app.core.sync_engine import sync_engine
                        from app.db import sessions
                        row = await asyncio.to_thread(sessions.get_session, result["session_id"])
                        await sync_engine.broadcast_user_session_created(row.get("api_user_id") or "admin", row["id"])
                    except Exception:
                        log.exception("Session queued, but sidebar broadcast failed")
            return ToolOutcome(text=json.dumps(result), ui=result)
        except (ValueError, PermissionError, LookupError, control.RunnerUnreachable,
                control.ControlRefused, session_active.MembershipUpdateFailed) as exc:
            return ToolOutcome(text=str(exc), is_error=True)
    return handle
