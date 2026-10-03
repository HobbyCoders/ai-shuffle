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
# The destination answers after one local transaction; a slow tunnel still fits.
REMOTE_TIMEOUT = 20
_COMMAND_RE = re.compile(r"orchestration:start:[0-9a-f]{64}")


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


def _remote_worker(value):
    """The StartSession ``worker`` pick: None for this worker, else its directory row.

    Only an online, active worker of this account network qualifies. Anything
    else is refused by name; a bad pick never falls back to starting here.
    """
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip() or len(value) > 200:
        raise ValueError("worker must be a worker name or device id of at most 200 characters")
    wanted = value.strip()
    from app.network import engine, membership, runners
    ctx = engine.context()
    if ctx is None:
        raise ValueError("This worker is not part of a worker network; omit worker to start here.")
    rows = runners.directory()["runners"]
    matches = [row for row in rows if row["device_id"] == wanted]
    if not matches:
        matches = [row for row in rows if (row.get("device_name") or "").casefold() == wanted.casefold()]
    names = ", ".join(f"{row['device_name']} ({'this worker' if row['is_self'] else 'online' if row['online'] else 'offline'})"
                      for row in rows) or "none"
    if not matches:
        raise ValueError(f"Unknown worker {wanted!r}. Workers on this account: {names}.")
    if len(matches) > 1:
        raise ValueError(f"More than one worker is named {wanted!r}; pass its device id instead.")
    row = matches[0]
    if row["is_self"]:
        return None
    member = ctx.member(row["device_id"])
    if (member is None or member.kind != membership.KIND_WORKER
            or member.status != membership.STATUS_ACTIVE):
        raise ValueError(f"{row['device_name']} is not an active worker of this account.")
    if not row["online"]:
        raise ValueError(f"{row['device_name']} is offline; nothing was started. Ask the user to bring it "
                         "online, or omit worker to start here.")
    return {"device_id": row["device_id"], "device_name": row["device_name"]}


def _remote_session_id(command_id):
    # Deterministic, so a retried request lands on the same chat on the destination.
    return str(uuid5(NAMESPACE_URL, "ai-shuffle:" + command_id))


def _remote_body(sender, data, overrides, payload, prompt, command_id, fingerprint):
    """What the destination needs to make the chat. No path, worktree, branch,
    credential or tool identity: those belong to this install."""
    return {
        "session_id": _remote_session_id(command_id), "command_id": command_id,
        "fingerprint": fingerprint, "title": _text(data, "title", 160), "prompt": prompt,
        "peer_message_content": payload["peer_message_content"],
        "sender_session_id": payload["orchestration_sender"],
        "sender_label": payload["peer_sender_label"],
        "depth": payload["orchestration_depth"],
        "profile_id": sender["profile_id"], "project_v2_id": sender.get("project_v2_id"),
        "provider_id": sender.get("provider_id"),
        "model": overrides.get("model") or sender.get("model"),
        "effort": overrides.get("effort", sender.get("effort")),
        "permission_mode": overrides.get("permission_mode", sender.get("permission_mode")),
        "context_window": sender.get("context_window"), "fast": sender.get("fast"),
    }


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
    model = _remote_text(body, "model", 200, required=False)
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

    def receipt(turn, created):
        return {"session_id": session_id, "turn_id": turn["id"], "created": created,
                "status": "queued" if created else "already_queued",
                "delivery_status": _delivery_status(turn)}

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
    if provider_id and model_providers.get_model_provider(provider_id) is None:
        provider_id = None
    payload = {
        "orchestration_depth": depth, "orchestration_sender": sender_session,
        "orchestration_fingerprint": fingerprint, "orchestration_remote_origin": from_device,
        "peer_message_content": content,
        "peer_sender_label": f"{sender_label} (on {worker_name})",
        "forwarded_by_device": from_device,
    }
    overrides = {key: value for key, value in (("model", model), ("effort", effort),
                                                ("permission_mode", permission_mode)) if value}
    with transaction() as conn:
        sessions.create_session(
            session_id=session_id, title=title, profile_id=profile_id, project_v2_id=project_v2_id,
            is_active=True, provider_id=provider_id, derive_provider_from_profile=provider_id is None,
            model=model, effort=effort, permission_mode=permission_mode,
            context_window=context_window, fast=fast, _conn=conn)
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


