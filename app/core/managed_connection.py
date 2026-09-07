"""The local owner offers this worker; their signed-in account approves it.

All hub credentials are encrypted at rest. A connector starts only on the
install that offered the worker, and exposes the application on loopback.
"""
import asyncio
import hashlib
import http.client
import json
import os
import secrets
import shutil
import ssl
import threading
import time
import uuid
from urllib.parse import urlsplit
from fastapi import HTTPException
from app.core import account_hub
from app.core.config import settings
from app.core.encryption import encrypt_value, decrypt_value
from app.db import database, mobile_hub
from app.db.async_database import run_sync
from app.serve import REMOTE_PORT

_process = None
_state = 'disconnected'
_error = None
_url = None
_offer_lock = threading.Lock()


def _key():
    return 'mobile_managed_pending:' + settings.effective_instance_id


def _owner():
    return 'managed:' + settings.effective_instance_id


def _pending():
    raw = database.get_system_setting(_key())
    return json.loads(decrypt_value(raw)) if raw else None


def status():
    try:
        pending = _pending()
    except (RuntimeError, ValueError):
        return {'status': 'locked', 'error': 'Restore the server credential key to unlock this connection.',
                'url': None, 'code': None, 'expires_at': None}
    valid = pending and pending['expires'] > time.time()
    return {'status': _state, 'error': _error, 'url': _url,
            'code': pending['code'] if valid and _state != 'connected' else None,
            'expires_at': pending['expires'] if valid else None}


def begin(name):
    # Concurrent clicks must reuse the same durable offer and server identity.
    with _offer_lock:
        return _begin(name)


def _begin(name):
    global _state, _error
    name = name.strip()
    if not 1 <= len(name) <= 80 or any(ord(c) < 32 or ord(c) == 127 for c in name):
        raise HTTPException(400, 'Enter a worker name')
    if not shutil.which('cloudflared'):
        raise HTTPException(503, 'Install the current Shuffle build with automatic remote access support')
    binding = mobile_hub.get_binding()
    if binding:
        if binding['linked_by'] != _owner():
            raise HTTPException(409, 'This deployment is already linked. Manage its existing account connection.')
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
    mobile_hub.activate()
    database.set_system_setting(_key(), '')
    return token, url


def _check_authorized():
    binding = mobile_hub.get_binding()
    if not binding or binding['linked_by'] != _owner() or not binding['active']:
        raise ValueError('Worker unlinked')
    if account_hub.call(binding, 'GET', 'status').get('active') is not True:
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


async def supervise():
    global _process, _state, _error, _url
    try:
        while True:
            delay = 3
            try:
                if _process and _process.returncode is None:
                    binding = await run_sync(_check_authorized)
                    ready = await run_sync(_reachable, binding, _url)
                    _state, _error = ('connected' if ready else 'connecting'), None
                    delay = 30 if ready else 5
                else:
                    credentials = await run_sync(_credentials)
                    if credentials:
                        token, _url = credentials
                        executable = shutil.which('cloudflared')
                        if not executable:
                            raise ValueError('Connector unavailable')
                        # Keep all application/provider secrets out of the child environment.
                        child_env = {k:v for k,v in os.environ.items() if k in ('PATH','SystemRoot','SYSTEMROOT','TEMP','TMP')}
                        child_env.update(TUNNEL_TOKEN=token, TUNNEL_LOGLEVEL='error')
                        _process = await asyncio.create_subprocess_exec(executable,'tunnel','--no-autoupdate','run',
                            env=child_env,stdout=asyncio.subprocess.DEVNULL,stderr=asyncio.subprocess.DEVNULL)
                        _state, _error = 'connecting', None
                        delay = 3
            except asyncio.CancelledError:
                raise
            except Exception:
                await _stop()
                _state, _error = 'reconnecting', 'The account connection is unavailable. Shuffle will retry.'
                delay = 15
            await asyncio.sleep(delay)
    finally:
        await _stop()
        _state = 'disconnected'
