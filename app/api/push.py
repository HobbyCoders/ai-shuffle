"""Write-only owner sender settings and self-service native push tokens."""
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, SecretStr
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute

from app.api.auth import require_admin
from app.api.mobile import get_device_with_scope
from app.db import mobile, settings as settings_db
from app.db.async_database import run_sync
from app.services import push_credentials
from app.services.push_notification import device_receives_push

class _PushRoute(APIRoute):
    def get_route_handler(self):
        handler = super().get_route_handler()
        async def wrapped(request):
            try:
                response = await handler(request)
            except RequestValidationError:
                # FastAPI otherwise echoes invalid input (including pasted keys).
                response = JSONResponse({"detail": "Invalid notification request"}, status_code=422)
            response.headers["Cache-Control"] = "no-store"
            return response
        return wrapped


router = APIRouter(tags=["Notifications"], route_class=_PushRoute)


class SenderConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    service_account: SecretStr


class PushToken(BaseModel):
    model_config = ConfigDict(extra="forbid")
    token: SecretStr


async def status():
    devices = await run_sync(mobile.get_all_mobile_devices)
    count = 0
    for device in devices:
        if device_receives_push(device) and await run_sync(mobile.push_device_is_live, device):
            count += 1
    return {"configured": bool(await run_sync(settings_db.get_system_setting_revision, push_credentials.SETTING_KEY)),
            "devices_registered": count}


@router.get("/api/v1/settings/notifications")
async def sender_status(_: str = Depends(require_admin)):
    return await status()


@router.put("/api/v1/settings/notifications")
async def save_sender(body: SenderConfig, _: str = Depends(require_admin)):
    try:
        await run_sync(push_credentials.save_account, body.service_account.get_secret_value())
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from None
    except RuntimeError:
        raise HTTPException(503, "Secret storage is locked") from None
    return await status()


@router.delete("/api/v1/settings/notifications")
async def remove_sender(_: str = Depends(require_admin)):
    await run_sync(settings_db.delete_system_setting, push_credentials.SETTING_KEY)
    return await status()


def _device(request):
    device = get_device_with_scope(request, "mobile:notifications")
    if device.get("platform") != "android":
        raise HTTPException(403, "Android device required")
    return device


@router.put("/api/v1/mobile/push-token")
def register_token(body: PushToken, request: Request):
    device = _device(request)
    token = body.token.get_secret_value()
    if not token or len(token) > 4096 or any(ch.isspace() for ch in token):
        raise HTTPException(400, "Invalid push token")
    if not mobile.set_push_token(device, token):
        raise HTTPException(401, "Pair this device again")
    return {"registered": True}


@router.delete("/api/v1/mobile/push-token")
def unregister_token(request: Request):
    device = _device(request)
    if not mobile.set_push_token(device, None):
        raise HTTPException(401, "Pair this device again")
    return {"registered": False}


@router.get("/api/v1/mobile/push-state")
async def push_state(request: Request):
    await run_sync(_device, request)
    from app.services.attention_dispatcher import dispatcher, _candidates, current_command
    worker = await run_sync(dispatcher.worker_id)
    ids = await run_sync(_candidates, worker) if worker else []
    commands = []
    for sid in ids:
        command = await run_sync(current_command, sid, worker)
        if command:
            # Reopening/re-registering is a silent resync, never a fresh alert.
            command["action"] = "clear" if command["action"] == "clear" else "update"
            command["silent"] = "true"
            import time
            command["expires_at"] = str(int(time.time() + 600))
            commands.append(command)
    return {"worker_id": worker, "notifications": commands}
