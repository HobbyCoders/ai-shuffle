"""Account-hub backchannel. Product or administrator-selected origin, pinned TLS.

MOBILE_ACCOUNT_HUB_URL may override the product default. No caller-selected
origin is trusted, no proxy environment is inherited, and DNS is resolved once
per connection with every address checked before connecting to a pinned IP.
"""
from concurrent.futures import Future, TimeoutError
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

DEFAULT_HUB = 'https://hobbycoders.com'

# Username rules mirror the hub's PATCH .../account validation.
USERNAME_PATTERN = re.compile(r'^[a-z0-9][a-z0-9_.-]{2,63}$')
USERNAME_HINT = 'Use 3–64 letters, numbers, underscores, periods or hyphens, starting with a letter or number.'
ACCOUNT_PROFILE_ERRORS = {
    400: USERNAME_HINT,
    401: 'This worker is no longer linked to its account. Sign in again.',
    409: 'That username is already taken.',
    429: 'Too many username changes. Try again in a few minutes.',
}

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
_verification_flights: dict = {}
_verification_generation = 0
_VERDICT_CAP = 2048

# Badge metadata only: never consulted by authorization or revocation checks.
_DISPLAY_LOCK = threading.Lock()
_display_entry = None
_DISPLAY_TTL = 60.0
_DISPLAY_RETRY = 10.0


def reset_display_cache():
    global _display_entry
    with _DISPLAY_LOCK:
        _display_entry = None


def _display_identity(binding):
    if not binding or not binding.get('active'):
        return None
    return tuple(binding.get(k) for k in (
        'hub_url', 'server_id', 'linked_by', 'encrypted_secret', 'instance_id'))


def account_display(binding):
    """Single-flight, bounded display cache for the current worker binding.

    A rename/sign-out invalidates even in-flight results. Read the local binding
    again before returning: an old account's response must not cross a relink.
    Failures briefly back off; expired names are never served on hub failure.
    """
    global _display_entry
    key = _display_identity(binding)
    if key is None:
        raise HTTPException(409, 'Sign in to a Shuffle account first')
    with _DISPLAY_LOCK:
        entry = _display_entry
        leader = entry is None or entry['key'] != key or entry['expires'] <= time.monotonic()
        if leader:
            entry = {'key': key, 'future': Future(), 'expires': time.monotonic() + 5.0}
            _display_entry = entry
    if leader:
        ttl = _DISPLAY_TTL
        try:
            account = call(binding, 'GET', 'account').get('account')
            if not isinstance(account, dict) or not isinstance(account.get('username'), str):
                raise HTTPException(503, 'Account profile unavailable')
            # Do not retain the account's worker/device inventory or credentials.
            value = {'account': {k: account.get(k) for k in ('id', 'username', 'profile_complete')}}
            entry['future'].set_result(value)
        except Exception as exc:
            ttl = _DISPLAY_RETRY
            entry['future'].set_exception(exc)
        finally:
            with _DISPLAY_LOCK:
                entry['expires'] = time.monotonic() + ttl
    try:
        value = entry['future'].result(timeout=5)
    except TimeoutError:
        raise HTTPException(503, 'Account profile unavailable') from None
    current_key = _display_identity(store.get_binding())
    with _DISPLAY_LOCK:
        if current_key != key or _display_entry is not entry:
            raise HTTPException(409, 'Account connection changed. Try again.')
    return value


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
    global _verification_generation
    with _VERDICT_LOCK:
        _verdicts.clear()
        _verification_generation += 1


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


_recent_authorization = {}

# The hub answers 503 with Retry-After when an operator kill switch is on
# (android/hub/src/switches.ts). Until then every call is answered locally, so
# no loop on this worker keeps asking a paused hub. Keyed by hub origin.
_paused_until = {}
_MAX_RETRY_AFTER = 6 * 3600.0


