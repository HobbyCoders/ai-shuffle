"""Driving a session that executes on another device.

The UI on worker A works against A's own database, where a session owned by
B is a read-only mirror. Anything that must run on B is a control request:

    submit a turn            -> B enqueues it exactly as a local send
    create a session on B    -> B creates the row (it is the owner) and
                                enqueues the first turn
    interrupt                -> B flips the live turn like its own stop button
    answer a prompt          -> B resolves the pending interaction
    hydrate a transcript     -> B publishes an old session's events

A calls B's ``/api/v1/network/peer/control/...`` with a device token; the
outcome shows up in A's mirror through the event feed. When B is
unreachable the caller gets ``RunnerUnreachable`` and the UI offers to fork
the mirrored transcript into a new session on an online runner.
"""
from __future__ import annotations

import logging
import re
from typing import Any, Dict, List, Optional

import httpx
from fastapi.encoders import jsonable_encoder

from app.db import network as store
from app.db.async_database import run_sync
from app.network import engine, membership, runners, transport

logger = logging.getLogger(__name__)

REQUEST_TIMEOUT = 20.0

# A strict ref name: no leading dash or dot, no "..", "@{", control
# characters, spaces, path tricks or the characters refs may not contain.
_BRANCH_RE = re.compile(
    r"^(?![-.])(?!.*\.\.)(?!.*@\{)(?!.*//)(?!.*/\.)(?!.*\.lock(?:/|$))"
    r"[^\x00-\x20\x7f~^:?*\[\\]+(?<![/.])$")


class RunnerUnreachable(RuntimeError):
    pass


class UnknownControlAction(LookupError):
    """No handler is registered for the requested control action."""


class ControlRefused(RuntimeError):
    """The owner answered with an error (4xx)."""


# ---------------------------------------------------------------------------
# Client side (the device whose UI is driving)
# ---------------------------------------------------------------------------

def owner_of(session_row: Dict[str, Any]) -> Optional[str]:
    """The device id that executes this session, or None when it is ours."""
    ctx = engine.context()
    if ctx is None:
        return None
    owner = session_row.get("owner_device_id")
    if not owner:
        instance = (session_row.get("instance_id") or "").strip()
        owner = runners.device_for_instance(instance) if instance else None
    if owner in (None, ctx.device_id):
        return None
    return owner


async def call(device_id: str, action: str, body: Optional[Dict[str, Any]] = None, *, timeout: float | None = None) -> Dict[str, Any]:
    """POST a control action to the owner with a device token."""
    ctx = engine.require_context()
    known = ctx.member(device_id)
    if known is not None and known.status != membership.STATUS_ACTIVE:
        # A removed device is untrusted: never hand it a prompt, an answer or
        # a stop. Its sessions stay read-only mirrors here (fork to continue).
        raise RunnerUnreachable("The owning worker was removed from this network")
    link = transport._links.get(device_id)
    if link is None:
        member = await run_sync(store.get_member, device_id)
        if member is None:
            raise RunnerUnreachable("Unknown device")
        from app.network.endpoints import peer_endpoints
        link = transport.PeerLink(device_id, peer_endpoints(member, engine.presence_of(device_id)))
        transport._links[device_id] = link
    if not link.endpoints:
        raise RunnerUnreachable("The owning worker has no reachable address")
    async with httpx.AsyncClient(follow_redirects=False) as client:
        try:
            await link.authenticate(client, ctx)
            # DB-backed control payloads contain datetimes (notably session rows).
            # httpx does not apply FastAPI's response encoding to outgoing JSON.
            for attempt in range(2):
                try:
                    result = await link._request(
                        client, "POST", f"/api/v1/network/peer/control/{action}",
                        json_body=jsonable_encoder(body or {}),
                        **({"timeout": timeout} if timeout is not None else {}),
                    )
                    break
                except transport.PeerTokenRejected:
                    # A worker restart invalidates tokens before their advertised
                    # expiry. A 401 is pre-dispatch, so retrying cannot duplicate
                    # a mutation. Never retry timeouts or other action failures.
                    if attempt:
                        raise
                    await link.authenticate(client, ctx)
        except transport.PeerError as exc:
            message = str(exc)
            if message.startswith("HTTP 4"):
                raise ControlRefused(message.split(" ", 2)[-1] if " " in message else message) from exc
            raise RunnerUnreachable(message) from exc
        except httpx.HTTPError as exc:
            raise RunnerUnreachable(f"{type(exc).__name__}") from exc
    transport.note_contact(device_id)
    return result


