"""Account-hub backchannel. Product or administrator-selected origin, pinned TLS.

MOBILE_ACCOUNT_HUB_URL may override the product default. No caller-selected
origin is trusted, no proxy environment is inherited, and DNS is resolved once
per connection with every address checked before connecting to a pinned IP.
"""
import http.client
import ipaddress
import json
import logging
import re
import socket
import ssl
import threading
import time
from urllib.parse import urlsplit
from fastapi import HTTPException
from app.core.managed_credentials import secret_for
from app.core.config import settings
from app.db import mobile_hub as store

logger = logging.getLogger(__name__)

DEFAULT_HUB = 'https://shuffle-account-hub.example.workers.dev'

# Recent positive device verdicts: (server_id, hub_device_id, account_id) -> monotonic
# time of the last hub confirmation. Every paired-client request (middleware,
# route dependency, WebSocket handshake) used to make its own live hub call, and
# the hub caps a source IP at 120 requests a minute, so one busy phone session
# rate-limited this worker and every request failed 503. The cache bounds hub
# traffic to one call per device per TTL and lets a hub outage ride on the last
# confirmation for a grace window. Only positive verdicts are stored; a revoked
# answer drops the entry immediately. Local checks (binding active, device row,
# key expiry) still run on every request.
_VERDICT_LOCK = threading.Lock()
_verdicts: dict = {}
_VERDICT_CAP = 2048


def _verdict_key(owner):
    return (owner['server_id'], owner['hub_device_id'], owner['account_id'])


def _verdict_age(key):
    with _VERDICT_LOCK:
        confirmed_at = _verdicts.get(key)
    return None if confirmed_at is None else time.monotonic() - confirmed_at


def _record_verdict(key):
    with _VERDICT_LOCK:
        _verdicts[key] = time.monotonic()
        if len(_verdicts) > _VERDICT_CAP:
            oldest = sorted(_verdicts.items(), key=lambda item: item[1])
            for stale, _ in oldest[:len(_verdicts) - _VERDICT_CAP]:
                _verdicts.pop(stale, None)


def _forget_verdict(key):
    with _VERDICT_LOCK:
        _verdicts.pop(key, None)


def reset_verification_cache():
    """Drop every remembered hub verdict (sign-out, tests)."""
    with _VERDICT_LOCK:
        _verdicts.clear()


def configured_origin():
    return settings.mobile_account_hub_url.rstrip('/')


def trusted_origin(value):
    configured = configured_origin()
    for candidate in (value, configured):
        u = urlsplit(candidate)
        if (not candidate or u.scheme != 'https' or not u.hostname or u.username
                or u.password or u.query or u.fragment or u.path not in ('', '/')
                or u.port not in (None, 443) or any(ch.isspace() for ch in candidate)
                or '%' in candidate or '@' in candidate or '?' in candidate or '#' in candidate):
            raise ValueError('Administrator must configure an HTTPS account hub origin')
        host = u.hostname
        if host == 'localhost' or host.endswith(('.localhost', '.local', '.internal')):
            raise ValueError('Public hub required')
        try:
            address = ipaddress.ip_address(host)
        except ValueError:
            pass
        else:
            if not address.is_global:
                raise ValueError('Public hub required')
    if value.rstrip('/') != configured.rstrip('/'):
        raise ValueError('Hub does not match administrator-selected origin')
    return configured.rstrip('/')


def identifier(value):
    if not isinstance(value, str) or not re.fullmatch(r'[a-f0-9-]{36}', value):
        raise ValueError('Invalid hub identity')
    return value


class _PinnedHTTPS(http.client.HTTPSConnection):
    def connect(self):
        addresses = socket.getaddrinfo(self.host, 443, type=socket.SOCK_STREAM)
        if not addresses or any(not ipaddress.ip_address(a[4][0]).is_global for a in addresses):
            raise ValueError('Public hub required')
        raw = socket.create_connection((addresses[0][4][0], 443), self.timeout)
        try:
            self.sock = self._context.wrap_socket(raw, server_hostname=self.host)
        except Exception:
            raw.close()
            raise


