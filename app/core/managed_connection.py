"""The local owner offers this worker; their signed-in account approves it.

All hub credentials are encrypted at rest. A connector starts only on the
install that offered the worker, and exposes the application on loopback.
"""
import asyncio
import hashlib
import http.client
import json
import os
import random
import secrets
import ssl
import threading
import time
import uuid
from urllib.parse import urlsplit
from fastapi import HTTPException
from app.core import account_hub, cloudflared, encryption
from app.core.config import settings
from app.core.encryption import encrypt_value, decrypt_value
from app.db import database, mobile_hub
from app.db.async_database import run_sync
from app.serve import REMOTE_PORT

_process = None
_state = 'disconnected'
_error = None
_url = None
_offer_lock = threading.RLock()
_generation = 0
# The hub's last tunnel_limit refusal ({limit, used, licensed}), shown in the
# remote-access UI until a connector starts or the worker signs out.
_refusal = None
# Bumped by retry_soon() (a license was just activated): cuts the hub backoff short.
_wake = 0

# Hub failures back off exponentially with jitter, from 15 s up to 10 minutes,
# and never retry sooner than the hub's Retry-After (a kill switch sends an
# hour). A running connector survives hub 5xx, 429 and network errors; only an
# explicit 401/403, or the local binding going away, stops it.
BACKOFF_BASE_SECONDS = 15.0
BACKOFF_CAP_SECONDS = 600.0
REVOKED_STATUSES = (401, 403)


def _backoff(failures, retry_after=None):
    ceiling = min(BACKOFF_CAP_SECONDS, BACKOFF_BASE_SECONDS * 2 ** max(0, failures - 1))
    delay = random.uniform(ceiling / 2, ceiling)
    if retry_after:
        delay = max(delay, retry_after)
    return delay


def _hub_failure(exc):
    """A hub call that failed for a reason other than revocation."""
    return isinstance(exc, HTTPException) and account_hub.hub_status(exc) not in REVOKED_STATUSES


def _tunnel_limit_message(refused):
    if refused.licensed:
        return (f'Remote access is off: this account already uses all {refused.limit} remote connections '
                'its license covers. Turn off remote access on another worker to free one.')
    one = 'its one remote connection' if refused.limit == 1 else f'its {refused.limit} remote connections'
    return (f'Remote access is off: this account already uses {one}. Remote access for more workers '
            'needs an AI Shuffle license (Settings > System > License), or turn off remote access on another worker. '
            'Local use is not affected.')


def _hub_error_message(exc):
    refused = account_hub.tunnel_limit(exc)
    if refused:
        return _tunnel_limit_message(refused)
    if account_hub.hub_status(exc) == 503 and account_hub.retry_after(exc):
        return 'The account service is paused. Remote access keeps working; Shuffle will retry later.'
    if account_hub.hub_status(exc) == 429:
        return 'The account service is busy. Shuffle will retry shortly.'
    return 'The account connection is unavailable. Shuffle will retry.'


def _key():
    return 'mobile_managed_pending:' + settings.effective_instance_id


def _owner():
    return 'managed:' + settings.effective_instance_id


def _pending():
    raw = database.get_system_setting(_key())
    return json.loads(decrypt_value(raw)) if raw else None


def status():
    binding = mobile_hub.get_binding()
    common = {'signed_in': bool(binding), 'profile_ready': bool(binding and binding['active']),
              'server_id': binding['server_id'] if binding else None}
    if not encryption.is_encryption_ready():
        return {**common, 'status': 'locked', 'error': encryption.credential_storage_error(),
                'url': None, 'code': None, 'expires_at': None, 'refusal': None}
    if not cloudflared.find_executable():
        return {**common, 'status': 'unavailable', 'error': _missing_connector_message(),
                'url': None, 'code': None, 'expires_at': None, 'refusal': None}
    try:
        pending = _pending()
    except (RuntimeError, ValueError):
        return {**common, 'status': 'locked', 'error': 'Restore the server credential key to unlock this connection.',
                'url': None, 'code': None, 'expires_at': None, 'refusal': None}
    valid = pending and pending['expires'] > time.time()
    state = _state if binding or valid else 'disconnected'
    if binding and (state == 'disconnected' or not binding['active']):
        state = 'connecting'
    if valid and not binding:
        state = 'waiting'
    refusal = _refusal if binding else None
    if refusal and state != 'connected':
        state = 'limited'
    return {**common, 'status': state, 'error': _error, 'url': _url,
            'code': pending['code'] if valid and _state != 'connected' else None,
            'expires_at': pending['expires'] if valid else None,
            'refusal': dict(refusal) if refusal else None}


