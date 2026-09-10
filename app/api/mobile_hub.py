"""Explicit controller linking and public one-time account enrollment."""
import hashlib
import secrets
from datetime import datetime, timedelta, timezone
from fastapi import APIRouter, HTTPException, Request, Depends
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute
from pydantic import BaseModel, Field, SecretStr
from app.core.models import DeviceInfo, MobileDeviceRegisterResponse
from app.core import account_hub, pairing
from app.core.encryption import encrypt_value, decrypt_value
from app.db import mobile_hub as store
from app.api.pairing import _ExchangeRateLimiter
from app.api.auth import require_admin


class _HubRoute(APIRoute):
    def get_route_handler(self):
        handler = super().get_route_handler()
        async def wrapped(request):
            try:
                return await handler(request)
            except HTTPException as exc:
                return JSONResponse({'error': str(exc.detail)}, status_code=exc.status_code,
                                    headers={'Cache-Control': 'no-store'})
            except RequestValidationError:
                return JSONResponse({'error': 'Invalid request'}, status_code=422)
        return wrapped


router = APIRouter(prefix='/hub', tags=['Mobile account hub'], route_class=_HubRoute)
_limiter = _ExchangeRateLimiter(limit=30)


class ManagedLinkRequest(BaseModel):
    name: str = Field(min_length=1, max_length=80)


@router.get('/connection')
def connection_status(_admin=Depends(require_admin)):
    from app.core import managed_connection
    return _connection_response(managed_connection.status())


def _connection_response(result):
    from app.core.owner_auth import claim_url
    code = result.get('code')
    result['authorization_url'] = claim_url(code)
    result['signup_url'] = claim_url(code, 'signup')
    result['qr_svg'] = pairing.render_qr_svg('aishuffle://worker/link#code=' + code) if code else None
    return JSONResponse(result, headers={'Cache-Control': 'no-store'})


class UsernameUpdate(BaseModel):
    username: str = Field(min_length=1, max_length=64)


def _active_binding():
    binding = store.get_binding()
    if not binding:
        raise HTTPException(409, 'Sign in to a Shuffle account first')
    if not binding['active']:
        raise HTTPException(503, 'Your account is linked. Waiting for this worker to finish connecting to the account service.')
    return binding


@router.get('/account')
def account_profile(_admin=Depends(require_admin)):
    """Account inventory, scoped by the hub to this worker's owning account.

    Only display metadata is returned for workers, phones and browsers, never
    their credentials or sessions. Local owner access is required.
    """
    binding = _active_binding()
    return JSONResponse(_profile(account_hub.call(binding, 'GET', 'account'), binding['server_id']), headers={'Cache-Control': 'no-store'})


@router.delete('/account/{kind}/{registration_id}')
def revoke_account_registration(kind: str, registration_id: str, _admin=Depends(require_admin)):
    if kind not in ('servers', 'devices'):
        raise HTTPException(404, 'Unknown account registration type')
    try:
        account_hub.identifier(registration_id)
    except ValueError:
        raise HTTPException(400, 'Invalid account registration')
    binding = _active_binding()
    if kind == 'servers' and registration_id == binding['server_id']:
        raise HTTPException(409, 'Use Sign out to disconnect this worker')
    try:
        result = account_hub.call(binding, 'DELETE', f'account/{kind}/{registration_id}')
    except HTTPException as exc:
        if exc.status_code != 401:
            raise
        # Older hubs route unknown worker endpoints through consumer-session
        # auth and return 401 even though the worker credential is still valid.
        # Probe a known read-only endpoint; never retry the destructive request.
        account_hub.call(binding, 'GET', 'account')
        raise HTTPException(503, 'The account service needs an update before it can revoke devices. This worker is still signed in.') from exc
    if result.get('ok') is not True:
        raise HTTPException(503, 'Account revocation was not confirmed')
    if kind == 'devices':
        account_hub.reset_verification_cache()
    return JSONResponse({'ok': True, 'cleanup_pending': result.get('cleanup_pending') is True},
                        headers={'Cache-Control': 'no-store'})


@router.patch('/account')
def update_account_username(body: UsernameUpdate, _admin=Depends(require_admin)):
    username = body.username.strip().lower()
    if not account_hub.USERNAME_PATTERN.fullmatch(username):
        raise HTTPException(400, account_hub.USERNAME_HINT)
    binding = _active_binding()
    return JSONResponse(_profile(account_hub.call(binding, 'PATCH', 'account', {'username': username}), binding['server_id']),
                        headers={'Cache-Control': 'no-store'})


def _profile(result, server_id=None):
    account = result.get('account') if isinstance(result, dict) else None
    if not isinstance(account, dict) or not isinstance(account.get('username'), str):
        raise HTTPException(503, 'Account profile unavailable')
    servers = []
    for item in result.get('servers') or []:
        if not isinstance(item, dict):
            continue
        try:
            worker_id = account_hub.identifier(item.get('id'))
        except ValueError:
            continue
        servers.append({'id': worker_id, 'name': str(item.get('name') or '')[:80], 'url': item.get('url') if isinstance(item.get('url'), str) else None,
                        'current': bool(item.get('current'))})
    profile = {'account': {'id': account.get('id'), 'username': account['username'],
                           'profile_complete': bool(account.get('profile_complete'))},
               'servers': servers}
    # Absence means an older hub, not that the account has no phones/browsers.
    if isinstance(result.get('devices'), list):
        devices = []
        for item in result['devices']:
            if not isinstance(item, dict):
                continue
            try:
                device_id = account_hub.identifier(item.get('id'))
            except ValueError:
                continue
            devices.append({'id': device_id, 'name': str(item.get('name') or '')[:80],
                            'platform': str(item.get('platform') or '')[:32]})
            if server_id:
                devices[-1]['local_device_id'] = store.local_device_id(server_id, device_id)
        profile['devices'] = devices
    return profile


