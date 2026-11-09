"""Session-scoped preview access for the agent browser.

Why this exists. The worker's UI sits behind
:func:`app.core.owner_auth.local_access`, which admits only a direct LAN owner
and rejects anything that looks proxied — including the branch's own Vite dev
server proxying ``/api`` to the worker, and the agent browser when it runs on
another host (the container's Chromium, a shared runner). Sessions that open
the worker's UI to verify a change hit "only accepts direct connections from
its local network" / ``local_access: false``. This module is the narrow door.

Threat model, and what the token is NOT:

* The token is minted by the harness for ONE app session and carries that
  session id; it is never accepted for a session that does not exist, nor
  for one that runs as an API key (a grant is the owner, see
  :func:`_session_exists`). It
  expires (``DEFAULT_TTL`` at most, see :func:`session_token`), and nothing
  renews it silently.
* It is an HMAC over ``session_id.expires`` under a per-install key kept in
  the data dir (``preview-access.key``, created 0600). Any process sharing the
  data dir — the running worker, or an isolated ``AI_SHUFFLE_PREVIEW=1``
  instance started from a branch — checks the signature statelessly: no
  in-memory table that a second process cannot see.
* It is honoured ONLY on the local ingress listener and ONLY from a private
  network peer (the same address families ``local_access`` trusts). Unlike
  ``local_access`` it tolerates ``X-Forwarded-*`` headers: the failing case
  is the branch's Vite dev server proxying ``/api`` to the worker
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
  even when the browser independently authenticates as a local owner, paired
  client or API user. One exception, on a launcher-marked throwaway instance
  only: it may add a project and set one up on, or remove it from, a paired
  dev worker, and create or update an agent
  (``app.api.auth._preview_may_change_dev_data``), so the dev loop can be
  driven at all. Cross-site mutations are refused by the same
  ``same_origin`` check the local owner is held to. Account-preference writes
  are also refused by the preferences API, even if the browser independently
  qualifies as a direct LAN owner; test clicks must not sync to the user's UI.
* A chat a browser with a live cookie sends is the preview's, whatever else
  the browser presents (``session_modes.caller_of``): the local listener, an
  owner login, a paired device or an API key, on a socket or over REST. A
  credential that fails is not replaced by the cookie, and a paired device's
  or API key's token keeps its own identity and scope under the limit.
* A cookie that cannot be checked (unreadable key, failed session lookup) is
  never read as "no preview here": :func:`verify` raises, ``enforce_limits``
  answers 503, and its one decision is what the request's later checks reuse.
* The URL that carries the token lands in the session's system prompt (the
  "# Workspace facts" card) and therefore in transcripts. That is accepted:
  the token is useless off the LAN, dies with the session, and can do nothing
  the model could not already do through its tools on that worker.
"""
from __future__ import annotations

import base64
import errno
import hmac
import ipaddress
import logging
import os
import re
import secrets
import threading
import time
import traceback
import uuid
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from functools import lru_cache
from hashlib import sha256
from pathlib import Path
from time import monotonic as _monotonic
from typing import Iterator, Optional
from urllib.parse import quote, urlsplit

logger = logging.getLogger(__name__)