def _set_refusal(exc):
    global _refusal
    refused = account_hub.tunnel_limit(exc) if exc is not None else None
    _refusal = ({'code': 'tunnel_limit', 'limit': refused.limit, 'used': refused.used,
                 'licensed': refused.licensed, 'message': _tunnel_limit_message(refused)}
                if refused else None)


def retry_soon():
    """Ask the hub again within seconds instead of after the backoff.

    Called when a license was activated: an account the hub refused a tunnel
    (tunnel_limit) may now be allowed more.
    """
    global _wake
    _wake += 1


def begin(name):
    # Concurrent clicks must reuse the same durable offer and server identity.
    with _offer_lock:
        return _begin(name)


def _missing_connector_message():
    return ('Remote access needs cloudflared. Run python -m app.core.cloudflared '
            'in the Shuffle folder, then try again.')


def _begin(name):
    global _state, _error
    name = name.strip()
    if not 1 <= len(name) <= 80 or any(ord(c) < 32 or ord(c) == 127 for c in name):
        raise HTTPException(400, 'Enter a worker name')
    if not encryption.is_encryption_ready():
        raise HTTPException(503, encryption.credential_storage_error())
    if not cloudflared.find_executable():
        raise HTTPException(503, _missing_connector_message())
    binding = mobile_hub.get_binding()
    if binding:
        if binding['linked_by'] != _owner():
            raise HTTPException(409, 'Sign out of this worker’s existing account connection before linking it again.')
        return status()
    pending = _pending()
    if not pending or pending['expires'] <= time.time():
        pending = {'id': str(uuid.uuid4()), 'name': name, 'secret': secrets.token_hex(32),
                   'code': ''.join(secrets.choice('ABCDEFGHIJKLMNOPQRSTUVWXYZ234567') for _ in range(12)),
                   'expires': time.time() + 600}
        # Save before contacting the hub so retries recover the same offer.
        database.set_system_setting(_key(), encrypt_value(json.dumps(pending)))
    account_hub.worker_link_call('start', {'id': pending['id'], 'name': pending['name'],
        'secret_hash': hashlib.sha256(pending['secret'].encode()).hexdigest(),
        'code_hash': hashlib.sha256(pending['code'].encode()).hexdigest()})
    _state, _error = 'waiting', None
    return status()


def _protected_listener_ready():
    """Do not change a live tunnel until the authenticated origin is serving."""
    connection = http.client.HTTPConnection('127.0.0.1', REMOTE_PORT, timeout=2)
    try:
        connection.request('GET', '/api/v1/mobile/hub/ingress', headers={'Accept': 'application/json'})
        response = connection.getresponse()
        raw = response.read(1025)
        if response.status != 200 or len(raw) > 1024:
            return False
        result = json.loads(raw)
        return (result.get('protected_ingress') is True
                and result.get('instance_id') == settings.effective_instance_id)
    except Exception:
        return False
    finally:
        connection.close()


def _credentials():
    """Called in the DB worker thread. Never returns a connector token to an API."""
    with _offer_lock:
        return _load_credentials()