class HubResponseError(HTTPException):
    """A failed hub call.

    ``status_code`` stays 503 so existing callers keep treating it as
    "unavailable". ``hub_status`` is what the hub answered (None when it was
    not reached) and ``retry_after`` its Retry-After in seconds, if any.
    """

    def __init__(self, detail, hub_status=None, retry_after=None):
        super().__init__(status_code=503, detail=detail)
        self.hub_status = hub_status
        self.retry_after = retry_after


class TunnelLimitError(HubResponseError):
    """The hub refused a new managed tunnel: the account is at its cap.

    The hub answers 409 ``{"code": "tunnel_limit", "limit", "used",
    "licensed"}`` on activate and connector calls (android/hub
    src/tunnel-capacity.ts). Only those three numbers are kept; the hub's own
    text is not echoed.
    """

    def __init__(self, limit, used, licensed, retry_after=None):
        super().__init__('Account tunnel limit reached', 409, retry_after)
        self.limit = limit
        self.used = used
        self.licensed = licensed


# Calls that may make the hub build a managed tunnel. They carry the worker's
# license lease (X-Shuffle-Lease) so a licensed account gets its larger cap.
LEASE_SUFFIXES = ('activate', 'connector')


def _lease_headers(suffix):
    if suffix not in LEASE_SUFFIXES:
        return {}
    try:
        from app.core import license as licensing
        lease = licensing.lease_header()
    except Exception:  # noqa: BLE001 - a license problem must never block the hub call
        logger.warning('Could not read the license lease for the account hub', exc_info=True)
        return {}
    # A header value must be one printable line; the lease is base64url.
    if not lease or len(lease) > 4096 or not re.fullmatch(r'[A-Za-z0-9_-]+', lease):
        return {}
    return {licensing.LEASE_HEADER: lease}


def _tunnel_limit(response, retry):
    """The hub's tunnel_limit refusal, or None for any other 409 body."""
    try:
        body = json.loads(response.read(4097)[:4096])
    except Exception:  # noqa: BLE001
        return None
    if not isinstance(body, dict) or body.get('code') != 'tunnel_limit':
        return None
    count = lambda v: v if isinstance(v, int) and not isinstance(v, bool) and 0 <= v <= 1000 else None
    limit, used = count(body.get('limit')), count(body.get('used'))
    if limit is None or used is None:
        return None
    return TunnelLimitError(limit, used, body.get('licensed') is True, retry)


def tunnel_limit(exc):
    """The TunnelLimitError behind a failed hub call, or None."""
    return exc if isinstance(exc, TunnelLimitError) else None


def hub_status(exc):
    return getattr(exc, 'hub_status', None)


def retry_after(exc):
    return getattr(exc, 'retry_after', None)


def _parse_retry_after(value):
    try:
        seconds = float(value)
    except (TypeError, ValueError):
        return None  # HTTP-date form is not used by the hub.
    if seconds != seconds or seconds < 0:
        return None
    return min(seconds, _MAX_RETRY_AFTER)


def reset_pause():
    _paused_until.clear()


