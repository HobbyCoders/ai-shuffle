"""Single-owner access to the local listener.

Who is the owner of an install:

- A browser that signed in with the owner password (a session cookie, or the
  same token as a Bearer). The password is created in the first-run wizard,
  or seeded once from ``OWNER_PASSWORD``, and stored argon2id-hashed in the
  settings store.
- A request from loopback on the local listener. When the desktop app started
  this worker with a listener token (``app.core.desktop_listener``), loopback
  also has to carry that token.
- Any direct private-network request, but only when the owner turned on
  "trusted LAN, no login" (off by default).
- Paired devices and hub-enrolled phones keep their own device keys and roles;
  explicit integration credentials keep their restricted roles.

Before any password exists, a direct local-network browser may create one and
nothing else (``bootstrap_access``), so a fresh install and an upgraded
install whose owner used to rely on LAN trust can both reach the setup step
without anyone being locked out. Secrets are hashed before persistence.
"""
import base64
import hashlib
import ipaddress
import logging
import re
import secrets
from datetime import datetime, timedelta, timezone
from urllib.parse import urlsplit

from fastapi import HTTPException
from app.core import desktop_listener, encryption
from app.core.config import settings
from app.db import database, mobile_hub
from app.db.base import get_db

logger = logging.getLogger(__name__)

RECOVERY_PREFIX = 'shr_'
SESSION_PREFIX = RECOVERY_PREFIX

# Settings-store keys. All are bound to this worker (see
# ``app.network.policy``): a password or LAN trust never syncs to other workers.
PASSWORD_KEY = 'owner_auth_password'
TRUSTED_LAN_KEY = 'owner_auth_trusted_lan'
ENV_SEEDED_KEY = 'owner_auth_env_seeded'
PASSWORD_ENV = 'OWNER_PASSWORD'

PASSWORD_MIN_LENGTH = 12
PASSWORD_MAX_LENGTH = 256
SESSION_DAYS = 30
# A browser opened from the desktop app's "Open in Browser".
HANDOFF_SESSION_DAYS = 7

# argon2id parameters for new hashes (RFC 9106's second recommended option).
# Verification reads the parameters stored with each hash.
_ARGON2_MEMORY_KIB = 65536
_ARGON2_ITERATIONS = 3
_ARGON2_LANES = 4
_ARGON2_LENGTH = 32
_PHC = re.compile(r'\$argon2id\$v=19\$m=(\d+),t=(\d+),p=(\d+)\$([A-Za-z0-9+/]+)\$([A-Za-z0-9+/]+)')

#: Headers whose mere presence marks a request as proxied. Shared with
#: ``app.core.preview_access`` so the preview door cannot be looser than this.
PROXY_HEADERS = ('forwarded', 'x-forwarded-for', 'x-forwarded-host',
                 'x-forwarded-proto', 'cf-connecting-ip', 'cf-ray')

# Loopback, RFC1918, Tailscale CGNAT and ULA: the owner's own network.
# Tailscale addresses (CGNAT 100.64.0.0/10) are part of the owner's own
# network: the worker is reached directly, never through the internet.
_PRIVATE_NETWORKS = tuple(ipaddress.ip_network(n) for n in
                          ('127.0.0.0/8', '10.0.0.0/8', '172.16.0.0/12', '192.168.0.0/16',
                           '100.64.0.0/10', '::1/128', 'fc00::/7'))


def is_private_address(address):
    """Whether an ``ipaddress`` object lies on the owner's own network."""
    return any(address in network for network in _PRIVATE_NETWORKS)


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
    """Passwordless owner access: loopback, or the LAN when the owner trusts it.

    Explicit integration credentials always retain their own restricted role.
    Header values can deny access, but cannot confer it.
    """
    if not settings.shuffle_local_access or not direct_local(request):
        return False
    return loopback_request(request) or trusted_lan()


def bootstrap_access(request):
    """A direct local-network browser may create the first owner password.

    Only while no password exists. It grants nothing else: the password route
    is the only caller.
    """
    return bool(settings.shuffle_local_access and direct_local(request) and not password_set(exact=True))


