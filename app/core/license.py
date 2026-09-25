"""License leases on the worker.

The license server (android/license) hands each activated worker an
Ed25519-signed lease. This module verifies leases offline against the public
keys built into the app, keeps the lease in the settings store, renews it once
a day with jittered retry, and answers one question for the rest of the app:
``license_allows_agent_runs()``.

Rules it follows:

- A lease is good for 14 days, so a worker can stay offline for two weeks.
- After a failed payment the server keeps issuing leases for 14 days with
  ``grace: true``; the worker shows a banner and keeps running.
- Without a valid lease the worker is *unlicensed*: it refuses to start new
  agent runs and nothing else. Sessions, files, settings, exports and backups
  stay readable and writable. Nothing here deletes or locks data.

Beta: with no license server URL configured, with ``LICENSE_BETA=true`` (set
at build time while the beta runs), or in a build without a lease public key,
every call reports an always-valid beta lease and no network call is made.

What the worker sends to the license server: the license key (on activation
only), a random install id, a SHA-256 fingerprint of that id and the machine
id, the host name as a display name, and the app version. Never prompts,
model traffic or provider credentials.

First-install key: a key given before the worker ever ran is activated on its
own when the worker starts (:func:`activate_first_install_key`). It comes from
the desktop launcher's ``license-key.json`` in the update folder
(``SHUFFLE_SOURCE_ROOT``; the desktop deletes that file itself once a lease
has opened its update feed) or from ``LICENSE_KEY`` in the environment
(Docker, Unraid), which is taken out of the environment at startup so agents
never inherit it. Each key is tried once: after the license server answered,
yes or no, the same key is not sent again.
"""
from __future__ import annotations

import asyncio
import base64
import binascii
import hashlib
import json
import logging
import os
import random
import socket
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from cryptography.hazmat.primitives.serialization import load_der_public_key

from app.core.config import settings
from app.db import database

logger = logging.getLogger(__name__)

LEASE_FORMAT = "shuffle-lease-v1"

# Public halves of the lease signing keys, by key id (SPKI DER, base64). The
# private key lives only in the license server's LEASE_SIGNING_KEY secret.
# Generate a pair with android/license/scripts/generate-lease-key.mjs and paste
# the public key here under the Worker's LEASE_KEY_ID. While this is empty the
# build cannot verify leases and runs in beta mode.
LEASE_PUBLIC_KEYS: dict[str, str] = {}

STATE_KEY_PREFIX = "license_state:"

DAY = 86400.0
REFRESH_INTERVAL = DAY
REFRESH_JITTER = 2 * 3600.0
RETRY_BASE = 300.0
RETRY_MAX = 6 * 3600.0
FIRST_CHECK_DELAY = 60.0
WARN_BEFORE_EXPIRY = 3 * DAY
HTTP_TIMEOUT = 15.0
MAX_RESPONSE_BYTES = 64 * 1024


class LicenseError(Exception):
    """A user-facing failure with an HTTP status for the API layer."""

    def __init__(self, status: int, message: str, **extra: Any):
        super().__init__(message)
        self.status = status
        self.message = message
        self.extra = extra


class LeaseError(ValueError):
    pass


# ---------------------------------------------------------------------------
# Mode
# ---------------------------------------------------------------------------

def server_url() -> str:
    return (settings.license_server_url or "").strip().rstrip("/")


def beta_mode() -> bool:
    return bool(settings.license_beta) or not server_url() or not LEASE_PUBLIC_KEYS


def _checked_server_url() -> str:
    url = server_url()
    parts = urlsplit(url)
    local = parts.hostname in ("localhost", "127.0.0.1", "::1")
    if parts.scheme != "https" and not (parts.scheme == "http" and local):
        raise LicenseError(503, "The license server address must use HTTPS.")
    if not parts.hostname or parts.username or parts.password or parts.query or parts.fragment:
        raise LicenseError(503, "The license server address is not valid.")
    return url


# ---------------------------------------------------------------------------
# Stored state (one row per install; installs sharing a database keep their own)
# ---------------------------------------------------------------------------

def _state_key() -> str:
    return STATE_KEY_PREFIX + settings.effective_instance_id


def _load_state(cached: bool = False) -> dict:
    getter = database.get_system_setting_cached if cached else database.get_system_setting
    raw = getter(_state_key())
    if not raw:
        return {}
    try:
        value = json.loads(raw)
        return value if isinstance(value, dict) else {}
    except (TypeError, ValueError):
        logger.warning("Saved license state is unreadable; treating this worker as unlicensed")
        return {}