def call(binding, method, suffix, body=None, *, credential=None, max_bytes=16384, timeout=None):
    """Bounded, nonredirecting request; never include hub bodies or secrets in errors.

    ``max_bytes`` caps the response (16 KiB by default; the network feed
    passes more). ``timeout`` overrides the per-route default.
    """
    conn = None
    detail = 'Account hub authorization unavailable'
    status = wait = None
    try:
        origin = trusted_origin(binding['hub_url'])
        server = identifier(binding['server_id'])
        remaining = _paused_until.get(origin, 0) - time.monotonic()
        if remaining > 0:
            raise HubResponseError('Account hub is paused', 503, remaining)
        if timeout is None:
            timeout = 65 if suffix in ('activate', 'connector') else 4
        conn = _PinnedHTTPS(urlsplit(origin).hostname, timeout=timeout, context=ssl.create_default_context())
        # The lease goes only here: to the administrator-selected hub origin,
        # on the two calls that can build a tunnel.
        conn.request(method, f'/v1/servers/{server}/{suffix}',
                     body=json.dumps(body) if body is not None else None,
                     headers={'Authorization': 'Bearer ' + (credential if credential is not None else secret_for(binding)),
                              'Content-Type': 'application/json', 'Accept': 'application/json',
                              **_lease_headers(suffix)})
        response = conn.getresponse()
        if response.status != 200:
            status = response.status
            getheader = getattr(response, 'getheader', None)  # Test doubles may omit headers.
            wait = _parse_retry_after(getheader('Retry-After') if getheader else None)
            if status == 503 and wait:
                _paused_until[origin] = time.monotonic() + wait
            if response.status in (401, 403):
                _recent_authorization.pop(_authorization_key(binding), None)
            if response.status == 409 and suffix in LEASE_SUFFIXES:
                refused = _tunnel_limit(response, wait)
                if refused:
                    logger.warning('Account hub refused a remote connection: tunnel limit %s of %s (licensed: %s)',
                                   refused.used, refused.limit, refused.licensed)
                    raise refused
            if suffix.startswith('browser-logins') and response.status in (401, 403):
                raise HTTPException(response.status, 'Account sign-in expired or authorization was rejected.')
            if suffix.startswith('account/') and response.status in (401, 403, 404, 409):
                messages = {
                    401: 'This worker is no longer linked to its account. Sign in again.',
                    403: 'Account revocation is not allowed for this worker.',
                    404: 'Registration unavailable, or the account service needs an update.',
                    409: 'Use Sign out to disconnect this worker.',
                }
                raise HTTPException(response.status, messages[response.status])
            if suffix == 'account' and response.status in ACCOUNT_PROFILE_ERRORS:
                # Fixed messages: the hub body is never echoed.
                message = ACCOUNT_PROFILE_ERRORS[response.status]
                if method == 'GET' and response.status == 429:
                    message = 'Account hub is rate limiting this worker. Try again shortly.'
                raise HTTPException(response.status, message)
            if response.status == 429:
                detail = 'Account hub is rate limiting this worker'
            raise ValueError(f'hub answered HTTP {response.status}')
        raw = response.read(max_bytes + 1)
        if len(raw) > max_bytes:
            raise ValueError('Oversize hub response')
        result = json.loads(raw)
        if not isinstance(result, dict):
            raise ValueError('Invalid hub response')
        if suffix == 'network/pending' or (suffix == 'status' and result.get('active') is True):
            # One worker binding, bounded cache, no identity/provider secrets.
            if len(_recent_authorization) >= 16:
                _recent_authorization.clear()
            _recent_authorization[_authorization_key(binding)] = time.monotonic()
        return result
    except HTTPException:
        raise
    except Exception as exc:
        # The reason is the exception class plus its own message (socket, TLS,
        # JSON, or the status line above); hub bodies and credentials never
        # appear in it.
        logger.warning('Account hub %s %s failed: %s: %s', method, suffix, type(exc).__name__, exc)
        if status == 503 and wait:
            detail = 'Account hub is paused'
        raise HubResponseError(detail, status, wait) from None
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
    with _VERDICT_LOCK:
        confirmed_at = _verdicts.get(key)
        if confirmed_at is not None and time.monotonic() - confirmed_at < settings.mobile_account_hub_verify_ttl_seconds:
            return binding
        generation = _verification_generation
        flight_key = (generation, _display_identity(binding), key)
        future = _verification_flights.get(flight_key)
        leader = future is None
        if leader:
            future = Future()
            _verification_flights[flight_key] = future
    try:
        if leader:
            try:
                result = call(binding, 'GET', suffix)
                if (result.get('active') is not True or result.get('account_id') != owner['account_id']
                        or result.get('device_id') != owner['hub_device_id']):
                    _forget_verdict(key)
                    raise HTTPException(status_code=401, detail='Account authorization revoked')
                if _display_identity(store.get_binding()) != _display_identity(binding):
                    raise HTTPException(status_code=401, detail='Account authorization revoked')
                with _VERDICT_LOCK:
                    if generation != _verification_generation:
                        raise HTTPException(status_code=401, detail='Account authorization revoked')
                    # One timestamp for the entire flight; waiters never extend
                    # the existing verification TTL or outage grace window.
                    _verdicts[key] = time.monotonic()
                    if len(_verdicts) > _VERDICT_CAP:
                        oldest = min(_verdicts, key=_verdicts.get)
                        _verdicts.pop(oldest, None)
                future.set_result(None)
            except BaseException as exc:  # noqa: BLE001 - release waiters even if the leader is interrupted
                future.set_exception(exc)
            finally:
                with _VERDICT_LOCK:
                    _verification_flights.pop(flight_key, None)
        future.result(timeout=30)
    except (HTTPException, TimeoutError) as exc:
        failure = exc if isinstance(exc, HTTPException) else HTTPException(status_code=503, detail='Account authorization unavailable')
        if generation != _verification_generation or _display_identity(store.get_binding()) != _display_identity(binding):
            raise HTTPException(status_code=401, detail='Account authorization revoked') from exc
        age = _verdict_age(key)
        if failure.status_code >= 500 and age is not None and age < settings.mobile_account_hub_grace_seconds:
            logger.warning('%s; keeping device %s authorized on a verdict %.0fs old',
                           failure.detail, owner['hub_device_id'], age)
            return binding
        raise failure
    if generation != _verification_generation or _display_identity(store.get_binding()) != _display_identity(binding):
        raise HTTPException(status_code=401, detail='Account authorization revoked')
    return binding


