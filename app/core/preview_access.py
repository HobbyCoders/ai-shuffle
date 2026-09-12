"""Session-scoped preview access for the agent browser.

Why this exists. The worker's UI sits behind
:func:`app.core.owner_auth.local_access`, which admits only a direct LAN owner
and rejects anything that looks proxied — including the branch's own Vite dev
server proxying ``/api`` to the worker, and the agent browser when it runs on
another host (the container's Chromium, a shared runner). Nine audited
sessions hit "only accepts direct connections from its local network" /
``local_access: false`` when they opened the worker's UI to verify a change,
and five then built their own preview servers. This module is the narrow door.

Threat model, and what the token is NOT:

* The token is minted by the harness for ONE app session and carries that
  session id; it is never accepted for a session that does not exist. It
  expires (``DEFAULT_TTL``), and nothing renews it silently.
* It is an HMAC over ``session_id.expires`` under a per-install key kept in
  the data dir (``preview-access.key``, created 0600). Any process sharing the
  data dir — the running worker, or an isolated ``AI_SHUFFLE_PREVIEW=1``
  instance started from a branch — verifies it statelessly; no DB row, no
  in-memory table that a second process cannot see.
* It is honoured ONLY on the local ingress listener and ONLY from a private
  network peer (the same address families ``local_access`` trusts). Unlike
  ``local_access`` it tolerates ``X-Forwarded-*`` headers: the audited
  failures were the branch's Vite dev server proxying ``/api`` to the worker
  (it sets ``x-forwarded-host``), and here the TOKEN is the credential, not
  the transport. A public tunnel still cannot present it — it terminates on
  the remote listener, which refuses the cookie outright.
* It reaches the request as an HttpOnly cookie set by one redirect endpoint
  (``/api/v1/auth/preview-access``). The query-string form is accepted there
  and nowhere else, so the secret is not sprayed into every referer and log.
* It grants the UI what the local owner has for reading and for driving a
  chat session, and NOTHING that changes who owns the install or what it can
  reach: every unsafe-method request under :data:`MUTATION_DENY_PREFIXES`
  (auth, pairing, credentials, settings, network, plugins, ...) is refused
  even with a valid cookie. Cross-site mutations are refused by the same
  ``same_origin`` check the local owner is held to.
* The URL that carries the token lands in the session's system prompt (the
  "# Workspace facts" card) and therefore in transcripts. That is accepted:
  the token is useless off the LAN, dies with the session, and can do nothing
  the model could not already do through its tools on that worker.
"""
from __future__ import annotations

import base64
import hmac
import ipaddress
import logging
import os
import secrets
import time
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from typing import Optional
from urllib.parse import quote, urlsplit

logger = logging.getLogger(__name__)

COOKIE_NAME = "ais_preview"
QUERY_PARAM = "token"
REDIRECT_PATH = "/api/v1/auth/preview-access"
KEY_FILE = "preview-access.key"
#: A long working session is still one session; the round-9 audit's longest
#: ran 6.7 hours. Twenty-four hours covers it without outliving the day.
DEFAULT_TTL = 24 * 3600
_VERSION = "v1"

#: Unsafe-method requests under these prefixes are refused for a preview
#: grant however valid the cookie is. The list is "everything that changes
#: who owns this install, what it can reach, or what runs on it": account
#: and pairing state, credentials, settings, network/cluster membership,
#: plugin installs and the internal control plane. Reads stay allowed so the
#: UI can render; chat/session routes stay writable so the agent can drive
#: the session it is verifying.
MUTATION_DENY_PREFIXES = (
    "/api/v1/auth",
    "/api/v1/security",
    "/api/v1/api-users",
    "/api/v1/pairing",
    "/api/v1/mobile",
    "/api/v1/model-providers",
    "/api/v1/claude",
    "/api/v1/github",
    "/api/v1/settings",
    "/api/settings",
    "/api/v1/export-import",
    "/api/v1/internal",
    "/api/v1/cluster",
    "/api/v1/network",
    "/api/v1/plugins",
    "/api/v1/connectors",
    "/api/v1/webhooks",
    "/api/v1/computer-use",
)
_SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})


@dataclass(frozen=True)
class Grant:
    session_id: str
    expires_at: int


def _key_path() -> Path:
    from app.core.config import settings

    return Path(settings.effective_data_dir) / KEY_FILE


def _key() -> bytes:
    """The per-install signing key, created on first use (0600, best effort)."""
    path = _key_path()
    try:
        raw = path.read_text(encoding="utf-8").strip()
        if len(raw) >= 64:
            return bytes.fromhex(raw)
    except (OSError, ValueError):
        pass
    fresh = secrets.token_hex(32)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(fresh, encoding="utf-8")
    try:
        os.chmod(tmp, 0o600)
    except OSError:
        pass
    os.replace(tmp, path)
    return bytes.fromhex(fresh)