def _save_state(state: dict) -> None:
    database.set_system_setting(_state_key(), json.dumps(state, sort_keys=True))


def _seal(token: str) -> str:
    from app.core import encryption
    if encryption.is_encryption_ready():
        return encryption.encrypt_value(token)
    return token


def _unseal(value: str | None) -> str | None:
    if not value:
        return None
    from app.core import encryption
    if encryption.is_encrypted(value):
        if not encryption.is_encryption_ready():
            return None
        try:
            return encryption.decrypt_value(value)
        except ValueError:
            return None
    return value


def _install_fingerprint(install_id: str) -> str:
    machine = ""
    for path in ("/etc/machine-id", "/var/lib/dbus/machine-id"):
        try:
            machine = Path(path).read_text().strip()
            if machine:
                break
        except OSError:
            continue
    if not machine:
        machine = socket.gethostname()
    return hashlib.sha256(f"shuffle-install:{install_id}:{machine}".encode()).hexdigest()


def _display_name() -> str:
    name = "".join(ch for ch in socket.gethostname() if ch.isprintable()).strip()
    return name[:80] or "AI Shuffle worker"


# ---------------------------------------------------------------------------
# Lease verification
# ---------------------------------------------------------------------------

_verified_lock = threading.Lock()
_verified: dict[str, dict] = {}