def mobile_hub_ownership(device):
    """Live-checked ownership dict or None for direct pairing. device is DB row."""
    owner = store.ownership(device['device_id'])
    require_worker_confirmation(owner)
    require_active(owner)
    return owner


def require_worker_confirmation(owner):
    if owner is not None:
        decision = store.confirmation(owner)
        if decision == 'denied':
            raise HTTPException(status_code=403, detail='This device was denied by the worker owner')
        if decision != 'approved':
            raise HTTPException(status_code=403, detail='Confirm this device on the worker before connecting')


def check_paired_client(client):
    """Validate the device live and derive this installation owner's authority.

    The hub may veto a device, but cannot approve one. Both a worker-local
    confirmation and a live managed binding are required. Direct/controller-linked
    connections do not delegate administration.
    """
    from datetime import datetime, timezone

    from app.core.device_keys import key_expiry
    from app.db.mobile import get_mobile_device

    owner = store.ownership(paired_client_id=client['id'])
    if owner is None:
        # Direct-paired phones cannot keep using a derived web/native session
        # after the originating key expires or its mobile record is removed.
        device = get_mobile_device(client.get('device_id'))
        if device:
            if (device.get('revoked_at') or device.get('paired_client_id') != client['id']
                    or key_expiry(device) <= datetime.now(timezone.utc)):
                raise HTTPException(status_code=401, detail='Mobile authorization expired or revoked')
        elif client.get('client_type') == 'mobile':
            raise HTTPException(status_code=401, detail='Mobile authorization revoked')
        return False
    require_worker_confirmation(owner)
    device = get_mobile_device(owner['device_id'])
    if (not device or device.get('revoked_at') or device.get('paired_client_id') != client['id']
            or owner['paired_client_id'] != client['id']
            or device.get('device_id') != owner['device_id']
            or owner['device_id'] != client.get('device_id')
            or not store.valid_device_identity(owner)):
        raise HTTPException(status_code=401, detail='Mobile authorization revoked')
    if key_expiry(device) <= datetime.now(timezone.utc):
        raise HTTPException(status_code=401, detail='Mobile authorization expired')
    binding = require_active(owner)
    from app.core.config import settings
    return (binding['linked_by'] == 'managed:' + settings.effective_instance_id
            and 'controller' in (client.get('scopes') or [])
            and 'web:session' in (device.get('scopes') or []))


def _authorization_key(binding):
    return tuple(binding.get(key) for key in ('hub_url', 'server_id', 'encrypted_secret'))


def recently_authorized(binding, *, max_age=300):
    import time
    return time.monotonic() - _recent_authorization.get(_authorization_key(binding), float('-inf')) < max_age