def _queue(sender_id, data, action):
    from app.db import sessions, turns, mobile_tool_identity
    from app.db.base import transaction
    from app.core import turn_scheduler
    from app.core.task_registry import task_registry

    sender = _caller(sender_id)
    context = _context(sender_id)
    if ((context.get("payload") or {}).get("_assistant_proactive") is not None
            or (context.get("overrides") or {}).get("_assistant_proactive") is not None):
        raise PermissionError("Background observation cannot start or message sessions.")
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
    remote = _remote_worker(data.get("worker")) if action == "start" else None
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
            if remote is not None:
                # Re-ask the destination: it answers with the chat it already made.
                return {"remote": remote, "receipt_id": previous["id"], "reserved": False,
                        "body": _remote_body(sender, data, overrides, payload, prompt, command_id, fingerprint)}
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
            body = _remote_body(sender, data, overrides, payload, prompt, command_id, fingerprint)
            receipt = turns.enqueue_turn(
                REMOTE_RECEIPT_PREFIX + body["session_id"], "agent_message",
                profile_id=sender["profile_id"], command_id=command_id, parent_turn_id=root_id,
                payload={"orchestration_fingerprint": fingerprint, "remote_device_id": remote["device_id"],
                         "remote_session_id": body["session_id"]}, _conn=conn)
            # Settled at birth: a receipt is accounting, never runnable work.
            cursor.execute("UPDATE turns SET state='completed', started_at=NOW(), ended_at=NOW(), "
                           "stop_reason='remote_start' WHERE id=%s", (receipt["id"],))
            return {"remote": remote, "receipt_id": receipt["id"], "reserved": True, "body": body}
        if action == "start":
            title = _text(data, "title", 160)
            new_id = str(uuid4())
            target = sessions.create_session(
                session_id=new_id, title=title, profile_id=sender["profile_id"],
                project_id=sender.get("project_id"), project_v2_id=sender.get("project_v2_id"),
                api_user_id=sender.get("api_user_id"), is_active=True,
                provider_id=sender.get("provider_id"), derive_provider_from_profile=False,
                model=overrides.get("model") or sender.get("model"),
                effort=overrides.get("effort", sender.get("effort")),
                permission_mode=overrides.get("permission_mode", sender.get("permission_mode")),
                context_window=sender.get("context_window"), fast=sender.get("fast"),
                mode=sender.get("mode"), worktree_id=sender.get("worktree_id"),
                worktree_branch=sender.get("worktree_branch"), worktree_path=sender.get("worktree_path"),
                _conn=conn,
            )
            if sender.get("branch"):
                cursor.execute("UPDATE sessions SET branch=%s WHERE id=%s", (sender["branch"], new_id))
            mobile_tool_identity.bind(new_id, principals[sender_id], new=True, conn=conn)
        turn = turns.enqueue_turn(
            target["id"], "agent_message", prompt=prompt,
            profile_id=target["profile_id"], project_id=target.get("project_id"),
            worktree_id=target_context.get("worktree_id") or target.get("worktree_id"),
            overrides=overrides, payload=payload, command_id=command_id,
            parent_turn_id=root_id, _conn=conn,
        )
    from app.core.assistant_service import home_for_session, link_job
    home = home_for_session(sender_id)
    if home and target["id"] != home:
        link_job(home, target["id"])
    return {"session_id": target["id"], "turn_id": turn["id"], "status": "queued",
            "delivery_status": "queued", "created": action == "start", "url": "/?session=" + target["id"],
            "note": "Queued is not delivered. Retry the same request_id to read queued/begun/consumed/stopped status. No automatic reply or completion notification; peers must use MessageSession. New sessions share the caller's workspace; coordinate edits or choose a separate branch."}


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
    return {"session_id": answer["session_id"], "turn_id": answer.get("turn_id"),
            "status": answer.get("status") or "queued",
            "delivery_status": answer.get("delivery_status") or "queued",
            "created": bool(answer.get("created")),
            "worker": {"device_id": owner, "name": name},
            "url": "/?session=" + answer["session_id"],
            "note": (f"Runs on {name}, in this project's checkout there (not your worktree), with no "
                     "private-access grants. Queued is not delivered; retry the same request_id to read "
                     "its status. It cannot start chats or MessageSession you back; the user sees it "
                     "in the sidebar once the worker publishes it.")}


def _delete_receipt(receipt_id):
    from app.db.base import get_db
    with get_db() as conn:
        conn.cursor().execute("DELETE FROM turns WHERE id=%s AND session_id LIKE %s",
                              (receipt_id, REMOTE_RECEIPT_PREFIX + "%"))


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
                async with turn_scheduler.message_resume_lock(target_id):
                    result = await asyncio.to_thread(_queue, sender_id, data, action)
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