async def submit_turn(device_id: str, session_id: str, *, prompt: str, source: str = "user",
                      profile_id: Optional[str] = None, project_id: Optional[str] = None,
                      overrides: Optional[Dict[str, Any]] = None, payload: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    body = {
        "session_id": session_id, "source": source, "prompt": prompt, "profile_id": profile_id,
        "project_id": project_id, "overrides": overrides, "payload": payload,
    }
    blob_ids = (payload or {}).get("upload_blob_ids")
    if blob_ids:
        from app.network import git
        try:
            body["uploads"] = await git.export_uploads(blob_ids)
        except ValueError as exc:
            raise ControlRefused(str(exc)) from exc
    return await call(device_id, "turns", body)


async def create_session(device_id: str, session: Dict[str, Any], turn: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    return await call(device_id, "sessions", {"session": session, "turn": turn})


async def interrupt(device_id: str, session_id: str, mode: str, *, stop_background: bool = False,
                    turn_id: Optional[str] = None) -> Dict[str, Any]:
    return await call(device_id, "interrupt", {"session_id": session_id, "mode": mode,
                                               "stop_background": stop_background, "turn_id": turn_id})


async def answer_permission(device_id: str, *, request_id: str, session_id: str, decision: str,
                            remember: Optional[str], pattern: Optional[str], answered_by_device: Optional[str]) -> Dict[str, Any]:
    return await call(device_id, "permission", {
        "request_id": request_id, "session_id": session_id, "decision": decision, "remember": remember,
        "pattern": pattern, "answered_by_device": answered_by_device})


async def answer_question(device_id: str, *, request_id: str, session_id: str, answers: Dict[str, Any],
                          answered_by_device: Optional[str], dismissed: bool, skipped: Optional[List[str]]) -> Dict[str, Any]:
    return await call(device_id, "question", {
        "request_id": request_id, "session_id": session_id, "answers": answers,
        "answered_by_device": answered_by_device, "dismissed": dismissed, "skipped": skipped})


async def hydrate(device_id: str, session_id: str) -> Dict[str, Any]:
    return await call(device_id, "hydrate", {"session_id": session_id})


# ---------------------------------------------------------------------------
# Owner side (handlers behind /peer/control/{action})
# ---------------------------------------------------------------------------

def _rewind_handler():
    from app.api.rewind_owner import handle_rewind
    return handle_rewind


def _session_git_handler():
    from app.api.git_owner import handle_session_git
    return handle_session_git


def _project_git_handler():
    from app.api.git_owner import handle_project_git
    return handle_project_git


def _git_state_handler():
    from app.network import git
    return git.handle_state


def _git_catalog_handler():
    from app.network import git
    return git.handle_catalog


def _git_op_handler():
    from app.network import git
    return git.handle_op


# Handlers that live in modules imported lazily (they pull in the session
# runtime). One table serves both dispatch and ``known_action`` so a new
# action cannot be reachable in one and unknown to the other.
def _application_update_handler():
    from app.core.update_workers import peer_action
    return peer_action


def _handoff_handler():
    from app.network.handoff import handle
    return handle


def _session_active_handler():
    from app.network.session_active import handle
    return handle


def _permission_mode_handler():
    from app.api.sessions_permissions import handle
    return handle


_LAZY_HANDLERS = {
    "permission-mode": _permission_mode_handler,
    "session-active": _session_active_handler,
    "handoff": _handoff_handler,
    "application-update": _application_update_handler,
    "rewind": _rewind_handler,
    "session-git": _session_git_handler,
    "project-git": _project_git_handler,
    "git-state": _git_state_handler,
    "git-catalog": _git_catalog_handler,
    "git-op": _git_op_handler,
}


async def handle(action: str, body: Dict[str, Any], *, from_device: str) -> Dict[str, Any]:
    loader = _LAZY_HANDLERS.get(action)
    handler = loader() if loader is not None else _HANDLERS.get(action)
    if handler is None:
        raise UnknownControlAction(action)
    return await handler(body, from_device)


def known_action(action: str) -> bool:
    return action in _HANDLERS or action in _LAZY_HANDLERS


def _require_owned(session_id: str) -> Dict[str, Any]:
    from app.db import database
    row = database.get_session(session_id)
    if row is None:
        raise LookupError("Unknown session")
    if owner_of(row) is not None:
        raise PermissionError("This worker does not own the session")
    return row


def _valid_permission_mode(value: Any) -> Optional[str]:
    from app.api.voice_context import VALID_PERMISSION_MODES
    if value in (None, ""):
        return None
    if not isinstance(value, str) or value not in VALID_PERMISSION_MODES:
        raise ValueError("Invalid permission_mode")
    return value


def _valid_branch(value: Any) -> Optional[str]:
    if value in (None, ""):
        return None
    if not isinstance(value, str) or len(value) > 255 or not _BRANCH_RE.match(value):
        raise ValueError("Invalid branch name")
    return value


def _turn_overrides(raw: Any) -> Optional[Dict[str, Any]]:
    """Only the keys the local WebSocket composer sends (model, permission_mode).

    Anything else — ``cwd`` and ``worktree_info`` above all — is dropped so a
    member cannot run the owner's agent outside the session's checkout.
    """
    if not isinstance(raw, dict):
        return None
    overrides: Dict[str, Any] = {}
    model = raw.get("model")
    if isinstance(model, str) and model.strip():
        overrides["model"] = model.strip()[:200]
    permission_mode = _valid_permission_mode(raw.get("permission_mode"))
    if permission_mode:
        overrides["permission_mode"] = permission_mode
    return overrides or None


async def _turns(body: Dict[str, Any], from_device: str) -> Dict[str, Any]:
    from app.core import turn_manager
    session_id = str(body.get("session_id") or "")
    row = _require_owned(session_id)
    payload = dict(body.get("payload") or {})
    blob_ids = payload.get("upload_blob_ids") or []
    uploads = body.get("uploads") or []
    if blob_ids or uploads:
        if (not isinstance(blob_ids, list) or not isinstance(uploads, list)
                or len(blob_ids) > 50 or len(blob_ids) != len(uploads)
                or any(not isinstance(blob_id, str) for blob_id in blob_ids)
                or any(not isinstance(record, dict) or not isinstance(record.get("id"), str) for record in uploads)
                or len(set(blob_ids)) != len(blob_ids)
                or {record.get("id") for record in uploads} != set(blob_ids)):
            raise ValueError("Invalid upload transfer")
        from app.network import git
        await run_sync(git.import_uploads, session_id, uploads)
    payload.setdefault("forwarded_by_device", from_device)
    # Forwarded turns bypass the chat WebSocket's first-message title backfill.
    # Only the owner can persist a title that survives catalog replication.
    source = str(body.get("source") or "user")
    prompt = body.get("prompt")
    if source in {"user", "worktree"} and isinstance(prompt, str) and not (row.get("title") or "").strip():
        from app.core.session_title import derive_session_title
        from app.db import database
        title = derive_session_title(prompt)
        if title:
            await run_sync(database.update_session, session_id=session_id, title=title)
            from app.core.sync_engine import sync_engine
            await sync_engine.broadcast_session_updated(
                session_id=session_id, updates={"title": title}, source_device_id=None)
    turn_id = await turn_manager.submit_async(
        session_id, source, prompt=prompt,
        profile_id=body.get("profile_id"), project_id=row.get("project_id") or None,
        overrides=_turn_overrides(body.get("overrides")), payload=payload or None)
    return {"turn_id": turn_id, "session_id": session_id}


async def _sessions(body: Dict[str, Any], from_device: str) -> Dict[str, Any]:
    from app.db import database
    from app.db.async_database import run_sync
    session = dict(body.get("session") or {})
    session_id = str(session.get("id") or "")
    if not session_id:
        raise ValueError("session.id is required")
    allowed = {"profile_id", "project_v2_id", "title", "model", "effort", "permission_mode",
               "context_window", "fast", "mode", "hidden"}
    fields = {k: v for k, v in session.items() if k in allowed}
    profile_id = fields.pop("profile_id", None) or "default"
    # The session keeps the API key that started it on the member (api_users
    # replicate), so this row, which is the one every device mirrors, says the
    # same. A key this worker does not know is dropped: the session is the owner's.
    requested_api_user = session.get("api_user_id")
    requested_api_user = requested_api_user if isinstance(requested_api_user, str) and requested_api_user else None
    if "hidden" in fields:
        fields["hidden"] = bool(fields["hidden"])
    if "permission_mode" in fields:
        fields["permission_mode"] = _valid_permission_mode(fields["permission_mode"])
    branch = _valid_branch(session.get("branch"))

    def _create():
        existing = database.get_session(session_id)
        if existing is not None:
            return existing
        if database.get_profile(profile_id) is None:
            raise ValueError(f"Unknown profile: {profile_id}")
        api_user_id = requested_api_user if (
            requested_api_user and database.get_api_user(requested_api_user) is not None) else None
        return database.create_session(session_id=session_id, profile_id=profile_id, project_id=None,
                                       is_active=True, api_user_id=api_user_id, **fields)

    row = await run_sync(_create)
    _require_owned(session_id)
    if branch:
        await run_sync(database.set_session_branch, session_id, branch, session.get("project_v2_id"))
    if body.get("uploads"):
        from app.network import git
        await run_sync(git.import_uploads, session_id, body["uploads"])
    result = {"session_id": row["id"], "owner_device_id": row.get("owner_device_id")}
    turn = body.get("turn")
    if turn:
        result["turn"] = await _turns({**turn, "session_id": session_id}, from_device)
    from app.network import catalog
    await run_sync(catalog.publish_owned)
    return result


async def _interrupt(body: Dict[str, Any], from_device: str) -> Dict[str, Any]:
    from app.core import turn_manager
    session_id = str(body.get("session_id") or "")
    _require_owned(session_id)
    row = await turn_manager.interrupt_async(session_id, str(body.get("mode") or "stop"),
                                             stop_background=bool(body.get("stop_background")),
                                             turn_id=body.get("turn_id") or None)
    if row is not None and body.get("stop_background"):
        from app.core.query_session_lifecycle import stop_all_background_tasks
        await stop_all_background_tasks(session_id, user_stop=True)
    return {"interrupted": row is not None, "turn_id": (row or {}).get("id")}


async def _permission(body: Dict[str, Any], from_device: str) -> Dict[str, Any]:
    from app.core.permission_handler import permission_handler
    session_id = str(body.get("session_id") or "")
    _require_owned(session_id)
    result = await permission_handler.respond(
        request_id=str(body.get("request_id") or ""), session_id=session_id, decision=body.get("decision"),
        remember=body.get("remember"), pattern=body.get("pattern"),
        answered_by_device=from_device)
    return {"result": result}


async def _question(body: Dict[str, Any], from_device: str) -> Dict[str, Any]:
    from app.core.user_question_handler import user_question_handler
    session_id = str(body.get("session_id") or "")
    _require_owned(session_id)
    result = await user_question_handler.respond(
        request_id=str(body.get("request_id") or ""), session_id=session_id, answers=body.get("answers") or {},
        answered_by_device=from_device, dismissed=bool(body.get("dismissed")),
        skipped=body.get("skipped"))
    return {"success": bool(result)}


async def _hydrate(body: Dict[str, Any], from_device: str) -> Dict[str, Any]:
    from app.db.async_database import run_sync
    from app.network import catalog
    session_id = str(body.get("session_id") or "")
    _require_owned(session_id)
    published = await run_sync(catalog.hydrate, session_id)
    return {"published": published}


_HANDLERS = {
    "turns": _turns,
    "sessions": _sessions,
    "interrupt": _interrupt,
    "permission": _permission,
    "question": _question,
    "hydrate": _hydrate,
}


# ---------------------------------------------------------------------------
# Fork on offline
# ---------------------------------------------------------------------------

def fork_here(session_id: str, *, title: Optional[str] = None, api_user_id: Optional[str] = None) -> Dict[str, Any]:
    """New local session seeded with the mirrored transcript; the original is untouched."""
    import uuid
    from app.db import database
    from app.db.base import transaction
    from app.network import catalog
    original = database.get_session(session_id)
    if original is None:
        raise LookupError("Unknown session")
    context_text = catalog.transcript_text(session_id)
    new_id = str(uuid.uuid4())
    with transaction() as txn:
        database.create_session(
            session_id=new_id, profile_id=original.get("profile_id") or "default", project_id=None,
            project_v2_id=original.get("project_v2_id"),
            title=title or f"{original.get('title') or 'Session'} (forked)", api_user_id=api_user_id,
            parent_session_id=session_id, is_active=True, model=original.get("model"),
            effort=original.get("effort"), permission_mode=original.get("permission_mode"),
            context_window=original.get("context_window"), fast=original.get("fast"), mode=original.get("mode"),
            _conn=txn)
        if context_text:
            database.add_session_message(
                session_id=new_id, role="user", content=(
                    "Context from the original session, which ran on another device:\n\n" + context_text),
                metadata={"forked_from": session_id, "forked_context": True}, _conn=txn)
    return {"session_id": new_id, "parent_session_id": session_id, "context_chars": len(context_text)}