@router.get('/endpoint')
def endpoint_identity(request: Request):
    # Public, non-secret identity used to check the assigned route. Never returns
    # an account, device, connector token, or administrator credential.
    binding = store.get_binding()
    if not binding or not binding['active']:
        raise HTTPException(404, 'Worker connection unavailable')
    return JSONResponse({'server_id': binding['server_id'], 'ready': True,
                         'account_repair': 'scoped-v1',
                         'protected_ingress': request.scope.get('shuffle.ingress') == 'remote'},
                        headers={'Cache-Control': 'no-store'})


@router.get('/ingress')
def protected_ingress(request: Request):
    """Non-secret proof that the managed connector's separate socket is live."""
    from app.core.config import settings
    return JSONResponse({'instance_id': settings.effective_instance_id,
                         'protected_ingress': request.scope.get('shuffle.ingress') == 'remote'},
                        headers={'Cache-Control': 'no-store'})


@router.post('/connection')
def connect_account(body: ManagedLinkRequest, _admin=Depends(require_admin)):
    from app.core import managed_connection
    if any(ord(c) < 32 or ord(c) == 127 for c in body.name):
        raise HTTPException(400, 'Invalid worker name')
    return _connection_response(managed_connection.begin(body.name.strip()))


@router.delete('/connection')
async def disconnect_account(_admin=Depends(require_admin)):
    from app.core import managed_connection
    return _connection_response(await managed_connection.disconnect())


class LinkRequest(BaseModel):
    hub_url: str = Field(min_length=1, max_length=2048)
    server_id: str = Field(pattern=r'^[a-f0-9-]{36}$')
    server_secret: SecretStr = Field(min_length=64, max_length=64)


class EnrollRequest(BaseModel):
    ticket: SecretStr = Field(min_length=1, max_length=128)
    device_info: DeviceInfo


@router.post('/link')
def link(body: LinkRequest, request: Request):
    # Local import avoids mobile -> hub router -> mobile circular initialization.
    from app.api.mobile import get_device_with_scope
    device = get_device_with_scope(request, 'controller')
    if not device.get('paired_client_id'):
        raise HTTPException(403, 'Explicit controller pairing required')
    # Account-enrolled devices must never bootstrap or change the trust root.
    if store.ownership(device['device_id']) is not None:
        raise HTTPException(403, 'Use an explicitly paired controller to link')
    _limiter.check('link')
    try:
        origin = account_hub.trusted_origin(body.hub_url)
        server_id = account_hub.identifier(body.server_id)
    except ValueError:
        raise HTTPException(400, 'Hub must match the administrator-selected HTTPS origin') from None
    secret = body.server_secret.get_secret_value()
    try:
        encrypted = encrypt_value(secret)
        binding = store.bind(origin, server_id, encrypted, device['paired_client_id'])
        if (binding['hub_url'] != origin or binding['server_id'] != server_id
                or binding['linked_by'] != device['paired_client_id']
                or not secrets.compare_digest(decrypt_value(binding['encrypted_secret']), secret)):
            raise HTTPException(409, 'Server already bound to a different authorization')
    except (RuntimeError, ValueError):
        raise HTTPException(503, 'Server encryption unavailable') from None
    # Persist first, activate second. On a lost response only this same controller
    # and exact binding can retry. Never roll back into a mutable trust root.
    if account_hub.call(binding, 'POST', 'activate', {}).get('ok') is not True:
        raise HTTPException(503, 'Hub activation unavailable')
    store.activate(server_id)
    return {'ok': True}


@router.post('/enroll', response_model=MobileDeviceRegisterResponse)
def enroll(body: EnrollRequest):
    _limiter.check('enroll')
    binding = store.get_binding()
    if not binding or not binding['active']:
        raise HTTPException(403, 'Server has not been linked to an account hub')
    identity = account_hub.call(binding, 'POST', 'exchange', {'ticket': body.ticket.get_secret_value()})
    try:
        account_hub.identifier(identity.get('account_id'))
        account_hub.identifier(identity.get('device_id'))
        if identity['device_id'] != body.device_info.device_id:
            raise ValueError('Device mismatch')
        if not isinstance(identity.get('device_name'), str) or not 1 <= len(identity['device_name']) <= 100:
            raise ValueError('Invalid device name')
    except ValueError:
        raise HTTPException(403, 'Ticket identity does not match this installation') from None
    identity['server_id'] = binding['server_id']
    account_hub.require_active({'server_id': binding['server_id'],
                                'account_id': identity['account_id'], 'hub_device_id': identity['device_id']})
    key = 'aim_' + secrets.token_urlsafe(32)
    expires = datetime.now(timezone.utc) + timedelta(days=365)
    scopes = list(pairing.DEFAULT_MOBILE_SCOPES)
    try:
        store.enroll(identity, body.device_info, hashlib.sha256(key.encode()).hexdigest(), expires, scopes, scoped=True)
    except ValueError:
        raise HTTPException(409, 'Device already authorized; use a fresh installation identity') from None
    except Exception:
        # Includes uniqueness races: never leak database statements or key hashes.
        raise HTTPException(409, 'Device enrollment unavailable; obtain a new ticket') from None
    # Android keeps its account identity on the wire; rows and credentials are
    # distinct per worker even when both workers share the same database.
    return JSONResponse({'api_key': key, 'device_id': identity['device_id'],
                         'expires_at': expires.isoformat(), 'scopes': scopes},
                        headers={'Cache-Control': 'no-store'})