COOKIE_PREFIX = "ais_preview_"
# Upgrade bridge: old grants still expire normally and must verify locally.
LEGACY_COOKIE_NAME = "ais_preview"
QUERY_PARAM = "token"
REDIRECT_PATH = "/api/v1/auth/preview-access"
KEY_FILE = "preview-access.key"
#: A long working session is still one session. Twenty-four hours covers one
#: without outliving the day.
DEFAULT_TTL = 24 * 3600
#: The harness's URL lands in the cached prompt prefix (the "# Workspace
#: facts" card). The harness persists that card and reuses it on reconnect,
#: re-rendering only when the quoted token is about to expire or no snapshot
#: could be stored. A token whose expiry is "now + TTL" would differ on every
#: such render, so the expiry is rounded DOWN to this window: every render
#: inside one window yields the same bytes, and a token never outlives
#: ``DEFAULT_TTL`` (it lives between ``DEFAULT_TTL - STABLE_WINDOW`` and
#: ``DEFAULT_TTL``).
STABLE_WINDOW = 6 * 3600
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
    "/api/v1/memories",
    "/api/settings",
    "/api/v1/internal",
    "/api/v1/cluster",
    "/api/v1/network",
    "/api/v1/plugins",
    "/api/v1/connectors",
    "/api/v1/computer-use",
    "/api/v1/chat-access",
    "/api/v1/voice",
)
_SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
#: Placing a call on a chat: attach it, open its audio socket, hang up.
_DEV_VOICE_CALL = re.compile(r"/api/v1/voice/sessions/[^/]+/(?:attach|audio|hangup)")
#: The listener the agent browser uses (see :mod:`app.serve`). The browser is
#: refused the worker's local and protected ports because a request from the
#: same host to them is the owner; this one never confers that trust, and
#: admits nothing but the token redirect and requests carrying a live grant.
PREVIEW_INGRESS = "preview"
_listener_port: Optional[int] = None
#: The address the listener is bound to ("0.0.0.0", "127.0.0.1", ...).
_listener_host: Optional[str] = None


def set_listener_port(port: Optional[int], host: Optional[str] = None) -> None:
    """Record the bound preview listener (None when it is off or failed)."""
    global _listener_port, _listener_host
    _listener_port = int(port) if port else None
    _listener_host = (host or None) if _listener_port else None


def listener_port() -> Optional[int]:
    """The preview listener's port in this process, or None without one."""
    return _listener_port


def listener_loopback_only() -> bool:
    """Whether the preview listener answers on loopback and nowhere else.

    The desktop worker binds 127.0.0.1, so a link naming the LAN address gets
    an empty response there (#1789). Its agent browser runs on the same
    machine, so loopback is the address that reaches the listener."""
    host = (_listener_host or "").strip().strip("[]")
    if not _listener_port or not host:
        return False
    if host.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def advertised_host(browser_host: Optional[str]) -> Optional[str]:
    """The host a preview link should name: one the listener answers on.

    A loopback-only listener is advertised by its loopback address; any other
    bind (all interfaces, a LAN address) keeps ``browser_host``, the address
    the agent browser reaches this machine by."""
    if listener_loopback_only():
        host = (_listener_host or "").strip().strip("[]")
        return "127.0.0.1" if host.lower() == "localhost" else host
    return browser_host


@dataclass(frozen=True)
class Grant:
    session_id: str
    expires_at: int


#: ``enforce_limits``' decision for one request. ASGI scope, not a header or
#: cookie, so a client cannot set it; it dies with the request.
_DECISION_SCOPE_KEY = "shuffle.preview_decision"


# Denial diagnostics (#1002). One request is checked at more than one stage
# (ingress, enforce_limits, grant when no decision exists yet), and each failing
# stage answers differently. A retained line names the stage and reason, correlated per request, so a
# transient failure can be classified afterwards. Only allow-listed metadata is
# logged: never a token, a cookie name or value, a query string or exception text.
_CHECK_SCOPE_KEY = "shuffle.preview_check"
_UNAVAILABLE_REASONS = frozenset({
    "key_unavailable", "session_lookup_unavailable", "check_failed", "check_not_started",
})
#: Each (stage, reason) logs at most once per window. An outage should stay
#: visible; a stale cookie replayed on every request should not flood the log.
_UNAVAILABLE_LOG_WINDOW_S = 1.0
_DENIAL_LOG_WINDOW_S = 30.0
_denial_log: dict[tuple[str, str], tuple[float, int]] = {}
_denial_log_lock = threading.Lock()


@dataclass
class _Check:
    """One verification of one request, for the denial log only."""
    stage: str
    request_id: str = "-"
    ordinal: int = 0
    started: Optional[float] = None
    method: str = "-"
    path: str = "-"
    cookie_present: Optional[bool] = None
    cookie_count: Optional[int] = None
    denied: bool = False


_active_check: ContextVar[Optional[_Check]] = ContextVar("preview_access_check", default=None)


