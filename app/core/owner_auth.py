"""Single-owner browser authentication and explicitly local console recovery.

The account hub proves a browser belongs to this worker's owner. Console grants
prove installation possession; neither a username nor an arbitrary Clerk login
is evidence of ownership. Secrets are hashed or encrypted before persistence.
"""
import hashlib
import ipaddress
import json
import re
import secrets
from datetime import datetime, timedelta, timezone
from urllib.parse import urlsplit

from fastapi import HTTPException
from app.core import account_hub, encryption, pairing
from app.core.config import settings
from app.db import database, mobile_hub
from app.db.base import get_db

COOKIE = 'shuffle_account_login'
RECOVERY_PREFIX = 'shr_'


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


def now():
    return datetime.now(timezone.utc)


def create_schema(cursor):
    cursor.execute('''CREATE TABLE IF NOT EXISTS owner_console_grants (
        token_hash TEXT PRIMARY KEY, instance_id TEXT NOT NULL,
        purpose TEXT NOT NULL, expires_at TEXT NOT NULL)''')
    cursor.execute('''CREATE TABLE IF NOT EXISTS owner_local_sessions (
        token_hash TEXT PRIMARY KEY, instance_id TEXT NOT NULL,
        purpose TEXT NOT NULL, expires_at TEXT NOT NULL)''')
    cursor.execute('''CREATE TABLE IF NOT EXISTS owner_browser_requests (
        proof_hash TEXT PRIMARY KEY, instance_id TEXT NOT NULL,
        server_id TEXT NOT NULL, encrypted_request TEXT NOT NULL,
        expires_at TEXT NOT NULL)''')


def linked_binding():
    binding = mobile_hub.get_binding()
    if (binding and binding['active'] and
            binding['linked_by'] == 'managed:' + settings.effective_instance_id):
        return binding
    return None


def setup_required():
    # Local installation setup is independent of optional remote ownership.
    if database.get_system_setting('installation_configured') == 'true':
        return False
    if database.get_admin() or database.get_system_setting('onboarding_complete') == 'true':
        database.set_system_setting('installation_configured', 'true')
        return False
    return True


def configure_installation():
    require_key()
    preference_identity()
    database.set_system_setting('installation_configured', 'true')
    database.set_system_setting('onboarding_started', 'true')


def local_access(request):
    """Only the trusted local ASGI listener may authorize a LAN owner.

    Explicit integration credentials always retain their own restricted role.
    Header values can deny access, but cannot confer it. Host checks close DNS
    rebinding against a malicious public name resolving to a private address.
    """
    import socket
    if (not settings.shuffle_local_access
            or getattr(request, 'scope', {}).get('shuffle.ingress') != 'local'
            or request.headers.get('authorization')
            or any(h in request.headers for h in ('forwarded', 'x-forwarded-for',
                       'x-forwarded-host', 'x-forwarded-proto', 'cf-connecting-ip', 'cf-ray'))):
        return False
    networks = tuple(ipaddress.ip_network(n) for n in
                     ('127.0.0.0/8', '10.0.0.0/8', '172.16.0.0/12', '192.168.0.0/16', '::1/128', 'fc00::/7'))
    try:
        peer = ipaddress.ip_address(request.client.host)
        if not any(peer in network for network in networks):
            return False
        hostname = urlsplit('http://' + request.headers.get('host', '')).hostname
        if not hostname:
            return False
        try:
            address = ipaddress.ip_address(hostname)
            host_ok = any(address in network for network in networks)
        except ValueError:
            names = {'localhost', socket.gethostname().lower(), socket.gethostname().lower() + '.local'}
            names.update(x.strip().lower() for x in settings.shuffle_local_hostnames.split(',') if x.strip())
            host_ok = hostname.lower() in names
        if not host_ok or request.headers.get('sec-fetch-site') == 'cross-site':
            return False
        origin = request.headers.get('origin')
        if origin and urlsplit(origin).netloc != request.headers.get('host'):
            return False
        return True
    except (ValueError, AttributeError):
        return False


def preference_identity():
    """Keep the install's existing preference namespace across account rename.

    Preserve legacy history rather than copying mutable account names into keys.
    A fresh install uses its immutable installation identifier.
    """
    key = 'owner_preference_identity'
    with get_db() as conn:
        c = conn.cursor()
        c.execute('SELECT value FROM system_settings WHERE key=%s', (key,))
        row = c.fetchone()
        if row:
            return row['value']
        c.execute('SELECT username FROM admin WHERE id=1')
        admin = c.fetchone()
        identity = admin['username'] if admin else 'owner:' + settings.effective_instance_id
        c.execute('''INSERT INTO system_settings(key,value) VALUES(%s,%s)
            ON CONFLICT(key) DO NOTHING''', (key, identity))
        c.execute('SELECT value FROM system_settings WHERE key=%s', (key,))
        return c.fetchone()['value']


def require_key():
    if not encryption.is_encryption_ready():
        raise HTTPException(503, encryption.credential_storage_error())


