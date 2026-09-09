"""Single-owner local access for the trusted LAN listener.

Shuffle is not exposed to the internet and never asks its owner to sign in:
direct requests on the local listener are the owner. Explicit integration
credentials and paired devices keep their own restricted roles. Secrets are
hashed before persistence.
"""
import hashlib
import ipaddress
import re
from datetime import datetime, timezone
from urllib.parse import urlsplit

from fastapi import HTTPException
from app.core import encryption
from app.core.config import settings
from app.db import database, mobile_hub
from app.db.base import get_db

RECOVERY_PREFIX = 'shr_'


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


def now():
    return datetime.now(timezone.utc)


def create_schema(cursor):
    cursor.execute('''CREATE TABLE IF NOT EXISTS owner_local_sessions (
        token_hash TEXT PRIMARY KEY, instance_id TEXT NOT NULL,
        purpose TEXT NOT NULL, expires_at TEXT NOT NULL)''')
    # The browser sign-in and console recovery login flows are gone; their
    # short-lived request stores hold nothing an upgraded install still needs.
    cursor.execute('DROP TABLE IF EXISTS owner_console_grants')
    cursor.execute('DROP TABLE IF EXISTS owner_browser_requests')


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
    # Tailscale addresses (CGNAT 100.64.0.0/10) are part of the owner's own
    # network: the worker is reached directly, never through the internet.
    networks = tuple(ipaddress.ip_network(n) for n in
                     ('127.0.0.0/8', '10.0.0.0/8', '172.16.0.0/12', '192.168.0.0/16', '100.64.0.0/10',
                      '::1/128', 'fc00::/7'))
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
            # Tailscale MagicDNS names only ever resolve to tailnet addresses.
            host_ok = hostname.lower() in names or hostname.lower().endswith('.ts.net')
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


def same_origin(request):
    """Owner CSRF checks do not inherit wildcard CORS or bearer exemptions."""
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