def describe_error(exc: BaseException) -> str:
    """An exception's class, errno or SQLSTATE and innermost frames, never its
    message: driver and OS messages can quote connection details and paths.
    Walks the traceback without reading source files (see #2112)."""
    parts = [type(exc).__name__]
    code = getattr(exc, "errno", None)
    if isinstance(code, int):
        parts.append(f"errno={errno.errorcode.get(code, code)}")
    sqlstate = getattr(exc, "pgcode", None)
    if isinstance(sqlstate, str) and re.fullmatch(r"[0-9A-Z]{5}", sqlstate):
        parts.append(f"sqlstate={sqlstate}")
    frames = [f"{os.path.basename(frame.f_code.co_filename)}:{line}:{frame.f_code.co_name}"
              for frame, line in traceback.walk_tb(exc.__traceback__)]
    if frames:
        parts.append("at=" + "<-".join(reversed(frames[-3:])))
    return " ".join(parts)


def _begin_check(stage: str, scope, cookies) -> _Check:
    """The next check of this request; scope state correlates its stages."""
    state = scope.get(_CHECK_SCOPE_KEY) if isinstance(scope, dict) else None
    if state is None:
        state = {"id": uuid.uuid4().hex[:8], "checks": 0, "started": _monotonic()}
        if isinstance(scope, dict):
            scope[_CHECK_SCOPE_KEY] = state
    state["checks"] += 1
    details = scope if isinstance(scope, dict) else {}
    method = "WEBSOCKET" if details.get("type") == "websocket" else str(details.get("method") or "-")
    return _Check(
        stage=stage,
        request_id=state["id"],
        ordinal=state["checks"],
        started=state["started"],
        method=method,
        # Decoded client input: keep it on one log line.
        path=re.sub(r"[^\x21-\x7e]", "?", str(details.get("path") or "-"))[:160],
        cookie_present=any(name == LEGACY_COOKIE_NAME or name.startswith(COOKIE_PREFIX) for name in cookies),
        cookie_count=len(cookies),
    )


@contextmanager
def _checking(stage: str, scope, cookies) -> Iterator[_Check]:
    check = _begin_check(stage, scope, cookies)
    active = _active_check.set(check)
    try:
        yield check
    except Exception as exc:
        if not check.denied:  # Anything the verifier did not already classify.
            _note_denial("check_failed", exc)
        raise
    finally:
        _active_check.reset(active)


def _note_denial(reason: str, exc: Optional[BaseException] = None) -> None:
    """Log why a check said no. Never raises: diagnostics cannot change a decision."""
    try:
        check = _active_check.get() or _Check(stage="verify")
        check.denied = True
        window = _UNAVAILABLE_LOG_WINDOW_S if reason in _UNAVAILABLE_REASONS else _DENIAL_LOG_WINDOW_S
        now = _monotonic()
        with _denial_log_lock:
            last, suppressed = _denial_log.get((check.stage, reason), (None, 0))
            if last is not None and now - last < window:
                _denial_log[(check.stage, reason)] = (last, suppressed + 1)
                return
            _denial_log[(check.stage, reason)] = (now, 0)
        elapsed = "-" if check.started is None else f"{(now - check.started) * 1000:.0f}"
        logger.warning(
            "Preview access denied: stage=%s reason=%s request=%s ordinal=%d elapsed_ms=%s "
            "cookie_present=%s cookie_count=%s method=%s path=%s suppressed=%d error=%s",
            check.stage, reason, check.request_id, check.ordinal, elapsed,
            check.cookie_present, check.cookie_count, check.method, check.path, suppressed,
            describe_error(exc) if exc is not None else "-",
        )
    except Exception:  # noqa: BLE001 — a log line must never decide access
        pass


def _key_path() -> Path:
    from app.core.config import settings

    return Path(settings.effective_data_dir) / KEY_FILE


def _read_key(path: Path) -> bytes:
    from app.core.private_storage import protect_private_path, service_files

    with service_files():
        protect_private_path(path)
        raw = path.read_text(encoding="utf-8").strip()
    if len(raw) < 64:
        raise ValueError("Invalid preview access key")
    return bytes.fromhex(raw)