def _b64(value: Any) -> bytes:
    if not isinstance(value, str):
        raise LeaseError("Lease field is not text")
    try:
        return base64.b64decode(value, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise LeaseError("Lease field is not base64") from exc


def verify_lease(envelope: Any, install_id: str | None = None) -> dict:
    """Return the lease payload if the signature and shape are right.

    Expiry is not checked here; :func:`status` compares it with the clock.
    """
    if not isinstance(envelope, dict) or envelope.get("format") != LEASE_FORMAT:
        raise LeaseError("Unknown lease format")
    cache_key = json.dumps(envelope, sort_keys=True)
    with _verified_lock:
        payload = _verified.get(cache_key)
    if payload is None:
        spki = LEASE_PUBLIC_KEYS.get(str(envelope.get("key_id")))
        if not spki:
            raise LeaseError("Lease was signed by an unknown key")
        body = _b64(envelope.get("payload"))
        try:
            key = load_der_public_key(base64.b64decode(spki))
            if not isinstance(key, Ed25519PublicKey):
                raise LeaseError("Lease key is not Ed25519")
            key.verify(_b64(envelope.get("signature")), body)
        except InvalidSignature as exc:
            raise LeaseError("Lease signature is not valid") from exc
        try:
            payload = json.loads(body)
        except ValueError as exc:
            raise LeaseError("Lease payload is not JSON") from exc
        if not isinstance(payload, dict) or payload.get("v") != 1:
            raise LeaseError("Unknown lease version")
        for field in ("issued_at", "expires_at", "seats"):
            if not isinstance(payload.get(field), int):
                raise LeaseError(f"Lease has no {field}")
        with _verified_lock:
            if len(_verified) > 64:
                _verified.clear()
            _verified[cache_key] = payload
    if install_id is not None and payload.get("install_id") != install_id:
        raise LeaseError("Lease belongs to another worker")
    return payload


# ---------------------------------------------------------------------------
# Status
# ---------------------------------------------------------------------------

def _iso(seconds: float | None) -> str | None:
    if seconds is None:
        return None
    return datetime.fromtimestamp(seconds, tz=timezone.utc).isoformat().replace("+00:00", "Z")


def _day(seconds: float) -> str:
    moment = datetime.fromtimestamp(seconds, tz=timezone.utc)
    return f"{moment:%B} {moment.day}, {moment.year}"


def _beta_status() -> dict:
    return {
        "mode": "beta", "state": "beta", "allowed": True, "reason": None, "banner": None,
        "plan": "beta", "seats": None, "seats_used": None, "account_id": None, "license_id": None,
        "key_hint": None, "install_id": None, "issued_at": None, "expires_at": None,
        "grace": False, "grace_until": None, "last_refresh_at": None, "next_refresh_at": None,
        "last_error": None, "server_status": None, "server_configured": bool(server_url()),
    }


def status(now: float | None = None, *, cached: bool = False) -> dict:
    """The license picture for this worker, as the API and the gate see it."""
    if beta_mode():
        return _beta_status()
    now = time.time() if now is None else now
    state = _load_state(cached=cached)
    result = _beta_status() | {
        "mode": "licensed", "plan": None,
        "key_hint": state.get("key_hint"), "install_id": state.get("install_id"),
        "last_refresh_at": _iso(state.get("last_refresh_at")),
        "next_refresh_at": _iso(state.get("next_refresh_at")),
        "last_error": state.get("last_error"), "server_status": state.get("server_status"),
    }
    lease = state.get("lease")
    if not lease:
        return result | {
            "state": "unlicensed", "allowed": False,
            "reason": "No license is active on this worker, so new agent runs are paused. Add your license key in Settings > License. Your data is not affected.",
            "banner": {"severity": "error", "message": "No license is active on this worker. New agent runs are paused until you add a license key."},
        }
    try:
        payload = verify_lease(lease, state.get("install_id"))
    except LeaseError as exc:
        logger.warning("Saved license lease rejected: %s", exc)
        return result | {
            "state": "invalid", "allowed": False,
            "reason": "The saved license lease could not be verified, so new agent runs are paused. Enter your license key again in Settings > License.",
            "banner": {"severity": "error", "message": "The saved license could not be verified. Enter your license key again."},
        }
    expires = float(payload["expires_at"])
    grace_until = payload.get("grace_until")
    result |= {
        "plan": payload.get("plan"), "seats": payload.get("seats"), "seats_used": payload.get("seats_used"),
        "account_id": payload.get("account_id"), "license_id": payload.get("license_id"),
        "key_hint": payload.get("key_hint") or state.get("key_hint"),
        "issued_at": _iso(payload["issued_at"]), "expires_at": _iso(expires),
        "grace": bool(payload.get("grace")), "grace_until": _iso(grace_until) if isinstance(grace_until, int) else None,
    }
    if now >= expires:
        return result | {
            "state": "expired", "allowed": False,
            "reason": f"This worker's license lease ended on {_day(expires)}, so new agent runs are paused. Connect it to the internet or renew your license in Settings > License. Your data is not affected.",
            "banner": {"severity": "error", "message": f"The license lease ended on {_day(expires)}. New agent runs are paused until it renews."},
        }
    server_status = state.get("server_status") or {}
    if server_status.get("code"):
        message = server_status.get("message") or "The license server no longer renews this worker."
        return result | {
            "state": "ending", "allowed": True, "reason": None,
            "banner": {"severity": "warning", "message": f"{message} Agents can start new runs until {_day(expires)}."},
        }
    if result["grace"]:
        until = grace_until if isinstance(grace_until, int) else expires
        return result | {
            "state": "grace", "allowed": True, "reason": None,
            "banner": {"severity": "warning", "message": f"Your last payment failed. Agents keep running until {_day(until)}; update your payment method to keep your license."},
        }
    if expires - now < WARN_BEFORE_EXPIRY:
        return result | {
            "state": "expiring", "allowed": True, "reason": None,
            "banner": {"severity": "warning", "message": f"This worker could not renew its license and its lease ends on {_day(expires)}. Check its internet connection."},
        }
    return result | {"state": "trialing" if payload.get("status") == "trialing" else "active", "allowed": True, "reason": None}


LEASE_HEADER = "X-Shuffle-Lease"


def lease_header() -> str | None:
    """The current lease as the value of the ``X-Shuffle-Lease`` header: the
    signed envelope as JSON, base64url. None in beta mode or without a lease.

    Sent only to the account hub: on the gated source feed
    (android/hub/src/license-gate.ts) and on the worker's activate and
    connector calls, where it lifts the account's tunnel cap
    (android/hub/src/tunnel-capacity.ts, app/core/account_hub.py)."""
    if beta_mode():
        return None
    lease = _load_state(cached=True).get("lease")
    if not isinstance(lease, dict):
        return None
    raw = json.dumps(lease, separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def license_allows_agent_runs() -> tuple[bool, str | None]:
    """Whether this worker may start a new agent run, and why not.

    Returns ``(True, None)`` when licensed, in grace, in beta, or when the
    license state cannot be read (the license is a soft check and must never
    be the reason a working install breaks). Otherwise ``(False, reason)``
    with a message fit to show the user. Only new runs are gated; callers
    must not use this to block reading or exporting data.
    """
    try:
        if beta_mode():
            return True, None
        current = status(cached=True)
        return bool(current["allowed"]), current["reason"]
    except Exception:
        logger.exception("License check failed; allowing the run")
        return True, None


# ---------------------------------------------------------------------------
# Talking to the license server
# ---------------------------------------------------------------------------

_lock: asyncio.Lock | None = None


def _state_lock() -> asyncio.Lock:
    global _lock
    if _lock is None:
        _lock = asyncio.Lock()
    return _lock


async def _post(path: str, body: dict) -> tuple[int, dict]:
    url = _checked_server_url() + "/v1/license/" + path
    try:
        async with httpx.AsyncClient(timeout=HTTP_TIMEOUT, follow_redirects=False) as client:
            response = await client.post(url, json=body, headers={"Accept": "application/json"})
    except httpx.HTTPError as exc:
        raise LicenseError(503, "Could not reach the license server. Check this worker's internet connection and try again.") from exc
    if len(response.content) > MAX_RESPONSE_BYTES:
        raise LicenseError(502, "The license server sent an unexpected response.")
    try:
        data = response.json()
    except ValueError:
        data = {}
    if not isinstance(data, dict):
        data = {}
    if response.status_code in (429,) or response.status_code >= 500:
        retry = response.headers.get("Retry-After")
        raise LicenseError(503, "The license server is busy. Try again in a few minutes.", retry_after=retry)
    return response.status_code, data


def _app_version() -> str:
    return "".join(ch for ch in str(settings.version) if ch.isalnum() or ch in ".+_-")[:40] or "0"


def _server_message(data: dict, fallback: str) -> str:
    message = data.get("error")
    return message if isinstance(message, str) and 0 < len(message) <= 300 else fallback


async def activate(key: str) -> dict:
    """Activate this worker with a license key and store the lease."""
    if beta_mode():
        raise LicenseError(409, "This build is part of the beta and needs no license key.")
    if not isinstance(key, str) or not key.strip() or len(key) > 64:
        raise LicenseError(400, "Enter a license key.")
    async with _state_lock():
        state = await asyncio.to_thread(_load_state)
        install_id = state.get("install_id") or str(uuid.uuid4())
        fingerprint = _install_fingerprint(install_id)
        code, data = await _post("activate", {
            "key": key.strip(), "install_id": install_id, "fingerprint": fingerprint,
            "name": _display_name(), "app_version": _app_version(),
        })
        if code != 200:
            extra = {}
            if code == 409 and data.get("code") == "seats_full":
                extra = {"code": "seats_full", "seats": data.get("seats"), "installs": data.get("installs") or []}
            status_code = code if code in (400, 402, 404, 409) else 502
            raise LicenseError(status_code, _server_message(data, "The license server refused this key."), **extra)
        lease, token = data.get("lease"), data.get("install_token")
        try:
            payload = verify_lease(lease, install_id)
        except LeaseError as exc:
            logger.error("License server returned a lease this build cannot verify: %s", exc)
            raise LicenseError(502, "The license server's answer could not be verified. Make sure this build is up to date.") from exc
        if not isinstance(token, str) or len(token) != 64:
            raise LicenseError(502, "The license server sent an unexpected response.")
        now = time.time()
        state = {
            "install_id": install_id,
            "install_token": _seal(token),
            "key_hint": payload.get("key_hint"),
            "lease": lease,
            "activated_at": now,
            "last_refresh_at": now,
            "next_refresh_at": _next_daily(now),
            "failures": 0,
            "last_error": None,
            "server_status": None,
        }
        await asyncio.to_thread(_save_state, state)
    logger.info("License activated on this worker (plan %s)", payload.get("plan"))
    _nudge_remote_access()
    return await asyncio.to_thread(status)


def _nudge_remote_access() -> None:
    """A new lease may lift the hub's tunnel cap for this account: ask it again now."""
    try:
        from app.core import managed_connection
        managed_connection.retry_soon()
    except Exception:  # noqa: BLE001 - remote access is optional
        logger.debug("Could not nudge the remote-access connection", exc_info=True)


async def deactivate() -> dict:
    """Give this worker's seat back and forget the lease. Data is untouched."""
    if beta_mode():
        raise LicenseError(409, "This build is part of the beta and needs no license key.")
    released = False
    async with _state_lock():
        state = await asyncio.to_thread(_load_state)
        token = _unseal(state.get("install_token"))
        if state.get("install_id") and token:
            try:
                code, _ = await _post("deactivate", {"install_id": state["install_id"], "install_token": token})
                released = code in (200, 404, 401, 410)
            except LicenseError:
                released = False
        # Keep the install id so a later activation reuses the same seat record.
        await asyncio.to_thread(_save_state, {"install_id": state.get("install_id")} if state.get("install_id") else {})
    logger.info("License deactivated on this worker (seat released on server: %s)", released)
    return (await asyncio.to_thread(status)) | {"seat_released": released}


async def release_seat(key: str, install_id: str) -> dict:
    """Free the seat of another worker on the same license (one that is gone),
    so this worker can activate. Needs the license key."""
    if beta_mode():
        raise LicenseError(409, "This build is part of the beta and needs no license key.")
    if not isinstance(key, str) or not key.strip() or len(key) > 64:
        raise LicenseError(400, "Enter a license key.")
    if not isinstance(install_id, str) or not 8 <= len(install_id) <= 128:
        raise LicenseError(400, "Choose a worker to deactivate.")
    code, data = await _post("deactivate", {"key": key.strip(), "install_id": install_id})
    if code != 200:
        raise LicenseError(code if code in (400, 404) else 502, _server_message(data, "The license server could not free that seat."))
    return {"ok": True, "seats_used": data.get("seats_used")}


def _next_daily(now: float) -> float:
    return now + REFRESH_INTERVAL + random.uniform(-REFRESH_JITTER, REFRESH_JITTER)


def _retry_delay(failures: int) -> float:
    base = min(RETRY_MAX, RETRY_BASE * (2 ** max(0, failures - 1)))
    return base * random.uniform(0.5, 1.5)


async def refresh() -> dict:
    """Renew the lease now. Failures are recorded, never raised to callers."""
    if beta_mode():
        return await asyncio.to_thread(status)
    async with _state_lock():
        state = await asyncio.to_thread(_load_state)
        token = _unseal(state.get("install_token"))
        install_id = state.get("install_id")
        if not install_id or not token:
            return await asyncio.to_thread(status)
        now = time.time()
        body = {"install_id": install_id, "install_token": token,
                "fingerprint": _install_fingerprint(install_id), "app_version": _app_version()}
        try:
            code, data = await _post("renew", body)
        except LicenseError as exc:
            failures = int(state.get("failures") or 0) + 1
            state |= {"failures": failures, "last_error": exc.message,
                      "next_refresh_at": now + _retry_delay(failures)}
            await asyncio.to_thread(_save_state, state)
            logger.warning("License renewal failed (attempt %d): %s", failures, exc.message)
            return await asyncio.to_thread(status)
        if code == 200:
            try:
                verify_lease(data.get("lease"), install_id)
            except LeaseError as exc:
                failures = int(state.get("failures") or 0) + 1
                state |= {"failures": failures, "last_error": "The renewed lease could not be verified.",
                          "next_refresh_at": now + _retry_delay(failures)}
                await asyncio.to_thread(_save_state, state)
                logger.error("License renewal returned an unverifiable lease: %s", exc)
                return await asyncio.to_thread(status)
            state |= {"lease": data["lease"], "last_refresh_at": now, "next_refresh_at": _next_daily(now),
                      "failures": 0, "last_error": None, "server_status": None}
        else:
            # The server answered and said no (cancelled, revoked, seat given
            # back). The current lease still runs to its end; check again daily.
            code_name = data.get("status") or data.get("code") or f"http_{code}"
            state |= {"last_refresh_at": now, "next_refresh_at": _next_daily(now), "failures": 0, "last_error": None,
                      "server_status": {"code": str(code_name)[:40],
                                        "message": _server_message(data, "The license server no longer renews this worker.")}}
            logger.warning("License server declined renewal: %s", code_name)
        await asyncio.to_thread(_save_state, state)
    return await asyncio.to_thread(status)


def _seconds_until_refresh(now: float) -> float:
    state = _load_state()
    if not state.get("install_token"):
        return REFRESH_INTERVAL
    due = state.get("next_refresh_at")
    if not isinstance(due, (int, float)):
        return FIRST_CHECK_DELAY
    return max(FIRST_CHECK_DELAY, due - now)


# ---------------------------------------------------------------------------
# First-install key
# ---------------------------------------------------------------------------

LICENSE_KEY_ENV = "LICENSE_KEY"
SOURCE_ROOT_ENV = "SHUFFLE_SOURCE_ROOT"
DESKTOP_KEY_FILE = "license-key.json"
FIRST_KEY_MARK_PREFIX = "license_first_key:"
FIRST_KEY_ATTEMPTS = 5

_env_key: str | None = None


def capture_env_key(environ=None) -> bool:
    """Take ``LICENSE_KEY`` out of the environment and keep it for
    :func:`activate_first_install_key`. Called once at startup, before any
    agent can start, like ``OWNER_PASSWORD``. Returns whether one was set."""
    global _env_key
    environ = os.environ if environ is None else environ
    value = (environ.pop(LICENSE_KEY_ENV, None) or "").strip()
    if value:
        _env_key = value
    return bool(value)


def _desktop_key() -> str | None:
    """The key the desktop launcher saved at first install, if any."""
    root = os.environ.get(SOURCE_ROOT_ENV) or ""
    if not root or not os.path.isabs(root):
        return None
    try:
        raw = (Path(root) / DESKTOP_KEY_FILE).read_bytes()
        if len(raw) > 4096:
            return None
        key = json.loads(raw).get("key")
    except (OSError, ValueError, AttributeError):
        return None
    return key.strip() if isinstance(key, str) and 0 < len(key.strip()) <= 64 else None


def first_install_key() -> tuple[str, str] | None:
    """(key, where it came from): the environment first, then the desktop file."""
    if _env_key:
        return _env_key, "environment"
    key = _desktop_key()
    return (key, "desktop") if key else None


def _first_key_mark() -> str:
    return FIRST_KEY_MARK_PREFIX + settings.effective_instance_id


def _key_digest(key: str) -> str:
    canonical = "".join(ch for ch in key.upper() if ch.isalnum())
    return hashlib.sha256(canonical.encode()).hexdigest()


async def activate_first_install_key() -> str:
    """Activate the key given at install time, once. Returns what happened:
    none, beta, licensed (this worker already has a license), used (this key
    was tried before), activated, refused, or unavailable (try again later)."""
    global _env_key
    found = first_install_key()
    if not found:
        return "none"
    key, source = found
    if beta_mode():
        return "beta"
    state = await asyncio.to_thread(_load_state)
    if state.get("install_token") or state.get("lease"):
        _env_key = None
        return "licensed"
    digest = _key_digest(key)
    if await asyncio.to_thread(database.get_system_setting, _first_key_mark()) == digest:
        _env_key = None
        return "used"
    try:
        await activate(key)
    except LicenseError as exc:
        if exc.status in (502, 503):
            logger.warning("First-install license key not activated yet: %s", exc.message)
            return "unavailable"
        await asyncio.to_thread(database.set_system_setting, _first_key_mark(), digest)
        _env_key = None
        where = "LICENSE_KEY" if source == "environment" else "the key entered at install"
        message = f"{where} was not accepted: {exc.message}"
        logger.warning("First-install license key refused (%s): %s", source, exc.message)
        async with _state_lock():
            current = await asyncio.to_thread(_load_state)
            if not current.get("lease"):
                current["last_error"] = message[:300]
                await asyncio.to_thread(_save_state, current)
        return "refused"
    await asyncio.to_thread(database.set_system_setting, _first_key_mark(), digest)
    _env_key = None
    logger.info("License activated from the key given at install (%s)", source)
    return "activated"


async def _first_install_activation() -> None:
    """Startup: activate a first-install key, retrying while the server is unreachable."""
    for attempt in range(FIRST_KEY_ATTEMPTS):
        try:
            if await activate_first_install_key() != "unavailable":
                return
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("First-install license activation failed")
            return
        await asyncio.sleep(_retry_delay(attempt + 1))


async def refresh_loop() -> None:
    """Background task: activate a first-install key, then renew the lease
    daily, with jittered backoff on failure."""
    await _first_install_activation()
    while True:
        try:
            if beta_mode():
                await asyncio.sleep(REFRESH_INTERVAL)
                continue
            delay = await asyncio.to_thread(_seconds_until_refresh, time.time())
            await asyncio.sleep(delay + random.uniform(0, 30))
            await refresh()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("License refresh loop error")
            await asyncio.sleep(RETRY_BASE)
