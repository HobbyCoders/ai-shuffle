"""Scoped native Android polling/result API. No install-admin authentication."""
import json

from fastapi import APIRouter, Depends, HTTPException, Request

from app.api.mobile import get_device_with_scope
from app.db import mobile_tools as queue
from app.services.mobile_tools import TOOLS, CommandError, bounded_json, MAX_RESULT_BYTES

router = APIRouter(prefix='/api/v1/mobile/tools', tags=['Mobile tools'])


def device_auth(request: Request):
    return get_device_with_scope(request, 'mobile:actions')


async def body(request, limit):
    data = bytearray()
    async for chunk in request.stream():
        data.extend(chunk)
        if len(data) > limit:
            raise HTTPException(413, 'payload_too_large')
    try:
        value = json.loads(data)
        bounded_json(value, limit)
        return value
    except (ValueError, RecursionError):
        raise HTTPException(422, 'invalid_json_object') from None


@router.get('/pending')
def pending(device=Depends(device_auth)):
    from app.services.mobile_tools import authorized_devices
    commands = []
    # authorized_devices() runs a per-device credential/ownership query set, so
    # resolve each distinct session once per request instead of once per claimed
    # command. This snapshot is discarded at the end of the request.
    checked: dict[str, bool] = {}

    def allowed(session_id: str) -> bool:
        if session_id not in checked:
            try:
                checked[session_id] = any(
                    d['device_id'] == device['device_id'] for d in authorized_devices(session_id))
            except Exception:
                checked[session_id] = False
        return checked[session_id]

    for command in queue.claim(device['device_id']):
        session_id = command.pop('session_id')
        permitted = allowed(session_id)
        if permitted:
            commands.append(command)
        else:
            queue.cancel(command['id'], session_id)
    return {'commands': commands}


@router.post('/capabilities')
async def capabilities(request: Request, device=Depends(device_auth)):
    data = await body(request, 4096)
    enabled = data.get('enabled', False)
    tools = data.get('tools', [])
    if (data.keys() - {'enabled', 'tools'} or type(enabled) is not bool or
            not isinstance(tools, list) or len(tools) > len(TOOLS) or
            any(not isinstance(t, str) or t not in TOOLS for t in tools)):
        raise HTTPException(422, 'invalid_capabilities')
    from app.db.async_database import run_sync
    await run_sync(queue.capabilities, device['device_id'], enabled, sorted(set(tools)))
    return {'ok': True}


@router.post('/{command_id}/result')
async def result(command_id: str, request: Request, device=Depends(device_auth)):
    data = await body(request, MAX_RESULT_BYTES + 1024)
    if set(data) != {'result'}:
        raise HTTPException(422, 'result_object_required')
    try:
        encoded = bounded_json(data['result'], MAX_RESULT_BYTES)
    except CommandError as exc:
        raise HTTPException(422, str(exc)) from None
    from app.db.async_database import run_sync
    error = await run_sync(queue.submit_result, device['device_id'], command_id, encoded)
    if error:
        raise HTTPException({'not_found': 404, 'expired': 410}.get(error, 409), error)
    return {'ok': True}