def direct_local(request):
    """A request that reached the local listener directly from a private address.

    Only the trusted local ASGI listener qualifies. Host checks close DNS
    rebinding against a malicious public name resolving to a private address.
    """
    import socket
    if (getattr(request, 'scope', {}).get('shuffle.ingress') != 'local'
            or request.headers.get('authorization')
            or any(h in request.headers for h in PROXY_HEADERS)):
        return False
    try:
        peer = ipaddress.ip_address(request.client.host)
        if not is_private_address(peer):
            return False
        hostname = urlsplit('http://' + request.headers.get('host', '')).hostname
        if not hostname:
            return False
        try:
            address = ipaddress.ip_address(hostname)
            host_ok = is_private_address(address)
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
    if not token or not isinstance(token, str) or not token.startswith(RECOVERY_PREFIX):
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


def owner_session(request, token):
    """A signed-in owner browser (or Bearer) on the local listener.

    Owner sessions never count on the protected remote listener: remote
    browsers and phones use their own device authorization there.
    """
    if getattr(request, 'scope', {}).get('shuffle.ingress') != 'local':
        return False
    return database.get_auth_session(token) is not None


def issue_session(purpose='password', days=SESSION_DAYS):
    """A new owner session token; only its hash is stored."""
    token = SESSION_PREFIX + secrets.token_urlsafe(32)
    expires = now() + timedelta(days=days)
    with get_db() as conn:
        conn.cursor().execute('''INSERT INTO owner_local_sessions(token_hash, instance_id, purpose, expires_at)
            VALUES(%s,%s,%s,%s)''', (digest(token), settings.effective_instance_id, purpose, expires.isoformat()))
    return token, expires


def revoke_all_sessions():
    """Sign out every owner browser of this worker (password change or reset)."""
    with get_db() as conn:
        c = conn.cursor()
        c.execute('DELETE FROM owner_local_sessions WHERE instance_id=%s', (settings.effective_instance_id,))
        return c.rowcount


# ── Owner password ──────────────────────────────────────────────────────────

def _b64(data):
    return base64.b64encode(data).decode().rstrip('=')


def _unb64(text):
    return base64.b64decode(text + '=' * (-len(text) % 4))


def _argon2(password, salt, memory, iterations, lanes, length):
    from cryptography.hazmat.primitives.kdf.argon2 import Argon2id
    return Argon2id(salt=salt, length=length, iterations=iterations, lanes=lanes,
                    memory_cost=memory).derive(password.encode('utf-8'))


def hash_password(password):
    """An argon2id hash in the PHC string format."""
    salt = secrets.token_bytes(16)
    key = _argon2(password, salt, _ARGON2_MEMORY_KIB, _ARGON2_ITERATIONS, _ARGON2_LANES, _ARGON2_LENGTH)
    return (f'$argon2id$v=19$m={_ARGON2_MEMORY_KIB},t={_ARGON2_ITERATIONS},p={_ARGON2_LANES}'
            f'${_b64(salt)}${_b64(key)}')


def verify_hash(password, encoded):
    if not isinstance(password, str) or not isinstance(encoded, str):
        return False
    match = _PHC.fullmatch(encoded)
    if not match:
        return False
    try:
        memory, iterations, lanes = (int(match.group(i)) for i in (1, 2, 3))
        salt, expected = _unb64(match.group(4)), _unb64(match.group(5))
        if not (8 <= memory <= 4 * 1024 * 1024 and 1 <= iterations <= 16 and 1 <= lanes <= 64):
            return False
        actual = _argon2(password, salt, memory, iterations, lanes, len(expected))
    except Exception:
        return False
    return secrets.compare_digest(actual, expected)


def validate_new_password(password):
    if not isinstance(password, str) or len(password) < PASSWORD_MIN_LENGTH:
        raise ValueError(f'Use at least {PASSWORD_MIN_LENGTH} characters.')
    if len(password) > PASSWORD_MAX_LENGTH:
        raise ValueError(f'Use at most {PASSWORD_MAX_LENGTH} characters.')
    if not password.strip():
        raise ValueError('The password cannot be only spaces.')


def password_set(exact=False):
    """Whether this worker has an owner password. Fails closed to True on a
    database error, so an outage never reopens the first-run setup step."""
    try:
        reader = database.get_system_setting if exact else database.get_system_setting_cached
        return bool(reader(PASSWORD_KEY))
    except Exception:
        logger.warning('Could not read the owner password state', exc_info=True)
        return True