def _sign(payload: str) -> str:
    return hmac.new(_key(), payload.encode("utf-8"), sha256).hexdigest()


def _b64(value: str) -> str:
    return base64.urlsafe_b64encode(value.encode("utf-8")).decode("ascii").rstrip("=")


def _unb64(value: str) -> str:
    padded = value + "=" * (-len(value) % 4)
    return base64.urlsafe_b64decode(padded.encode("ascii")).decode("utf-8")


def mint(session_id: str, ttl_seconds: int = DEFAULT_TTL, *, now: Optional[float] = None) -> str:
    """A token for ``session_id`` that expires ``ttl_seconds`` from now."""
    if not session_id or not isinstance(session_id, str):
        raise ValueError("A session id is required to mint preview access")
    if ttl_seconds <= 0 or ttl_seconds > 7 * 24 * 3600:
        raise ValueError("Preview access TTL must be within a week")
    expires = int((now if now is not None else time.time()) + ttl_seconds)
    payload = f"{_VERSION}.{_b64(session_id)}.{expires}"
    return f"{payload}.{_sign(payload)}"


def _session_exists(session_id: str) -> bool:
    """Whether the bound session is a real row. Fail closed on lookup errors.

    Without a managed database (unit tests, isolated runs) there is no row to
    check and the signature alone decides.
    """
    try:
        from app.core.config import settings

        if not settings.managed_database_available():
            return True
        from app.db.sessions import get_session

        return get_session(session_id) is not None
    except Exception:  # noqa: BLE001 — an unreadable DB is a denial, not a grant
        logger.debug("preview access: session lookup failed", exc_info=True)
        return False


def verify(token: Optional[str], *, now: Optional[float] = None) -> Optional[Grant]:
    """The grant a token carries, or None for anything malformed, forged,
    expired or bound to a session that no longer exists."""
    if not token or not isinstance(token, str) or len(token) > 512:
        return None
    parts = token.split(".")
    if len(parts) != 4 or parts[0] != _VERSION:
        return None
    payload = ".".join(parts[:3])
    if not hmac.compare_digest(_sign(payload), parts[3]):
        return None
    try:
        session_id = _unb64(parts[1])
        expires = int(parts[2])
    except (ValueError, UnicodeDecodeError):
        return None
    if expires <= (now if now is not None else time.time()):
        return None
    if not session_id or not _session_exists(session_id):
        return None
    return Grant(session_id=session_id, expires_at=expires)


def _private_peer(request) -> bool:
    """The transport half of the check, sharing ``local_access``'s address
    families: local ingress listener and a private-network peer. Forwarded
    headers are deliberately NOT a denial here (see the module docstring)."""
    from app.core import owner_auth

    if getattr(request, "scope", {}).get("shuffle.ingress") != "local":
        return False
    try:
        peer = ipaddress.ip_address(request.client.host)
    except (ValueError, AttributeError):
        return False
    return owner_auth.is_private_address(peer)


def transport_ok(request) -> bool:
    """Public wrapper of the transport checks for the redirect endpoint."""
    return _private_peer(request)


def mutation_allowed(path: str, method: str) -> bool:
    """Whether a preview grant may make this request at all."""
    if method.upper() in _SAFE_METHODS:
        return True
    return not path.startswith(MUTATION_DENY_PREFIXES)


def grant(request) -> Optional[Grant]:
    """The preview grant riding this request's cookie, if every check passes.

    Works for HTTP requests and WebSockets alike (both carry ``scope``,
    ``headers``, ``client`` and ``cookies``). Never raises.
    """
    try:
        cookies = getattr(request, "cookies", None) or {}
        token = cookies.get(COOKIE_NAME)
        if not token:
            return None
        if not _private_peer(request):
            return None
        found = verify(token)
        if found is None:
            return None
        scope = getattr(request, "scope", {}) or {}
        method = str(scope.get("method") or "GET")
        path = str(scope.get("path") or "/")
        if not mutation_allowed(path, method):
            return None
        return found
    except Exception:  # noqa: BLE001 — a broken cookie is a denial
        logger.debug("preview access: grant check failed", exc_info=True)
        return None


def safe_next(value: Optional[str]) -> str:
    """A same-app relative path for the post-cookie redirect, else ``/``."""
    if not value or not value.startswith("/") or value.startswith("//") or "\\" in value:
        return "/"
    parsed = urlsplit(value)
    if parsed.scheme or parsed.netloc:
        return "/"
    return value


def preview_url(session_id: str, host: str, port: int, *, next_path: str = "/",
                token: Optional[str] = None) -> str:
    """The one URL the agent browser opens first: sets the cookie, redirects."""
    token = token or mint(session_id)
    host_part = f"[{host}]" if ":" in host and not host.startswith("[") else host
    return (
        f"http://{host_part}:{port}{REDIRECT_PATH}"
        f"?{QUERY_PARAM}={quote(token, safe='')}&next={quote(safe_next(next_path), safe='/')}"
    )