def _key(path: Optional[Path] = None) -> bytes:
    """Publish one complete key without replacing another process's key."""
    path = path if path is not None else _key_path()
    try:
        return _read_key(path)
    except FileNotFoundError:
        pass
    fresh = secrets.token_hex(32)
    path.parent.mkdir(parents=True, exist_ok=True)
    from app.core.private_storage import private_mkstemp, service_files

    with service_files():
        fd, name = private_mkstemp(prefix="preview-access-", suffix=".tmp", dir=path.parent)
        tmp = Path(name)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                stream.write(fresh)
            # Both operations publish a complete file without replacing a winner.
            # Windows rename is exclusive; POSIX rename would overwrite. Avoid a
            # second Windows hard link whose cleanup could race a reader's handle.
            try:
                if os.name == "nt":
                    os.rename(tmp, path)
                else:
                    os.link(tmp, path)
            except FileExistsError:
                pass
            return _read_key(path)
        finally:
            tmp.unlink(missing_ok=True)


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
    return _token(session_id, expires)


def _token(session_id: str, expires: int) -> str:
    payload = f"{_VERSION}.{_b64(session_id)}.{expires}"
    return f"{payload}.{_sign(payload)}"


def session_token(session_id: str, *, now: float | None = None) -> str:
    """The session's prompt-stable token: the same string for every call
    inside one :data:`STABLE_WINDOW`.

    The signature is a pure function of ``(session_id, expires)`` under the
    install key, so pinning ``expires`` to a window boundary is all it takes.
    The boundary is rounded DOWN from ``now + DEFAULT_TTL``, so the token is
    never valid for longer than :func:`mint`'s default would be.
    """
    return session_token_with_expiry(session_id, now=now)[0]


def session_token_with_expiry(session_id: str, *, now: float | None = None) -> tuple[str, int]:
    """:func:`session_token` plus the expiry (epoch seconds) it carries.

    The harness records the expiry next to the persisted "# Workspace facts"
    card, which quotes the token, so a reconnect can tell when the frozen
    card's URL has stopped working and must be rendered again."""
    if not session_id or not isinstance(session_id, str):
        raise ValueError("A session id is required to mint preview access")
    current = now if now is not None else time.time()
    expires = (int(current) + DEFAULT_TTL) // STABLE_WINDOW * STABLE_WINDOW
    return _token(session_id, expires), expires


def _session_exists(session_id: str) -> bool:
    """Whether the bound session is a real row the OWNER runs.

    A failed lookup raises rather than answering "no": callers applying
    restrictions must not read it as an absent grant (see :func:`cookie_token`).

    A grant authenticates as the owner (``is_admin``, no ``api_user``), so a
    session that runs as an API key (its own ``api_user_id`` or a worker under
    one) never gets one: its agent would otherwise sign in as the owner and
    undo every key limit (MCP masking, usage refusal, notes scoping). The
    harness does not mint for such sessions; this refuses any token that was.

    The row always decides, never the signature alone. Only a worker verifies
    grants, and it cannot serve without the pool it opened at startup. The
    supervisor's descriptor file says nothing about that pool afterwards, and
    with no database to ask the answer is no.
    """
    try:
        from app.core.api_user_scope import session_api_user_id
        from app.db.sessions import get_session

        row = get_session(session_id)
        if row is None:
            _note_denial("session_missing")
            return False
        if session_api_user_id(row) is not None:
            _note_denial("session_not_owner")
            return False
        return True
    except Exception as exc:
        _note_denial("session_lookup_unavailable", exc)
        raise