def storage_status():
    reader = getattr(encryption, 'credential_key_status', None)
    result = reader() if reader else {'ready': encryption.is_encryption_ready(),
                                     'state': 'ready' if encryption.is_encryption_ready() else 'unavailable'}
    return {name: result[name] for name in ('ready', 'state')}


def issue_console_grant(purpose):
    """Only invoked by a local CLI, never by an HTTP endpoint."""
    if purpose != 'recovery':
        raise ValueError('Unknown console grant')
    token = secrets.token_urlsafe(32)
    expires = now() + timedelta(minutes=10)
    with get_db() as conn:
        c = conn.cursor()
        c.execute('DELETE FROM owner_console_grants WHERE instance_id=%s AND purpose=%s',
                  (settings.effective_instance_id, purpose))
        c.execute('INSERT INTO owner_console_grants VALUES(%s,%s,%s,%s)',
                  (digest(token), settings.effective_instance_id, purpose, expires.isoformat()))
    return token


def consume_console_grant(code, purpose):
    require_key()
    if not isinstance(code, str) or not re.fullmatch(r'[A-Za-z0-9_-]{43}', code):
        raise HTTPException(401, 'Local code is invalid or expired.')
    token = RECOVERY_PREFIX + secrets.token_urlsafe(32)
    expires = now() + timedelta(minutes=15)
    with get_db() as conn:
        c = conn.cursor()
        c.execute('''DELETE FROM owner_console_grants WHERE token_hash=%s
            AND instance_id=%s AND purpose=%s AND expires_at>%s RETURNING token_hash''',
                  (digest(code), settings.effective_instance_id, purpose, now().isoformat()))
        if not c.fetchone():
            raise HTTPException(401, 'Local code is invalid or expired.')
        c.execute('INSERT INTO owner_local_sessions VALUES(%s,%s,%s,%s)',
                  (digest(token), settings.effective_instance_id, purpose, expires.isoformat()))
    return token, expires


def local_session(token):
    if not token or not token.startswith(RECOVERY_PREFIX):
        return None
    with get_db() as conn:
        c = conn.cursor()
        c.execute('''SELECT * FROM owner_local_sessions WHERE token_hash=%s
            AND instance_id=%s AND expires_at>%s''',
                  (digest(token), settings.effective_instance_id, now().isoformat()))
        row = c.fetchone()
        return dict(row) if row else None


def revoke_local_session(token):
    with get_db() as conn:
        conn.cursor().execute('DELETE FROM owner_local_sessions WHERE token_hash=%s', (digest(token),))


def loopback_request(request):
    """Reject proxy headers too: a local tunnel connector is not a local operator."""
    if (getattr(request, 'scope', {}).get('shuffle.ingress') != 'local' or
            any(h in request.headers for h in ('forwarded', 'x-forwarded-for', 'x-forwarded-host',
                                               'x-forwarded-proto', 'cf-connecting-ip', 'cf-ray'))):
        return False
    try:
        host = urlsplit('http://' + request.headers.get('host', '')).hostname
        host_local = host == 'localhost'
        if not host_local:
            host_local = ipaddress.ip_address(host).is_loopback
        return host_local and ipaddress.ip_address(request.client.host).is_loopback
    except (ValueError, AttributeError):
        return False


def require_local_request(request):
    if not loopback_request(request):
        raise HTTPException(403, 'Recovery is available only through a direct local connection.')


def same_origin(request):
    """Login CSRF checks do not inherit wildcard CORS or bearer exemptions."""
    origin = request.headers.get('origin')
    if not origin:
        if loopback_request(request):
            return
        raise HTTPException(403, 'A same-origin browser request is required.')
    try:
        parsed = urlsplit(origin)
        host = request.headers.get('host', '')
        if (parsed.scheme not in ('http', 'https') or parsed.netloc != host or
                parsed.username or parsed.password or parsed.path or parsed.query or parsed.fragment):
            raise ValueError()
        # A trusted HTTPS reverse proxy may terminate TLS before the worker.
        if request.url.scheme == 'https' and parsed.scheme != 'https':
            raise ValueError()
    except ValueError:
        raise HTTPException(403, 'A same-origin browser request is required.') from None