def _load_credentials():
    binding = mobile_hub.get_binding()
    if not binding:
        pending = _pending()
        if not pending or pending['expires'] <= time.time():
            return None
        result = account_hub.worker_link_call('poll', {'id': pending['id'], 'secret': pending['secret']})
        if result.get('status') == 'pending':
            return None
        if result.get('status') != 'approved' or result.get('server_id') != pending['id']:
            raise ValueError('Invalid worker approval')
        binding = mobile_hub.bind(account_hub.configured_origin(), pending['id'],
                                  encrypt_value(pending['secret']), _owner())
        if (binding['server_id'] != pending['id'] or binding['linked_by'] != _owner()
                or binding['hub_url'] != account_hub.configured_origin()
                or not secrets.compare_digest(decrypt_value(binding['encrypted_secret']), pending['secret'])):
            raise ValueError('Worker already bound to another authorization')
    if binding['linked_by'] != _owner():
        return None
    if not _protected_listener_ready():
        raise ValueError('Protected remote listener is not ready')
    result = account_hub.call(binding, 'POST',
                              'activate' if not binding['active'] else 'connector', {'remote_port': REMOTE_PORT})
    if result.get('remote_port') != REMOTE_PORT:
        raise ValueError('Hub has not verified the protected remote listener')
    token, url = result.get('tunnel_token'), result.get('url')
    if not isinstance(token, str) or not 32 <= len(token) <= 4096 or any(c.isspace() for c in token):
        raise ValueError('Invalid connector credential')
    parsed = urlsplit(url or '')
    if (parsed.scheme != 'https' or not parsed.hostname or parsed.port not in (None, 443)
            or parsed.username or parsed.password or parsed.query or parsed.fragment or parsed.path not in ('','/')):
        raise ValueError('Invalid connector address')
    mobile_hub.activate(binding['server_id'])
    database.set_system_setting(_key(), '')
    return token, url


def _disconnect_local():
    global _state, _error, _url, _generation
    with _offer_lock:
        binding = mobile_hub.get_binding()
        credential = None
        try:
            if binding:
                credential = account_hub.secret_for(binding)
            else:
                pending = _pending()
                if pending:
                    binding = {'hub_url': account_hub.configured_origin(), 'server_id': pending['id']}
                    credential = pending['secret']
        except Exception:
            # Local sign-out must work even if key storage or the hub is down.
            pass
        mobile_hub.disconnect()
        _generation += 1
        _state, _error, _url = 'disconnected', None, None
        _set_refusal(None)
        return binding, credential, _generation


async def disconnect():
    global _error
    binding, credential, generation = await run_sync(_disconnect_local)
    await _stop()
    error = None
    if binding:
        try:
            if not credential:
                raise ValueError('Credential unavailable')
            result = await run_sync(lambda: account_hub.call(binding, 'DELETE', 'connection', credential=credential))
            if result.get('ok') is not True:
                raise ValueError('Sign-out unavailable')
        except Exception:
            error = ('Signed out on this worker. The account service could not remove its listing; '
                     'remove this worker from your Shuffle account when the service is available.')
    if generation == _generation:
        _error = error
    return status()


def _check_authorized():
    binding = mobile_hub.get_binding()
    if not binding or binding['linked_by'] != _owner() or not binding['active']:
        raise ValueError('Worker unlinked')
    if not account_hub.recently_authorized(binding) and account_hub.call(binding, 'GET', 'status').get('active') is not True:
        raise ValueError('Worker revoked')
    return binding


def _reachable(binding, url):
    """Verify the assigned public route reaches this binding; send no credentials."""
    conn = None
    try:
        conn = account_hub._PinnedHTTPS(urlsplit(url).hostname, timeout=4, context=ssl.create_default_context())
        conn.request('GET', '/api/v1/mobile/hub/endpoint', headers={'Accept': 'application/json'})
        response = conn.getresponse()
        raw = response.read(1025)
        if response.status != 200 or len(raw) > 1024:
            return False
        result = json.loads(raw)
        return (result.get('ready') is True and result.get('server_id') == binding['server_id']
                and result.get('protected_ingress') is True)
    except Exception:
        return False
    finally:
        if conn:
            conn.close()


async def _stop():
    global _process
    process, _process = _process, None
    if process and process.returncode is None:
        process.terminate()
        try:
            await asyncio.wait_for(process.wait(), timeout=5)
        except asyncio.TimeoutError:
            process.kill()
            await process.wait()


def _linked_binding():
    binding = mobile_hub.get_binding()
    if not binding or binding['linked_by'] != _owner() or not binding['active']:
        raise ValueError('Worker unlinked')
    return binding


async def _sleep(delay, generation):
    """Sleep, waking early when the worker signs out (so a relink is not
    delayed) or a license is activated (retry_soon)."""
    if delay <= 30:
        await asyncio.sleep(delay)
        return
    end = time.monotonic() + delay
    wake = _wake
    while generation == _generation and wake == _wake:
        remaining = end - time.monotonic()
        if remaining <= 0:
            return
        await asyncio.sleep(min(remaining, 15))


