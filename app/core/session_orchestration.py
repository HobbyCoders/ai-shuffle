"""Owner-scoped, durable top-level session orchestration.

Every action is a peer-authored turn, never a user command. Session identity,
permissions and ancestry come from the host, not model-authored arguments.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from uuid import uuid4

from fluid.context.peer import format_peer_message
from fluid.tools.hardening import defang_harness_markers
from fluid.tools.registry import ToolOutcome

log = logging.getLogger(__name__)
MAX_ACTIONS = 32
MAX_STARTS = 4
MAX_HOPS = 8


def _text(data, key, limit):
    value = data.get(key)
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        raise ValueError(f"{key} must be a non-empty string of at most {limit} characters")
    return value.strip()


def _local_top_level(row):
    from app.core.config import settings
    from app.network.runners import self_device_id
    return bool(row and row.get("instance_id") == settings.effective_instance_id
                and row.get("owner_device_id") in (None, self_device_id())
                and row.get("is_active") and not row.get("hidden")
                and not row.get("is_worker") and not row.get("parent_session_id")
                and row.get("status") != "archived" and row.get("mode") != "voice")


def _principal(session_id):
    from app.db import mobile_tool_identity
    return mobile_tool_identity.read(session_id)


def _owner(row):
    principal = _principal(row["id"])
    # Legacy install-local chats remain usable without acquiring mobile grants.
    return (row.get("api_user_id"), (principal or {}).get("owner", "install-admin"))


def _caller(session_id):
    from app.db import sessions
    from app.core import mobile_tool_identity
    from app.core.task_registry import task_registry
    row = sessions.get_session(session_id) if session_id else None
    if not _local_top_level(row):
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
    caller = _caller(session_id)
    scope = data.get("scope", "project")
    if scope not in ("project", "all"):
        raise ValueError("scope must be project or all")
    offset = data.get("offset", 0)
    if type(offset) is not int or offset < 0:
        raise ValueError("offset must be a non-negative integer")
    rows = sessions.get_sessions(api_user_id=caller.get("api_user_id") or "",
                                 active_only=True, limit=100, offset=offset)
    result = []
    caller_owner = _owner(caller)
    for row in rows:
        if (row["id"] == session_id or not _local_top_level(row)
                or _owner(row) != caller_owner
                or task_registry.is_user_stopped(row["id"])):
            continue
        if scope == "project" and (
            row.get("project_v2_id") or row.get("project_id")
        ) != (caller.get("project_v2_id") or caller.get("project_id")):
            continue
        entry = {key: row.get(key) for key in (
            "id", "title", "status", "profile_id", "project_id", "project_v2_id",
            "branch", "model", "updated_at")}
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


def _queue(sender_id, data, action):
    from app.db import sessions, turns, mobile_tool_identity
    from app.db.base import transaction
    from app.core import turn_scheduler
    from app.core.task_registry import task_registry

    sender = _caller(sender_id)
    context = _context(sender_id)
    request_id = _text(data, "request_id", 120)
    text = _text(data, "prompt" if action == "start" else "message", 8000)
    # Human input starts a fresh budget; all automated descendants share it.
    ancestry = context.get("payload") or {}
    root_id = ancestry.get("orchestration_root") or context["id"]
    depth = int(ancestry.get("orchestration_depth") or 0)
    if depth >= MAX_HOPS:
        raise ValueError("Peer-chain limit reached (8 hops); ask the user to continue.")
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
    return {"session_id": target["id"], "turn_id": turn["id"], "status": "queued",
            "delivery_status": "queued", "created": action == "start", "url": "/?session=" + target["id"],
            "note": "Queued is not delivered. Retry the same request_id to read queued/begun/consumed/stopped status. No automatic reply or completion notification; peers must use MessageSession. New sessions share the caller's workspace; coordinate edits or choose a separate branch."}


def make_handler(supplier):
    async def handle(action, data, ctx):
        from app.core import turn_scheduler
        from app.core.task_registry import task_registry
        from app.db import turns
        try:
            sender_id = supplier()
            if action == "list":
                result = await asyncio.to_thread(list_sessions, sender_id, data)
            else:
                target_id = sender_id if action == "start" else data.get("session_id")
                sender_generation = task_registry.stop_generation(sender_id)
                target_generation = task_registry.stop_generation(target_id)
                async with turn_scheduler.message_resume_lock(target_id):
                    result = await asyncio.to_thread(_queue, sender_id, data, action)
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
        except (ValueError, PermissionError) as exc:
            return ToolOutcome(text=str(exc), is_error=True)
    return handle