def begin_browser(request_origin):
    require_key()
    binding = linked_binding()
    if not binding:
        raise HTTPException(409, 'Link this worker to its owner account first.')
    proof, poll_secret = secrets.token_urlsafe(32), secrets.token_hex(32)
    result = account_hub.call(binding, 'POST', 'browser-logins',
                             {'poll_challenge': digest(poll_secret), 'device_name': 'Web browser',
                              'browser_origin': request_origin})
    request_id, url = result.get('request_id'), result.get('authorization_url')
    if not isinstance(request_id, str) or not re.fullmatch(r'[A-Za-z0-9_-]{32,128}', request_id):
        raise HTTPException(503, 'Account sign-in could not be started.')
    try:
        target = urlsplit(url)
        # The authorization page may use the configured canonical browser host;
        # its origin must be explicitly pinned, never inferred from user input.
        trusted = urlsplit(browser_origin())
        if (target.scheme != 'https' or target.netloc != trusted.netloc or trusted.scheme != 'https'
                or target.username or target.password or target.fragment):
            raise ValueError()
        expiry = datetime.fromisoformat(result['expires_at'].replace('Z', '+00:00'))
        if not now() < expiry <= now() + timedelta(minutes=11):
            raise ValueError()
    except (TypeError, ValueError, KeyError):
        raise HTTPException(503, 'Account sign-in could not be started.') from None
    payload = encryption.encrypt_value(json.dumps({'request_id': request_id, 'poll_secret': poll_secret}))
    with get_db() as conn:
        c = conn.cursor()
        c.execute('DELETE FROM owner_browser_requests WHERE expires_at<=%s', (now().isoformat(),))
        c.execute('INSERT INTO owner_browser_requests VALUES(%s,%s,%s,%s,%s)',
                  (digest(proof), settings.effective_instance_id, binding['server_id'], payload, expiry.isoformat()))
    return proof, {'request_id': request_id, 'authorization_url': url, 'expires_at': expiry.isoformat()}


def browser_origin():
    value = settings.account_browser_origin.rstrip('/')
    parsed = urlsplit(value)
    if (parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password
            or parsed.path or parsed.query or parsed.fragment or parsed.port not in (None, 443)):
        raise HTTPException(503, 'Account browser origin is not configured correctly.')
    return value


def claim_url(code, screen=None):
    """The hub page that signs in (or, with ``screen='signup'``, creates) the owner account and links this worker."""
    if not isinstance(code, str) or not re.fullmatch(r'[A-Z2-7]{12}', code):
        return None
    if screen not in (None, 'signin', 'signup'):
        raise ValueError('Unknown sign-in screen')
    return browser_origin() + '/browser/claim?code=' + code + ('&screen=' + screen if screen else '')


def poll_browser(proof, completion_secret=None):
    require_key()
    if not proof or not re.fullmatch(r'[A-Za-z0-9_-]{43}', proof):
        raise HTTPException(401, 'Sign-in expired. Start again.')
    with get_db() as conn:
        c = conn.cursor()
        c.execute('''SELECT * FROM owner_browser_requests WHERE proof_hash=%s
            AND instance_id=%s AND expires_at>%s''',
                  (digest(proof), settings.effective_instance_id, now().isoformat()))
        pending = c.fetchone()
    binding = linked_binding()
    if not pending or not binding or binding['server_id'] != pending['server_id']:
        raise HTTPException(401, 'Sign-in expired. Start again.')
    if completion_secret is not None and not re.fullmatch(r'[A-Za-z0-9_-]{43}', completion_secret):
        raise HTTPException(400, 'Invalid sign-in completion code.')
    body = json.loads(encryption.decrypt_value(pending['encrypted_request']))
    if completion_secret is not None:
        body['completion_secret'] = completion_secret
    result = account_hub.call(binding, 'POST', 'browser-logins/poll', body)
    if result.get('status') == 'pending':
        return None
    # Status-only polling may report denial, but can never authorize a session.
    # Keep this local invariant even if an incompatible hub returns approved.
    if completion_secret is None or result.get('status') != 'approved':
        raise HTTPException(401, 'Account authorization rejected.')
    try:
        identity = {name: account_hub.identifier(result.get(name)) for name in ('account_id', 'device_id')}
        identity.update(server_id=binding['server_id'], device_name='Web browser')
        account_hub.require_active({'server_id': binding['server_id'], 'account_id': identity['account_id'],
                                    'hub_device_id': identity['device_id']})
    except ValueError:
        raise HTTPException(401, 'Account authorization rejected.') from None
    from app.core.models import DeviceCapabilities
    from types import SimpleNamespace
    token = pairing.generate_controller_session()
    expiry = now() + timedelta(days=7)
    # Browser devices reuse the same immutable owner/device proof and revocation
    # graph, but receive no mobile capabilities or usable mobile API credential.
    mobile_hub.enroll(identity, SimpleNamespace(platform='browser', capabilities=DeviceCapabilities()),
                      digest(secrets.token_hex(32)), expiry,
                      ['controller', 'web:session'], session_token_hash=pairing.hash_controller_session(token),
                      login_proof=digest(proof), scoped=True)
    preference_identity()
    return token, expiry


def main():
    import argparse
    parser = argparse.ArgumentParser(description='Issue a one-use local Shuffle recovery code.')
    parser.add_argument('action', choices=('recovery',))
    args = parser.parse_args()
    from app.db.base import init_pool, close_pool
    try:
        init_pool(min_conn=1, max_conn=2)
        database.init_database()
        code = issue_console_grant(args.action)
        print('One-use ' + args.action + ' code (expires in 10 minutes): ' + code)
        print('Open Shuffle /login via a direct localhost/SSH-forwarded connection to recover.')
    finally:
        close_pool()


if __name__ == '__main__':
    main()
