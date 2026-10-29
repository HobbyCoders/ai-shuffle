"""Self-service native push tokens and silent attention-state refresh."""
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict, SecretStr
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute

from app.api.mobile import get_device_with_scope
from app.db import mobile
from app.db.async_database import run_sync

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


class PushToken(BaseModel):
    model_config = ConfigDict(extra="forbid")
    token: SecretStr


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