def set_password(password, *, source):
    """Store a new owner password and sign every owner browser out."""
    validate_new_password(password)
    database.set_system_setting(PASSWORD_KEY, hash_password(password))
    revoke_all_sessions()
    logger.info('Owner password set (%s)', source)


def verify_password(password):
    encoded = database.get_system_setting(PASSWORD_KEY)
    if not encoded:
        # Same work as a real check, so timing does not reveal the state.
        verify_hash(password, _dummy_hash())
        return False
    return verify_hash(password, encoded)


_dummy = None


def _dummy_hash():
    global _dummy
    if _dummy is None:
        _dummy = hash_password(secrets.token_urlsafe(16))
    return _dummy


def clear_password():
    """Remove the owner password (container-local reset) and sign everyone out."""
    removed = database.delete_system_setting(PASSWORD_KEY)
    revoke_all_sessions()
    return removed


def trusted_lan():
    """The "trusted LAN, no login" toggle. Off unless the owner turned it on;
    a database error reads as off."""
    try:
        return database.get_system_setting_cached(TRUSTED_LAN_KEY) == 'true'
    except Exception:
        logger.warning('Could not read the trusted LAN setting', exc_info=True)
        return False


def set_trusted_lan(enabled):
    if enabled:
        database.set_system_setting(TRUSTED_LAN_KEY, 'true')
    else:
        database.delete_system_setting(TRUSTED_LAN_KEY)


def seed_password_from_env(environ=None):
    """``OWNER_PASSWORD`` seeds the owner password on first boot only.

    Once seeded (or once a password exists some other way), the variable is
    ignored for good: the wizard, Settings and the reset command change the
    password afterwards. The value is always removed from this process's
    environment so agents never inherit it. Returns what happened.
    """
    import os
    environ = os.environ if environ is None else environ
    value = environ.pop(PASSWORD_ENV, None)
    if not value:
        return 'absent'
    if database.get_system_setting(ENV_SEEDED_KEY) == 'true':
        return 'ignored'
    if database.get_system_setting(PASSWORD_KEY):
        database.set_system_setting(ENV_SEEDED_KEY, 'true')
        return 'ignored'
    try:
        validate_new_password(value)
    except ValueError as exc:
        logger.error('%s was not used: %s Set the password in the setup wizard instead.', PASSWORD_ENV, exc)
        return 'invalid'
    set_password(value, source='environment')
    database.set_system_setting(ENV_SEEDED_KEY, 'true')
    return 'seeded'


def status(request):
    """What the web UI needs to pick between sign-in, first-run setup and
    the app. Never reveals anything beyond whether a password exists."""
    has_password = password_set()
    local = getattr(request, 'scope', {}).get('shuffle.ingress') == 'local'
    lan = trusted_lan()
    return {
        'password_set': has_password,
        # A browser here may create the first password.
        'setup_allowed': (not has_password) and local and bootstrap_access(request),
        # The blocking "set an owner password" step applies. Desktop (local
        # mode) installs only listen on loopback, where the password is optional.
        'required': (not has_password) and not lan and not settings.is_local_mode(),
        'trusted_lan': lan,
        'login_available': has_password and local,
        'desktop_handoff': desktop_listener.configured(),
        'min_length': PASSWORD_MIN_LENGTH,
    }


def listener_ok(request):
    """True unless the desktop started this worker with a listener token and
    the request does not carry it."""
    if not desktop_listener.configured():
        return True
    return desktop_listener.matches(request.headers.get(desktop_listener.HEADER))


def loopback_request(request):
    """Reject proxy headers too: a local tunnel connector is not a local operator."""
    if (getattr(request, 'scope', {}).get('shuffle.ingress') != 'local' or
            any(h in request.headers for h in PROXY_HEADERS) or not listener_ok(request)):
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
    """Owner CSRF checks do not inherit wildcard CORS or bearer exemptions.

    A request with no Origin is accepted only from the desktop app's own
    process: loopback carrying the per-launch listener token, which is a
    credential no web page can attach. Plain loopback is not enough, because
    any local program (or a page that makes a browser omit Origin) reaches it.
    """
    origin = request.headers.get('origin')
    if not origin:
        if desktop_listener.configured() and loopback_request(request):
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