async def supervise():
    global _process, _state, _error, _url
    unreachable_since = None
    failures = 0
    hub_retry_at = 0.0
    hub_error = None
    backoff_generation = _generation
    seen_wake = _wake
    try:
        while True:
            delay = 3
            generation = _generation
            if generation != backoff_generation:
                # Signed out or relinked: the old binding's backoff is moot.
                failures, hub_retry_at, hub_error, backoff_generation = 0, 0.0, None, generation
            if _wake != seen_wake:
                # A license was just activated: a refused tunnel may be allowed now.
                seen_wake = _wake
                failures, hub_retry_at = 0, 0.0
            try:
                if _process and _process.returncode is None:
                    if time.monotonic() >= hub_retry_at:
                        try:
                            binding = await run_sync(_check_authorized)
                            failures, hub_retry_at, hub_error = 0, 0.0, None
                        except HTTPException as exc:
                            if not _hub_failure(exc):
                                raise ValueError('Worker revoked') from None
                            # The hub is down, slow or rate limiting: the
                            # connector and its token are still good.
                            failures += 1
                            hub_retry_at = time.monotonic() + _backoff(failures, account_hub.retry_after(exc))
                            hub_error = _hub_error_message(exc)
                            binding = await run_sync(_linked_binding)
                    else:
                        binding = await run_sync(_linked_binding)
                    ready = await run_sync(_reachable, binding, _url)
                    _state, _error = ('connected' if ready else 'connecting'), hub_error
                    delay = 30 if ready else 5
                    if ready:
                        unreachable_since = None
                    elif unreachable_since is None:
                        unreachable_since = time.monotonic()
                    elif time.monotonic() - unreachable_since >= 60 and time.monotonic() >= hub_retry_at:
                        # cloudflared can stay alive while its route is stuck.
                        # Refresh the connector for the SAME saved binding.
                        await _stop()
                        unreachable_since = None
                        _state, _error = 'reconnecting', 'The cloud route is not responding. Restarting the connection.'
                        delay = 3
                elif time.monotonic() < hub_retry_at:
                    # The connector stopped while the hub is backing us off;
                    # wait for the backoff before asking for a new token.
                    delay = hub_retry_at - time.monotonic()
                else:
                    credentials = await run_sync(_credentials)
                    if credentials and generation == _generation:
                        token, _url = credentials
                        executable = cloudflared.find_executable()
                        if not executable:
                            raise ValueError('Connector unavailable')
                        # Keep all application/provider secrets out of the child environment.
                        child_env = {k:v for k,v in os.environ.items() if k in ('PATH','SystemRoot','SYSTEMROOT','TEMP','TMP')}
                        child_env.update(TUNNEL_TOKEN=token, TUNNEL_LOGLEVEL='error')
                        _process = await asyncio.create_subprocess_exec(executable,'tunnel','--no-autoupdate','run',
                            env=child_env,stdout=asyncio.subprocess.DEVNULL,stderr=asyncio.subprocess.DEVNULL)
                        if generation != _generation:
                            await _stop()
                            continue
                        unreachable_since = time.monotonic()
                        failures, hub_retry_at, hub_error = 0, 0.0, None
                        _state, _error = 'connecting', None
                        _set_refusal(None)
                        delay = 3
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                await _stop()
                _state, _error = 'reconnecting', 'The account connection is unavailable. Shuffle will retry.'
                delay = 15
                if isinstance(exc, HTTPException):
                    # Activation or connector refresh failed at the hub
                    # (including 401/403): back off instead of every 15 s.
                    failures += 1
                    delay = _backoff(failures, account_hub.retry_after(exc))
                    hub_retry_at = time.monotonic() + delay
                    _error = _hub_error_message(exc)
                    # The hub refused a tunnel (account at its cap): the
                    # worker keeps running locally; the UI says why.
                    _set_refusal(exc)
                else:
                    failures = 0
            await _sleep(delay, generation)
    finally:
        await _stop()
        _state = 'disconnected'
