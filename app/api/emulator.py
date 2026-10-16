"""Phone panel API: this worker's Android emulator (docs/internal/phone-panel-api.md).

The emulator is a worker resource shared by every session, not one session's,
so these routes are the owner's: the owner's password session, a paired
device whose account owner is the install administrator, the local listener
the owner reaches the app on, and the agent's preview cookie (the same set
``require_auth`` marks ``is_admin``). API keys and non-owner paired devices
are refused; they have no session here to scope access to.

  GET  /api/v1/emulator/status
  POST /api/v1/emulator/setup                       {"accept_license": true}
  POST /api/v1/emulator/devices/{id}/start | /stop
  POST /api/v1/emulator/devices/{id}/install        multipart field "apk"
  WS   /api/v1/emulator/devices/{id}/stream
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import tempfile
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from starlette.datastructures import UploadFile
from starlette.formparsers import MultiPartException, MultiPartParser

from app.api.ws_utils import safe_ws_send
from app.core.android_emulator.manager import EmulatorError, get_manager
from app.db.async_database import run_sync

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/emulator", tags=["Emulator"])

MAX_APK_BYTES = 512 * 1024 * 1024
MAX_BODY = MAX_APK_BYTES + 65536
MAX_WS_MESSAGE = 16 * 1024
OWNER_ONLY = "Only the owner can use this worker's Android emulator."
_uploading: set = set()


def require_emulator_owner(request: Request) -> str:
    from app.api.auth import require_auth

    principal = require_auth(request)
    if getattr(request.state, "is_admin", False) is not True:
        raise HTTPException(403, OWNER_ONLY)
    return principal


def _error(exc: EmulatorError) -> JSONResponse:
    return JSONResponse({"detail": exc.message, "code": exc.code}, status_code=exc.status)


class SetupRequest(BaseModel):
    accept_license: bool = False
    system_image: Optional[str] = None


@router.get("/status")
async def emulator_status(_: str = Depends(require_emulator_owner)) -> dict:
    return await get_manager().status()


@router.post("/setup", status_code=202)
async def emulator_setup(body: SetupRequest, _: str = Depends(require_emulator_owner)):
    try:
        return await get_manager().setup(body.accept_license, body.system_image)
    except EmulatorError as exc:
        return _error(exc)


@router.post("/devices/{device_id}/start")
async def emulator_start(device_id: str, _: str = Depends(require_emulator_owner)):
    try:
        return (await get_manager().start(device_id)).to_dict()
    except EmulatorError as exc:
        return _error(exc)


@router.post("/devices/{device_id}/stop")
async def emulator_stop(device_id: str, _: str = Depends(require_emulator_owner)):
    try:
        return (await get_manager().stop(device_id)).to_dict()
    except EmulatorError as exc:
        return _error(exc)


@router.post("/devices/{device_id}/install")
async def emulator_install(device_id: str, request: Request, _: str = Depends(require_emulator_owner)):
    manager = get_manager()
    try:
        manager.running(device_id)
    except EmulatorError as exc:
        return _error(exc)
    if request.headers.get("content-type", "").split(";", 1)[0].strip().lower() != "multipart/form-data":
        raise HTTPException(400, "Send the APK as multipart/form-data in the field \"apk\".")
    length = request.headers.get("content-length", "")
    if length.isdigit() and int(length) > MAX_BODY:
        raise HTTPException(413, "APKs up to 512 MB can be installed.")
    if device_id in _uploading:
        raise HTTPException(409, "An install is already in progress on this device.")
    _uploading.add(device_id)
    too_large = False

    async def bounded_body():
        nonlocal too_large
        size = 0
        async for chunk in request.stream():
            size += len(chunk)
            if size > MAX_BODY:
                too_large = True
                raise MultiPartException("Upload too large")
            yield chunk

    try:
        try:
            form = await MultiPartParser(request.headers, bounded_body(), max_files=1, max_fields=0,
                                         max_part_size=MAX_BODY).parse()
        except MultiPartException:
            raise HTTPException(413 if too_large else 400,
                                "Send one APK of up to 512 MB in the field \"apk\".") from None
        try:
            items = form.multi_items()
            apk = items[0][1] if len(items) == 1 and items[0][0] == "apk" else None
            if not isinstance(apk, UploadFile):
                raise HTTPException(400, "Send one APK in the multipart field \"apk\".")
            if (apk.size or 0) > MAX_APK_BYTES:
                raise HTTPException(413, "APKs up to 512 MB can be installed.")
            uploads = manager.layout.root / ".uploads"
            await asyncio.to_thread(uploads.mkdir, parents=True, exist_ok=True)
            folder = await asyncio.to_thread(tempfile.mkdtemp, prefix="apk-", dir=str(uploads))
            path = Path(folder) / "upload.apk"
            try:
                out = await asyncio.to_thread(open, path, "wb")
                try:
                    while True:
                        block = await apk.read(1024 * 1024)
                        if not block:
                            break
                        await asyncio.to_thread(out.write, block)
                finally:
                    await asyncio.to_thread(out.close)
                package = await manager.install(device_id, path)
            finally:
                await asyncio.to_thread(_remove_tree, Path(folder))
            return {"package": package}
        finally:
            await form.close()
    except EmulatorError as exc:
        return _error(exc)
    finally:
        _uploading.discard(device_id)


def _remove_tree(folder: Path) -> None:
    import shutil

    shutil.rmtree(folder, ignore_errors=True)


# ---------------------------------------------------------------------------
# WebSocket
# ---------------------------------------------------------------------------
async def _owner_socket(websocket: WebSocket, token: Optional[str]) -> bool:
    """``authenticate_websocket`` narrowed to the owner (see the module docstring)."""
    from app.api.ws_auth import authenticate_websocket

    ok, api_user = await authenticate_websocket(websocket, token)
    if not ok or api_user is not None:
        return False
    principal = getattr(websocket.state, "mobile_tool_principal", None) or {}
    if principal.get("kind") == "admin":
        return True
    if principal.get("kind") == "paired":
        from app.db import pairing

        def owner_admin() -> bool:
            client = pairing.get_paired_client_by_session_hash(principal.get("credential_hash"))
            return bool(client and client.get("account_owner_admin") is True)

        return await run_sync(owner_admin)
    return False


def _number(data: dict, key: str) -> int:
    value = data.get(key)
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError(f"{key} must be a number")
    return int(round(value))


async def _handle_input(device_id: str, data: dict) -> None:
    manager = get_manager()
    kind = data.get("type")
    if kind == "tap":
        await manager.tap(device_id, _number(data, "x"), _number(data, "y"))
    elif kind == "swipe":
        duration = data.get("duration_ms", 300)
        await manager.swipe(device_id, _number(data, "x1"), _number(data, "y1"),
                            _number(data, "x2"), _number(data, "y2"),
                            _number({"d": duration}, "d") if duration is not None else 300)
    elif kind == "text":
        text = data.get("text")
        if not isinstance(text, str):
            raise ValueError("text must be a string")
        await manager.type_text(device_id, text)
    elif kind == "key":
        key = data.get("key")
        if not isinstance(key, str):
            raise ValueError("key must be a string")
        await manager.key(device_id, key)
    elif kind == "rotate":
        await manager.rotate(device_id)
    else:
        raise ValueError(f"unknown message type {kind!r}")


@router.websocket("/devices/{device_id}/stream")
async def emulator_stream(websocket: WebSocket, device_id: str, token: Optional[str] = Query(None)) -> None:
    from app.api.ws_key_watch import watch_api_key
    from app.core.android_emulator import stream

    await websocket.accept()
    if not await _owner_socket(websocket, token):
        await websocket.close(code=4001, reason="Authentication failed")
        return
    manager = get_manager()
    try:
        dev = manager.device(device_id)
    except EmulatorError:
        await websocket.close(code=4004, reason="Device not found")
        return
    hub = stream.hub_for(manager, dev)
    viewer = hub.attach()

    async def pump() -> None:
        while True:
            message = await viewer.next()
            try:
                await safe_ws_send(websocket, message)
            except Exception:  # noqa: BLE001 — a dead socket ends the stream
                try:
                    await websocket.close(code=1011)
                except Exception:  # noqa: BLE001
                    pass
                return

    sender = asyncio.create_task(pump(), name=f"emulator-ws-{device_id}")
    # Input runs in order, one at a time, off the receive loop.
    inputs: asyncio.Queue = asyncio.Queue(maxsize=64)

    async def apply() -> None:
        while True:
            data = await inputs.get()
            try:
                await _handle_input(device_id, data)
            except EmulatorError as exc:
                viewer.send({"type": "error", "message": exc.message, "code": exc.code})
            except ValueError as exc:
                viewer.send({"type": "error", "message": str(exc)[:300], "code": "bad_input"})
            except Exception:  # noqa: BLE001
                logger.debug("emulator input failed", exc_info=True)
                viewer.send({"type": "error", "message": "The input could not be sent.", "code": "input_failed"})

    applier = asyncio.create_task(apply(), name=f"emulator-input-{device_id}")
    try:
        async with watch_api_key(websocket):
            while True:
                try:
                    raw = await websocket.receive_text()
                except (WebSocketDisconnect, RuntimeError):
                    break
                try:
                    if len(raw) > MAX_WS_MESSAGE:
                        raise ValueError("message too large")
                    data = json.loads(raw)
                    if not isinstance(data, dict):
                        raise ValueError("message is not an object")
                except (ValueError, RecursionError):
                    await websocket.close(code=4400, reason="Protocol violation")
                    break
                if data.get("type") == "pong":
                    continue
                dev.touch()
                try:
                    inputs.put_nowait(data)
                except asyncio.QueueFull:
                    viewer.send({"type": "error", "message": "Too many inputs at once; slow down.",
                                 "code": "input_busy"})
    finally:
        sender.cancel()
        applier.cancel()
        hub.detach(viewer)