def call(binding, method, suffix, body=None, *, credential=None, max_bytes=16384, timeout=None):
    """Bounded, nonredirecting request; never include hub bodies or secrets in errors.

    ``max_bytes`` caps the response (16 KiB by default; the network feed
    passes more). ``timeout`` overrides the per-route default.
    """
    conn = None
    detail = 'Account hub authorization unavailable'
    try:
        origin = trusted_origin(binding['hub_url'])
        server = identifier(binding['server_id'])
        if timeout is None:
            timeout = 65 if suffix in ('activate', 'connector') else 4
        conn = _PinnedHTTPS(urlsplit(origin).hostname, timeout=timeout, context=ssl.create_default_context())
        conn.request(method, f'/v1/servers/{server}/{suffix}',
                     body=json.dumps(body) if body is not None else None,
                     headers={'Authorization': 'Bearer ' + (credential if credential is not None else secret_for(binding)),
                              'Content-Type': 'application/json', 'Accept': 'application/json'})
        response = conn.getresponse()
        if response.status != 200:
            if suffix.startswith('browser-logins') and response.status in (401, 403):
                raise HTTPException(response.status, 'Account sign-in expired or authorization was rejected.')
            if response.status == 429:
                detail = 'Account hub is rate limiting this worker'
            raise ValueError(f'hub answered HTTP {response.status}')
        raw = response.read(max_bytes + 1)
        if len(raw) > max_bytes:
            raise ValueError('Oversize hub response')
        result = json.loads(raw)
        if not isinstance(result, dict):
            raise ValueError('Invalid hub response')
        return result
    except HTTPException:
        raise
    except Exception as exc:
        # The reason is the exception class plus its own message (socket, TLS,
        # JSON, or the status line above); hub bodies and credentials never
        # appear in it.
        logger.warning('Account hub %s %s failed: %s: %s', method, suffix, type(exc).__name__, exc)
        raise HTTPException(status_code=503, detail=detail) from None
    finally:
        if conn:
            conn.close()


def worker_link_call(action, body):
    """Only the two product-owned bootstrap routes may be called before binding."""
    if action not in ('start', 'poll'):
        raise ValueError('Invalid worker linking action')
    conn = None
    try:
        origin = trusted_origin(configured_origin())
        conn = _PinnedHTTPS(urlsplit(origin).hostname, timeout=8, context=ssl.create_default_context())
        conn.request('POST', '/v1/worker-links/' + action, body=json.dumps(body),
                     headers={'Content-Type': 'application/json', 'Accept': 'application/json'})
        response = conn.getresponse()
        raw = response.read(16385)
        if response.status != 200 or len(raw) > 16384:
            raise ValueError('Link unavailable')
        result = json.loads(raw)
        if not isinstance(result, dict):
            raise ValueError('Invalid response')
        return result
    except Exception:
        raise HTTPException(503, 'Account connection unavailable. Try again shortly.') from None
    finally:
        if conn:
            conn.close()


def require_active(owner):
    """Hub check for a device, at most one live call per verify TTL.

    None means a legacy/direct-paired credential. The local binding is checked
    on every call. The hub is asked again once the remembered verdict is older
    than ``mobile_account_hub_verify_ttl_seconds``; if the hub is unreachable
    or rate limiting, a verdict younger than
    ``mobile_account_hub_grace_seconds`` keeps the device authorized. A
    revoked answer, or an outage with no verdict to lean on, fails as before.
    """
    if owner is None:
        return
    binding = store.get_binding()
    if not binding or not binding['active'] or owner['server_id'] != binding['server_id']:
        raise HTTPException(status_code=401, detail='Account authorization revoked')
    suffix = 'devices/' + identifier(owner['hub_device_id'])
    key = _verdict_key(owner)
    age = _verdict_age(key)
    if age is not None and age < settings.mobile_account_hub_verify_ttl_seconds:
        return binding
    try:
        result = call(binding, 'GET', suffix)
    except HTTPException as exc:
        if exc.status_code >= 500 and age is not None and age < settings.mobile_account_hub_grace_seconds:
            logger.warning('%s; keeping device %s authorized on a verdict %.0fs old',
                           exc.detail, owner['hub_device_id'], age)
            return binding
        raise
    if (result.get('active') is not True or result.get('account_id') != owner['account_id']
            or result.get('device_id') != owner['hub_device_id']):
        _forget_verdict(key)
        raise HTTPException(status_code=401, detail='Account authorization revoked')
    _record_verdict(key)
    return binding


def mobile_hub_ownership(device):
    """Live-checked ownership dict or None for direct pairing. device is DB row."""
    owner = store.ownership(device['device_id'])
    require_active(owner)
    return owner


def check_paired_client(client):
    """Validate the device live and derive this installation owner's authority.

    Only administrator-started managed connections delegate administration to
    the hub's single owner account. Direct/controller-linked connections do not.
    The result is request-local evidence, never a stored or caller-selected role.
    """
    owner = store.ownership(paired_client_id=client['id'])
    if owner is None:
        return False
    from app.db.mobile import get_mobile_device
    from datetime import datetime, timezone
    device = get_mobile_device(owner['device_id'])
    if (not device or device.get('revoked_at') or device.get('paired_client_id') != client['id']
            or owner['paired_client_id'] != client['id']
            or device.get('device_id') != owner['device_id']
            or owner['device_id'] != client.get('device_id')
            or not store.valid_device_identity(owner)):
        raise HTTPException(status_code=401, detail='Mobile authorization revoked')
    expires = device.get('api_key_expires_at')
    if isinstance(expires, str):
        expires = datetime.fromisoformat(expires.replace('Z', '+00:00'))
    if not expires or expires <= datetime.now(timezone.utc):
        raise HTTPException(status_code=401, detail='Mobile authorization expired')
    binding = require_active(owner)
    from app.core.config import settings
    return (binding['linked_by'] == 'managed:' + settings.effective_instance_id
            and 'controller' in (client.get('scopes') or [])
            and 'web:session' in (device.get('scopes') or []))
