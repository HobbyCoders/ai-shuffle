"""In-app terminal routes and the terminal WebSocket (#1945, phase 1).

Owner only. Every route goes through :func:`require_terminal_owner` and the
socket through :func:`authenticate_terminal_socket`; both admit exactly:

* the owner's password session (cookie, or the session token as an explicit
  credential), from the local listener;
* a paired device whose account owner is the install administrator
  (``account_owner_admin``), the same rule ``require_admin`` applies;
* the desktop app's own loopback requests carrying its per-launch listener
  token (a credential no agent process inherits).

Everything else is refused, including passwordless local access (plain
loopback or trusted LAN): an agent's shell on this worker reaches the API
over loopback or the container's LAN address, so that bypass must not open a
shell. Agent-preview cookies, API keys, non-owner paired devices, peer-worker
device tokens and share links are refused too. No tool, hook or automation
calls into this module.

Contract: ``docs/internal/terminals.md``.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from app.core import terminals as service
from app.core.terminals import TerminalError, registry
from app.db.async_database import run_sync

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/terminals", tags=["Terminals"])
ws_router = APIRouter(tags=["Terminals"])

PROTOCOL_VERSION = 1
MAX_CLIENT_MESSAGE_BYTES = 64 * 1024
INPUT_BURST_BYTES = 256 * 1024
INPUT_BYTES_PER_SECOND = 128 * 1024
MESSAGE_BURST = 200
MESSAGES_PER_SECOND = 100
PING_SECONDS = 30.0
SEND_TIMEOUT_SECONDS = 10.0
# An open socket re-runs its handshake check this often; a revoked or expired
# owner session, or a paired device that lost owner admin, loses the socket.
AUTH_RECHECK_SECONDS = 15.0

OWNER_ONLY = "Only the owner can use terminals. Sign in with the owner password."
PREVIEW_REFUSED = "Agent previews can't use terminals."


# ---------------------------------------------------------------------------
# Authorization
# ---------------------------------------------------------------------------

def _preview_cookie_present(conn) -> bool:
    """A valid agent-preview cookie rides this request (it restricts every
    credential sent with it, as ``preview_access.enforce_limits`` does)."""
    from app.core import preview_access

    token = (getattr(conn, "cookies", None) or {}).get(preview_access.COOKIE_NAME)
    return bool(token) and preview_access.verify(token) is not None


def _desktop_loopback(conn) -> bool:
    from app.core import desktop_listener, owner_auth

    return bool(desktop_listener.configured() and owner_auth.loopback_request(conn))


def require_terminal_owner(request: Request) -> str:
    """``require_admin`` minus its passwordless and preview paths.

    Returns who is acting, for the log: ``owner``, ``paired:<client id>`` or
    ``desktop``. Never a credential.
    """
    from app.api.auth import require_admin

    if _preview_cookie_present(request):
        raise HTTPException(403, PREVIEW_REFUSED)
    principal = require_admin(request)
    if principal.startswith("preview:"):
        raise HTTPException(403, PREVIEW_REFUSED)
    if principal == "local-owner":
        if _desktop_loopback(request):
            return "desktop"
        raise HTTPException(403, OWNER_ONLY)
    if principal.startswith("paired_client:"):
        return "paired:" + principal.split(":", 1)[1]
    return "owner"


async def authenticate_terminal_socket(websocket: WebSocket, token: Optional[str]) -> Optional[str]:
    """The WebSocket form of :func:`require_terminal_owner`.

    Origin is checked the way ``app.api.ws_auth`` checks it: an ambient
    session cookie and the desktop loopback path need a same-origin browser
    (or the desktop's own token-carrying request); an explicit token is a
    credential no other site can attach. Returns who, or None.
    """
    from app.api.auth import get_paired_client_from_token
    from app.core import owner_auth
    from app.core.pairing import PAIRED_SESSION_PREFIX

    if await run_sync(_preview_cookie_present, websocket):
        return None
    cookie = websocket.cookies.get("session")
    for candidate, ambient in ((token, False), (cookie, True)):
        if not candidate:
            continue
        if ambient:
            try:
                owner_auth.same_origin(websocket)
            except Exception:  # noqa: BLE001
                return None
        if candidate.startswith(PAIRED_SESSION_PREFIX):
            try:
                client = await run_sync(get_paired_client_from_token, candidate, touch=False)
            except HTTPException as exc:
                if exc.status_code >= 500:
                    raise
                return None
            except Exception:  # noqa: BLE001 — a failed check grants nothing
                return None
            if client and client.get("account_owner_admin") is True:
                return "paired:" + str(client["id"])
            return None
        if await run_sync(owner_auth.owner_session, websocket, candidate):
            return "owner"
        # An explicit credential that is not the owner's is a refusal, never a
        # reason to try the ambient cookie instead.
        return None
    if _desktop_loopback(websocket) and owner_auth.local_access(websocket):
        try:
            owner_auth.same_origin(websocket)
        except Exception:  # noqa: BLE001
            return None
        return "desktop"
    return None


# ---------------------------------------------------------------------------
# HTTP routes
# ---------------------------------------------------------------------------

def _error(exc: TerminalError) -> JSONResponse:
    return JSONResponse({"detail": exc.message, "code": exc.code}, status_code=exc.status)


class TargetRef(BaseModel):
    kind: str = Field("worker", max_length=32)
    worker_id: Optional[str] = Field(None, max_length=200)
    grant_id: Optional[str] = Field(None, max_length=200)


class CreateTerminal(BaseModel):
    session_id: Optional[str] = Field(None, max_length=200)
    cwd: Optional[str] = Field(None, max_length=4096)
    shell: Optional[str] = Field(None, max_length=32)
    cols: Optional[int] = None
    rows: Optional[int] = None
    target: Optional[TargetRef] = None


class PasteBody(BaseModel):
    text: str = Field(..., max_length=service.PASTE_MAX_CHARS * 2)


def _remote_target(target: Optional[TargetRef], worker: Dict[str, Any]) -> bool:
    return target is not None and (target.kind != service.TARGET_WORKER
                                   or bool(target.worker_id and target.worker_id != worker["id"]))


def _listing(worker: Dict[str, Any], session_id: Optional[str]) -> Dict[str, Any]:
    items = registry.list(session_id)
    return {
        "terminals": [t.to_dict() for t in items],
        "open": registry.count,
        "running": sum(1 for t in registry.list() if t.state != service.STATE_EXITED),
        "limit": registry.limit,
        "worker": worker,
    }


@router.get("")
async def list_terminals(session_id: Optional[str] = Query(None, max_length=200),
                         who: str = Depends(require_terminal_owner)):
    worker = await run_sync(service.worker_identity)
    return _listing(worker, session_id)


@router.get("/targets")
async def terminal_targets(session_id: Optional[str] = Query(None, max_length=200),
                           who: str = Depends(require_terminal_owner)):
    from app.core.terminal_targets import targets

    worker = await run_sync(service.worker_identity)
    other_targets = await targets(session_id)
    is_supported = service.supported()
    shells = service.available_shells() if service.pty_supported() else []
    default_folder = None
    if is_supported:
        try:
            path, source = await run_sync(service.resolve_folder, session_id, None)
            default_folder = {"path": path, "source": source}
        except TerminalError as exc:
            if exc.code != "chat_on_other_worker" or not other_targets:
                return _error(exc)
            # Do not let a mirrored chat's local-folder refusal hide peer targets.
            is_supported = False
            shells = []
    return {
        "worker": worker,
        "supported": is_supported or bool(other_targets),
        "unsupported_reason": None if is_supported or other_targets else "Terminals are not supported on this worker yet.",
        "targets": [{"kind": service.TARGET_WORKER, "worker_id": worker["id"], "name": worker["name"],
                     "available": is_supported}, *other_targets],
        "shells": [{"name": s["name"], "default": s["default"]} for s in shells],
        "default_shell": shells[0]["name"] if shells else None,
        "default_folder": default_folder,
        "limit": registry.limit,
        "open": registry.count,
    }


@router.post("", status_code=201)
async def create_terminal(body: CreateTerminal, request: Request, who: str = Depends(require_terminal_owner)):
    try:
        if _remote_target(body.target, await run_sync(service.worker_identity)):
            from app.core.terminal_targets import create
            terminal = await create(registry, body, who, request)
        else:
            cwd, cwd_root = await run_sync(_validated_terminal_cwd, body.cwd)
            terminal = await registry.create(
                created_by=who, session_id=body.session_id, cwd=cwd, cwd_root=cwd_root, shell=body.shell,
                cols=body.cols, rows=body.rows,
            )
    except TerminalError as exc:
        return _error(exc)
    return JSONResponse({"terminal": terminal.to_dict()}, status_code=201)


class FromFilesBody(BaseModel):
    path: str = Field("", max_length=4096)


def _validated_terminal_cwd(cwd: Optional[str]) -> tuple[Optional[str], Optional[str]]:
    if cwd is None:
        return None, None
    from app.api.workspace_files import validate_workspace_path

    # This is name resolution, not spawn authorization: the service reopens
    # the result without following links and pins it through create/restart.
    path, _ = service.resolve_folder(None, cwd)
    return str(validate_workspace_path(path)), str(validate_workspace_path(""))


@router.post("/from-files", status_code=201)
async def terminal_from_files(body: FromFilesBody, who: str = Depends(require_terminal_owner)):
    from app.api.workspace_files import validate_workspace_path

    path = await run_sync(validate_workspace_path, body.path)
    return await create_terminal(CreateTerminal(cwd=str(path)), who)


# Monitor watching is deliberately separate from interactive terminals. These
# routes never create a PTY, attach stdin, resize, stop, or take ownership of a
# command. The producer remains Monitor's existing bounded stdout reader.
MONITOR_REFRESH_SECONDS = 0.25
MONITOR_SNAPSHOT_CHARS = 256 * 1024


def _monitor_task(session_id: str, task_id: str):
    from app.mcp.builtin_tools import _background_tasks

    task = _background_tasks.get(task_id)
    if task is None or task.session_id != session_id:
        raise HTTPException(404, "Background shell task not found")
    return task


def _monitor_info(task) -> dict:
    from app.core.task_registry import task_registry

    record = task_registry.get_task(task.session_id, task.task_id) or {}
    return {
        "id": task.task_id, "session_id": task.session_id,
        "subject": record.get("subject") or task.command,
        "status": record.get("status") or (
            "running" if task.is_running() else "completed" if task.exit_code == 0 else "error"
        ),
        "started_at": task.started_at, "exit_code": task.exit_code,
    }


def _monitor_snapshot(task) -> dict:
    # Replacement snapshots tolerate the producer dropping its oldest lines,
    # without guessing a cursor from a bounded buffer's changing length.
    remaining = MONITOR_SNAPSHOT_CHARS
    lines = []
    truncated = False
    for line in reversed(task.lines):
        if len(line) + 1 > remaining:
            if remaining > 1:
                lines.append(line[-(remaining - 1):])
            truncated = True
            break
        lines.append(line)
        remaining -= len(line) + 1
    reader = task._reader_task
    return {
        "type": "output", "readonly": True, "task": _monitor_info(task),
        "lines": list(reversed(lines)), "truncated": truncated,
        "finished": not task.is_running() and (reader is None or reader.done()),
    }


@router.get("/monitor")
async def list_monitor_tasks(session_id: str = Query(..., min_length=1, max_length=200),
                             who: str = Depends(require_terminal_owner)):
    from app.mcp.builtin_tools import _background_tasks

    return {"tasks": [_monitor_info(task) for task in list(_background_tasks.values())
                      if task.session_id == session_id]}


@router.get("/monitor/{task_id}")
async def monitor_output(task_id: str, session_id: str = Query(..., min_length=1, max_length=200),
                         who: str = Depends(require_terminal_owner)):
    return _monitor_snapshot(_monitor_task(session_id, task_id))


@ws_router.websocket("/api/v1/ws/terminals/monitor/{task_id}")
async def monitor_socket(websocket: WebSocket, task_id: str,
                         session_id: str = Query(..., min_length=1, max_length=200),
                         token: Optional[str] = Query(None)) -> None:
    await websocket.accept()
    try:
        who = await authenticate_terminal_socket(websocket, token)
    except HTTPException:
        await _close(websocket, 1013, "Authorization unavailable; retry later")
        return
    if not who:
        await _close(websocket, 4001, "Authentication failed")
        return
    try:
        task = _monitor_task(session_id, task_id)
    except HTTPException:
        await _close(websocket, 4004, "Background shell task not found")
        return

    async def send_output():
        previous = None
        while True:
            snapshot = _monitor_snapshot(task)
            if snapshot != previous:
                await _send_json(websocket, snapshot)
                previous = snapshot
            if snapshot["finished"]:
                await _close(websocket, 1000, "Command finished")
                return
            await asyncio.sleep(MONITOR_REFRESH_SECONDS)

    async def refuse_input():
        message = await websocket.receive()
        if message["type"] != "websocket.disconnect":
            await _close(websocket, 1008, "Monitor output is read-only")

    tasks = [asyncio.create_task(send_output()), asyncio.create_task(refuse_input()),
             asyncio.create_task(_hold_authority(websocket, token, who, asyncio.Event()))]
    try:
        done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for task in done:
            task.result()
    except (WebSocketDisconnect, asyncio.TimeoutError, RuntimeError, OSError):
        pass
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


@router.get("/{terminal_id}")
async def get_terminal(terminal_id: str, who: str = Depends(require_terminal_owner)):
    try:
        return {"terminal": registry.get(terminal_id).to_dict()}
    except TerminalError as exc:
        return _error(exc)


@router.delete("/{terminal_id}")
async def close_terminal(terminal_id: str, who: str = Depends(require_terminal_owner)):
    try:
        await registry.close(terminal_id, by=who)
    except TerminalError as exc:
        return _error(exc)
    return {"closed": True, "id": terminal_id}


@router.post("/{terminal_id}/restart")
async def restart_terminal(terminal_id: str, who: str = Depends(require_terminal_owner)):
    try:
        terminal = await registry.restart(terminal_id, by=who)
    except TerminalError as exc:
        return _error(exc)
    return {"terminal": terminal.to_dict()}


@router.post("/{terminal_id}/paste")
async def paste_into_terminal(terminal_id: str, body: PasteBody, who: str = Depends(require_terminal_owner)):
    try:
        mode = await registry.paste(terminal_id, body.text)
        terminal = registry.get(terminal_id)
    except TerminalError as exc:
        return _error(exc)
    return {"mode": mode, "terminal": terminal.to_dict()}


# ---------------------------------------------------------------------------
# WebSocket
# ---------------------------------------------------------------------------

class _Bucket:
    """Token bucket: ``burst`` capacity refilled at ``rate`` per second."""

    def __init__(self, burst: float, rate: float):
        self.burst, self.rate = burst, rate
        self.tokens = burst
        self.stamp = time.monotonic()

    def take(self, amount: float) -> bool:
        now = time.monotonic()
        self.tokens = min(self.burst, self.tokens + (now - self.stamp) * self.rate)
        self.stamp = now
        if amount > self.tokens:
            return False
        self.tokens -= amount
        return True


async def _send_json(websocket: WebSocket, payload: Dict[str, Any]) -> None:
    await asyncio.wait_for(websocket.send_text(json.dumps(payload, default=str)), SEND_TIMEOUT_SECONDS)


async def _send_bytes(websocket: WebSocket, data: bytes) -> None:
    for start in range(0, len(data), service.OUTPUT_FRAME_BYTES):
        await asyncio.wait_for(websocket.send_bytes(data[start:start + service.OUTPUT_FRAME_BYTES]),
                               SEND_TIMEOUT_SECONDS)


async def _close(websocket: WebSocket, code: int, reason: str) -> None:
    try:
        await websocket.close(code=code, reason=reason)
    except Exception:  # noqa: BLE001 — already gone
        pass


async def _pump(websocket: WebSocket, viewer: service.Viewer) -> None:
    """Viewer outbox -> socket: output as binary frames, state as JSON."""
    try:
        while True:
            try:
                await asyncio.wait_for(viewer.event.wait(), PING_SECONDS)
            except asyncio.TimeoutError:
                await _send_json(websocket, {"type": "ping"})
                continue
            if viewer.overflowed:
                await _close(websocket, 4408, "Too far behind; reattach")
                return
            for kind, payload in viewer.drain():
                if kind == "output":
                    await _send_bytes(websocket, payload)
                else:
                    await _send_json(websocket, payload)
            if viewer.closed_reason:
                if viewer.closed_reason == "shutdown":
                    await _close(websocket, 1001, "Worker stopping")
                else:
                    await _close(websocket, 4410, "Terminal closed")
                return
    except (asyncio.TimeoutError, WebSocketDisconnect, RuntimeError, OSError):
        await _close(websocket, 1011, "Send failed")


def _limits() -> Dict[str, Any]:
    return {
        "max_message_bytes": MAX_CLIENT_MESSAGE_BYTES,
        "input_burst_bytes": INPUT_BURST_BYTES,
        "input_bytes_per_second": INPUT_BYTES_PER_SECOND,
        "message_burst": MESSAGE_BURST,
        "messages_per_second": MESSAGES_PER_SECOND,
        "cols": list(service.COLS_RANGE),
        "rows": list(service.ROWS_RANGE),
    }


@ws_router.websocket("/api/v1/ws/terminals/{terminal_id}")
async def terminal_socket(websocket: WebSocket, terminal_id: str, token: Optional[str] = Query(None)) -> None:
    await websocket.accept()
    try:
        who = await authenticate_terminal_socket(websocket, token)
    except HTTPException:
        await _close(websocket, 1013, "Authorization unavailable; retry later")
        return
    if not who:
        await _close(websocket, 4001, "Authentication failed")
        return
    try:
        terminal = registry.get(terminal_id)
    except TerminalError:
        await _close(websocket, 4004, "Terminal not found")
        return

    viewer, replay = terminal.attach()
    lost = asyncio.Event()
    tasks: list = []
    try:
        await _send_json(websocket, {
            "type": "attached", "protocol": PROTOCOL_VERSION, "terminal": terminal.to_dict(),
            "replay_bytes": len(replay), "limits": _limits(),
        })
        if replay:
            await _send_bytes(websocket, replay)
        await _send_json(websocket, {"type": "replayed"})
        pump = asyncio.create_task(_pump(websocket, viewer))
        guard = asyncio.create_task(_hold_authority(websocket, token, who, lost))
        receiver = asyncio.create_task(_receive(websocket, terminal, viewer, pump, lost))
        tasks = [pump, guard, receiver]
        await asyncio.wait({guard, receiver}, return_when=asyncio.FIRST_COMPLETED)
        if receiver.done():
            receiver.result()
    except (WebSocketDisconnect, asyncio.TimeoutError, RuntimeError, OSError):
        pass
    finally:
        terminal.detach(viewer)
        for task in tasks:
            if not task.done():
                task.cancel()
        for task in tasks:
            try:
                await task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass


async def _hold_authority(websocket: WebSocket, token: Optional[str], who: str,
                          lost: asyncio.Event) -> None:
    """Re-run the handshake's owner check for the life of the socket.

    The same credential (the explicit token, else the session cookie, else
    the desktop's own loopback request) must still name the same owner; a
    check that cannot reach a verdict fails closed. ``lost`` is set before
    the close so no input that arrives meanwhile reaches the shell.
    """
    while True:
        await asyncio.sleep(AUTH_RECHECK_SECONDS)
        try:
            current = await authenticate_terminal_socket(websocket, token)
        except Exception:  # noqa: BLE001 — HTTPException from the hub, a DB error
            lost.set()
            await _close(websocket, 1013, "Authorization unavailable; retry later")
            return
        if current != who:
            lost.set()
            await _close(websocket, 4001, "Signed out")
            return


async def _receive(websocket: WebSocket, terminal: service.Terminal, viewer: service.Viewer,
                   pump: asyncio.Task, lost: Optional[asyncio.Event] = None) -> None:
    """Socket -> shell. Replies go through the viewer's outbox so only the
    pump ever sends on the socket."""
    input_bytes = _Bucket(INPUT_BURST_BYTES, INPUT_BYTES_PER_SECOND)
    messages = _Bucket(MESSAGE_BURST, MESSAGES_PER_SECOND)
    while not pump.done():
        message = await websocket.receive()
        if message.get("type") == "websocket.disconnect" or (lost is not None and lost.is_set()):
            return
        if message.get("bytes") is not None:
            await _close(websocket, 4400, "Protocol violation")
            return
        text = message.get("text") or ""
        if len(text.encode("utf-8", errors="replace")) > MAX_CLIENT_MESSAGE_BYTES:
            await _close(websocket, 4400, "Message too large")
            return
        if not messages.take(1):
            await _close(websocket, 4429, "Input too fast")
            return
        try:
            data = json.loads(text)
        except ValueError:
            data = None
        if not isinstance(data, dict):
            await _close(websocket, 4400, "Protocol violation")
            return
        kind = data.get("type")
        if kind == "input":
            value = data.get("data")
            if not isinstance(value, str):
                await _close(websocket, 4400, "Protocol violation")
                return
            raw = value.encode("utf-8", errors="replace")
            if not input_bytes.take(len(raw)):
                await _close(websocket, 4429, "Input too fast")
                return
            if not raw:
                continue
            if lost is not None and lost.is_set():
                return
            try:
                await terminal.write(raw)
            except TerminalError as exc:
                viewer.push_state({"type": "error", "code": exc.code, "message": exc.message})
        elif kind == "resize":
            cols, rows = data.get("cols"), data.get("rows")
            if type(cols) is not int or type(rows) is not int:
                await _close(websocket, 4400, "Protocol violation")
                return
            terminal.resize(service._clamp(cols, service.COLS_RANGE, service.DEFAULT_COLS),
                            service._clamp(rows, service.ROWS_RANGE, service.DEFAULT_ROWS))
        elif kind == "ping":
            viewer.push_state({"type": "pong"})
        elif kind == "pong":
            continue
        else:
            await _close(websocket, 4400, "Protocol violation")
            return