def verify(token: Optional[str], *, now: Optional[float] = None) -> Optional[Grant]:
    """The grant a token carries, or None for anything malformed, forged,
    expired or bound to a session that no longer exists.

    Raises when it cannot tell (unreadable key, failed session lookup), so each
    caller decides what "unknown" means: a denial when admitting, a refusal when
    restricting. Treating it as None would lift every restriction."""
    if not token:
        return None  # No credential was presented, so nothing was refused.
    if not isinstance(token, str) or len(token) > 512:
        _note_denial("malformed_token")
        return None
    parts = token.split(".")
    if len(parts) != 4 or parts[0] != _VERSION:
        _note_denial("malformed_token")
        return None
    payload = ".".join(parts[:3])
    if not parts[3].isascii():
        _note_denial("bad_signature")
        return None
    try:
        expected = _sign(payload)
    except Exception as exc:
        # Propagates: callers applying restrictions must not read an unreadable
        # key as an absent grant.
        _note_denial("key_unavailable", exc)
        raise
    if not hmac.compare_digest(expected, parts[3]):
        _note_denial("bad_signature")
        return None
    try:
        session_id = _unb64(parts[1])
        expires = int(parts[2])
    except (ValueError, UnicodeDecodeError):
        _note_denial("malformed_token")
        return None
    if expires <= (now if now is not None else time.time()):
        _note_denial("expired_token")
        return None
    if not session_id:
        _note_denial("malformed_token")
        return None
    if not _session_exists(session_id):
        return None
    return Grant(session_id=session_id, expires_at=expires)


def _private_peer(request) -> bool:
    """The transport half of the check, sharing ``local_access``'s address
    families: local ingress listener and a private-network peer. Forwarded
    headers are deliberately NOT a denial here (see the module docstring)."""
    from app.core import owner_auth

    if getattr(request, "scope", {}).get("shuffle.ingress") not in ("local", PREVIEW_INGRESS):
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
    if path.startswith("/api/v1/projects/") and "/git/worktrees/" in path and (
        path.rstrip("/").endswith("/forget") or path.rstrip("/").endswith("/git/worktrees/reclaim")
    ):
        return False
    # Attaching a project gives a no-project chat a shell and file edits
    # (issue #371). Owner only, even when the browser is also the owner.
    if path.startswith("/api/v1/sessions/") and path.rstrip("/").endswith("/attach-project"):
        return False
    if _DEV_VOICE_CALL.fullmatch(path):
        # Calls spend the owner's voice provider, so a preview may not place
        # one (#420), except on a dev instance, whose only voice provider is
        # the dev loop's fake (app.core.voice_endpoints).
        from app.core.voice_endpoints import dev_calls_allowed

        return dev_calls_allowed()
    return not path.startswith(MUTATION_DENY_PREFIXES)


@lru_cache(maxsize=32)
def _cookie_name_for_path(path: Path) -> str:
    # Cache only the public name, never the signing key or verification result.
    # Keys are install-lifetime files; replacing one requires a worker restart.
    fingerprint = sha256(b"ai-shuffle-preview-cookie\0" + _key(path)).hexdigest()[:24]
    return COOKIE_PREFIX + fingerprint


def cookie_name() -> str:
    """A stable namespace per install, independent of listener/proxy ports.

    Browsers share cookies across ports on a host. Isolated previews use
    different signing keys, so their grants must have different names too.
    Resolve lazily after startup config, then cache per key path so ordinary
    lookups do not repeat disk reads (or Windows ACL protection writes).
    """
    return _cookie_name_for_path(_key_path())


def cookie_token(cookies) -> Optional[str]:
    """Read this install's name, with a legacy fallback for in-flight grants.

    Normal owner cookies must not read or create a preview key. Lookup errors
    for preview cookies deliberately propagate: callers applying restrictions
    must not confuse an unreadable key with an absent grant.
    """
    legacy = cookies.get(LEGACY_COOKIE_NAME)
    if not any(name.startswith(COOKIE_PREFIX) for name in cookies):
        return legacy
    return cookies.get(cookie_name()) or legacy


def _noted_cookie_token(cookies) -> Optional[str]:
    try:
        return cookie_token(cookies)
    except Exception as exc:
        _note_denial("key_unavailable", exc)
        raise


def admit_ingress(scope) -> bool:
    """Whether the preview listener lets this request reach the app at all."""
    from starlette.requests import HTTPConnection

    cookies = HTTPConnection(scope).cookies
    try:
        with _checking("ingress", scope, cookies) as check:
            token = _noted_cookie_token(cookies)
            if not token:
                _note_denial("other_install_cookie" if check.cookie_present else "missing_cookie")
                return False
            return verify(token) is not None
    except Exception:  # noqa: BLE001 — an unreadable grant is a denial
        return False


def note_ingress_not_started(scope, exc: BaseException) -> None:
    """The listener could not start its check, usually because no worker thread
    could. Runs on the event loop: only the shared log limiter, no key, database
    or executor work. Never raises: the request is refused either way."""
    try:
        from starlette.requests import HTTPConnection

        with _checking("ingress", scope, HTTPConnection(scope).cookies):
            _note_denial("check_not_started", exc)
    except Exception:  # noqa: BLE001 — a log line must never decide access
        pass


def _remember(conn, found: Optional[Grant]) -> None:
    conn.state.preview_grant = found
    conn.scope[_DECISION_SCOPE_KEY] = found


def settle(conn) -> None:
    """Decide a connection's preview cookie before its other credentials count.

    For socket authentication, which offers several credentials and has no
    ``enforce_limits`` in front of it. :func:`grant` then reuses this answer, so
    a later failure cannot read as "no preview here". Raises when it cannot
    tell. A connection without a preview cookie does no key or database work.
    """
    with _checking("settle", getattr(conn, "scope", None), conn.cookies):
        token = _noted_cookie_token(conn.cookies)
        if token and _DECISION_SCOPE_KEY not in conn.scope:
            _remember(conn, verify(token))


def enforce_limits(request) -> None:
    """A valid preview cookie restricts all HTTP and voice WebSocket auth paths.

    This is not authentication: transport checks still apply when a preview
    cookie is used to admit a request. Even a separately authenticated owner,
    paired client or API user must not bypass the cookie's mutation ceiling.
    Invalid/expired cookies neither grant access nor restrict ordinary clients.
    """
    from fastapi import HTTPException

    try:
        with _checking("enforce_limits", getattr(request, "scope", None), request.cookies):
            token = _noted_cookie_token(request.cookies)
            found = verify(token) if token else None
    except Exception as exc:  # noqa: BLE001 — never bypass limits on key failure
        raise HTTPException(503, "Preview access could not be checked. Try again later.") from exc
    _remember(request, found)
    # WebSockets have no HTTP method: never treat an audio upgrade as a safe GET.
    method = "WEBSOCKET" if request.scope.get("type") == "websocket" else request.method
    if found is not None and not mutation_allowed(request.url.path, method):
        if request.url.path.startswith("/api/v1/voice"):
            raise HTTPException(403, "Voice calls are not available in preview access")
        raise HTTPException(403, "Agent previews cannot change ownership, credentials or settings.")


def grant(request) -> Optional[Grant]:
    """The preview grant riding this request's cookie, if every check passes.

    Works for HTTP requests and WebSockets alike (both carry ``scope``,
    ``headers``, ``client`` and ``cookies``). Never raises.
    """
    try:
        cookies = getattr(request, "cookies", None) or {}
        scope = getattr(request, "scope", {}) or {}
        with _checking("grant", scope, cookies):
            if _DECISION_SCOPE_KEY in scope:
                found = scope[_DECISION_SCOPE_KEY]
                if found is None:
                    return None
                if not _private_peer(request):
                    _note_denial("transport_refused")
                    return None
            else:
                token = _noted_cookie_token(cookies)
                if not token:
                    return None
                if not _private_peer(request):
                    _note_denial("transport_refused")
                    return None
                found = verify(token)
                if found is None:
                    return None
            method = str(scope.get("method") or "GET")
            path = str(scope.get("path") or "/")
            if not mutation_allowed(path, method):
                return None
            return found
    except Exception:  # noqa: BLE001 — a broken cookie is a denial
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
    """The one URL the agent browser opens first: sets the cookie, redirects.

    Without an explicit ``token`` it carries :func:`session_token`, so the
    URL (and the cached prompt card that quotes it) is byte-identical across
    reconnects of the same session inside one :data:`STABLE_WINDOW`."""
    token = token or session_token(session_id)
    host_part = f"[{host}]" if ":" in host and not host.startswith("[") else host
    return (
        f"http://{host_part}:{port}{REDIRECT_PATH}"
        f"?{QUERY_PARAM}={quote(token, safe='')}&next={quote(safe_next(next_path), safe='/')}"
    )
