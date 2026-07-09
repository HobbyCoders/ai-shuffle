"""Multiple Anthropic (Claude Code OAuth) accounts with in-app switching.

This service owns the DB-backed account registry and the logic that keeps the
single on-disk ``~/.claude/.credentials.json`` file in sync with the currently
active account row. The subtle part is SYNC-BACK: the credentials file is a
shared mutable resource — the app writes it on login/refresh/switch, but CLI
subprocesses spawned by the SDK ALSO self-refresh it. If we blindly overwrote
the DB row from the file we could clobber a fresher refresh token, and if we
blindly overwrote the file from the DB row we could clobber a rotation the
subprocess just performed. The design below reconciles the two directions
carefully and, crucially, NEVER raises from the hot paths (it runs downstream
of token refresh and on hot unauthenticated status endpoints).

Account list is shared across installs (``claude_accounts`` table); the active
pointer is per-install (``system_settings`` key ``claude_active_account:{id}``).
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
import uuid
from typing import Any, Dict, Optional

from app.db import database as db
from app.core import encryption
from app.core.oauth import direct_oauth

logger = logging.getLogger(__name__)


# Module-level mtime cache for the credentials file. sync_back_active_account()
# is invoked on every hot status endpoint; this lets it short-circuit with a
# single stat() when the file hasn't changed since the last successful check.
_last_synced_mtime: Optional[float] = None

# Rate-limit the "encryption not ready, marking sync_pending" warning so a hot
# status endpoint doesn't spam the log.
_last_pending_warn: float = 0.0
_PENDING_WARN_INTERVAL = 300.0  # seconds

# Throttle for last_used_at stamping. record_active_account_used() runs from the
# hot sync-back path (every status poll / after every token refresh), so we cap
# the DB write to at most once per interval. This is a write-rate optimization
# only — the authoritative last_used_at value lives in the DB row, so losing this
# in-memory stamp on restart merely means the next use re-stamps immediately.
_last_used_stamp_monotonic: Optional[float] = None
_USED_STAMP_THROTTLE = 60.0  # seconds

# Suppresses sync_back during a deliberate switch write. switch_account writes
# the INCOMING account's credentials to disk BEFORE it updates the active
# pointer; the post-write hook (sync_back_active_account) would otherwise fire
# with pointer still = the OUTGOING account and clobber that account's row with
# the incoming account's tokens. This code is synchronous single-threaded, so a
# plain module flag toggled in a try/finally is sufficient (no lock needed).
_suppress_sync_back: bool = False


def _active_pointer_key() -> str:
    """The per-install system_settings key holding the active account id."""
    from app.core.config import settings
    return f"claude_active_account:{settings.effective_instance_id}"


def get_active_account_id() -> Optional[str]:
    """Return this install's active Claude account id, or None."""
    try:
        return db.get_system_setting(_active_pointer_key())
    except Exception as e:  # noqa: BLE001
        logger.warning(f"Could not read active Claude account pointer: {e}")
        return None


def _set_active_account_id(account_id: Optional[str]) -> None:
    key = _active_pointer_key()
    if account_id is None:
        db.delete_system_setting(key)
    else:
        db.set_system_setting(key, account_id)


def _read_file_oauth() -> Optional[Dict[str, Any]]:
    """Read the ``claudeAiOauth`` object from the credentials file, or None.

    Reuses direct_oauth's reader so behavior stays identical.
    """
    return direct_oauth._read_oauth_data()


def _credentials_file_path():
    return direct_oauth.config_dir / ".credentials.json"


def _file_mtime() -> Optional[float]:
    try:
        return _credentials_file_path().stat().st_mtime
    except OSError:
        return None


def _fingerprint(oauth_dict: Dict[str, Any]) -> str:
    """sha256 hex of the canonical (sorted-keys) JSON of the oauth object."""
    return hashlib.sha256(
        json.dumps(oauth_dict, sort_keys=True).encode()
    ).hexdigest()


def _oauth_expires_at(oauth_dict: Dict[str, Any]) -> Optional[int]:
    exp = oauth_dict.get("expiresAt")
    try:
        return int(exp) if exp is not None else None
    except (TypeError, ValueError):
        return None


def sync_back_active_account(record_usage: bool = True) -> str:
    """Reconcile the on-disk credentials file back into the active account row.

    Returns one of: ``"synced"``, ``"clean"``, ``"pending"``, ``"no_active"``,
    ``"no_file"``. NEVER raises (runs downstream of token refresh and on hot
    status endpoints). The whole body is wrapped so any DB/unexpected error is
    logged and swallowed.

    ``record_usage`` also stamps the active account's ``last_used_at`` (throttled)
    when the file holds a live credential for it. The pre-switch sync-back passes
    ``record_usage=False`` so switching stamps only the INCOMING account, not the
    one being switched away from.
    """
    global _last_synced_mtime, _last_pending_warn
    if _suppress_sync_back:
        # A deliberate switch write is in flight; the pointer may not yet name
        # the account whose credentials are on disk. Skip to avoid clobbering
        # the outgoing account's row.
        return "clean"
    try:
        active_id = get_active_account_id()
        if not active_id:
            # Legacy single-account mode — nothing to sync back.
            return "no_active"

        oauth_dict = _read_file_oauth()
        if not oauth_dict or not oauth_dict.get("accessToken"):
            # File missing/unreadable/empty — NEVER overwrite the row with
            # nothing (a force_reauth may be mid-flight, having deleted it).
            return "no_file"

        # The active account has a live credential on disk — it is the account
        # currently in use on this install. Record usage (throttled) so
        # last_used_at reflects real activity, not just the last manual switch.
        # Only the ACTIVE account's timestamp advances; siblings keep the time
        # they were last active.
        if record_usage:
            record_active_account_used(active_id)

        # Fast-path: file unchanged since our last sync check.
        mtime = _file_mtime()
        if mtime is not None and _last_synced_mtime is not None and mtime == _last_synced_mtime:
            return "clean"

        row = db.get_claude_account(active_id)
        if row is None:
            # Active pointer references a row that no longer exists — don't
            # fabricate one here; leave it to seed/switch to reconcile.
            return "no_active"

        file_fp = _fingerprint(oauth_dict)
        if file_fp == row.get("credential_fingerprint"):
            # Row already matches the file — stamp the cache and short-circuit.
            _last_synced_mtime = mtime
            return "clean"

        # Diverged. Persist the file's credential back into the row.
        if not encryption.is_encryption_ready():
            # Can't encrypt the new blob. Mark the row so we (or another install
            # with the key) can reconcile later. sync_pending is a plaintext
            # column so this works without the key.
            try:
                db.mark_claude_account_sync_pending(active_id)
            except Exception as e:  # noqa: BLE001
                logger.warning(f"Could not mark account sync_pending: {e}")
            now = time.monotonic()
            if now - _last_pending_warn > _PENDING_WARN_INTERVAL:
                _last_pending_warn = now
                logger.warning(
                    "Claude credentials rotated but encryption is locked — "
                    "cannot sync back to active account row (marked sync_pending)."
                )
            return "pending"

        encrypted = encryption.encrypt_value(json.dumps(oauth_dict))
        db.update_claude_account_credentials(
            active_id,
            credentials=encrypted,
            credential_fingerprint=file_fp,
            expires_at=_oauth_expires_at(oauth_dict),
            has_refresh_token=bool(oauth_dict.get("refreshToken")),
            clear_sync_pending=True,
        )
        _last_synced_mtime = mtime
        logger.info("Synced rotated Claude credentials back to active account row")
        return "synced"

    except Exception as e:  # noqa: BLE001 — must never break refresh/status
        logger.warning(f"sync_back_active_account failed (ignored): {e}")
        return "clean"


def record_active_account_used(account_id: Optional[str] = None) -> None:
    """Stamp the active account's ``last_used_at`` to now (throttled).

    Called from the in-use path (sync-back, which runs on hot status endpoints
    and downstream of every token refresh) so the displayed "last used" reflects
    real activity rather than only the last manual switch. ONLY the active
    account is stamped, so siblings keep the time they were last active. The
    throttle bounds the write rate; the value itself persists in the DB. Never
    raises (shares the hot path with refresh/status).
    """
    global _last_used_stamp_monotonic
    now = time.monotonic()
    if (
        _last_used_stamp_monotonic is not None
        and now - _last_used_stamp_monotonic < _USED_STAMP_THROTTLE
    ):
        return
    try:
        active_id = account_id or get_active_account_id()
        if not active_id:
            return
        db.touch_claude_account_last_used(active_id)
        _last_used_stamp_monotonic = now
    except Exception as e:  # noqa: BLE001 — must never break refresh/status
        logger.debug(f"record_active_account_used failed (ignored): {e}")


def _reset_mtime_cache() -> None:
    """Stamp the mtime cache to the current file mtime (after a known write)."""
    global _last_synced_mtime
    _last_synced_mtime = _file_mtime()


def _row_credentials_to_dict(row: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Decrypt a row's credentials blob into the oauth dict, or None on failure."""
    blob = row.get("credentials")
    if not blob:
        return None
    try:
        plaintext = encryption.decrypt_value(blob)
        return json.loads(plaintext)
    except Exception as e:  # noqa: BLE001
        logger.error(f"Failed to decrypt Claude account credentials: {e}")
        return None


def switch_account(account_id: str) -> Dict[str, Any]:
    """Make ``account_id`` the active account, writing its credentials to disk.

    Returns ``{"success": bool, ...}``. On success includes
    ``active_account_id``. On failure includes an ``error`` code the API layer
    maps to an HTTP status.
    """
    global _suppress_sync_back
    if not encryption.is_encryption_ready():
        return {"success": False, "error": "encryption_locked"}

    row = db.get_claude_account(account_id, include_credentials=True)
    if row is None:
        return {"success": False, "error": "not_found"}

    # 1. Persist the OUTGOING account's rotated refresh token FIRST, before we
    #    overwrite the file. Otherwise a rotation performed since the last sync
    #    would be lost. (This runs BEFORE the suppression guard is set.)
    #    NOTE: this path deliberately bypasses ensure_fresh_credentials's
    #    asyncio lock. Worst case a concurrent refresh overwrites the file with
    #    the outgoing account's refreshed token; the next status sync-back
    #    reconciles it — transient, accepted for v1.
    #    record_usage=False: the explicit touch below stamps the INCOMING account;
    #    the outgoing account should not be re-stamped as "used" by the switch.
    sync_back_active_account(record_usage=False)

    # 2. Decrypt the target account's stored credential.
    oauth_dict = _row_credentials_to_dict(row)
    if oauth_dict is None:
        return {"success": False, "error": "decrypt_failed"}

    # 3. Write it to the on-disk credentials file VERBATIM. The existing
    #    session_pool credentials-rotation machinery (claude_credentials_mtime →
    #    credentials_rotated → refresh_subprocess_credentials) picks up the
    #    file mtime change and propagates it to live SDK subprocesses.
    #    ACCEPTED v1 LIMITATION: pool._copy_if_newer compares expiry timestamps;
    #    switching to an account whose token expiry is OLDER than the outgoing
    #    one could momentarily be treated as "not newer" by that heuristic. This
    #    is acceptable for v1 (the next status check / refresh reconciles it).
    #
    #    The write fires the post-write hook (sync_back_active_account). At this
    #    point the active pointer STILL names the OUTGOING account, so an
    #    unsuppressed hook would see file=incoming, pointer=outgoing, fingerprint
    #    mismatch → write the incoming credentials into the OUTGOING row (silent
    #    cross-account corruption). Suppress sync-back across the write and the
    #    pointer update; clear in finally.
    _suppress_sync_back = True
    try:
        if not direct_oauth.write_credentials_dict(oauth_dict):
            return {"success": False, "error": "write_failed"}

        # 4. Invalidate direct_oauth's validity cache so the next status check
        #    reads the freshly written credential rather than a stale verdict.
        direct_oauth.invalidate_credentials_cache()

        # 5. Update the per-install active pointer and usage stamp, and reset our
        #    mtime cache to the file we just wrote (so the next status sync-back
        #    treats it as clean).
        _set_active_account_id(account_id)
        try:
            db.touch_claude_account_last_used(account_id)
        except Exception as e:  # noqa: BLE001
            logger.warning(f"Could not stamp last_used for account: {e}")
        _reset_mtime_cache()
    finally:
        _suppress_sync_back = False

    # 6. Invalidate any in-flight usage poll (started against the previous
    #    account) so the limits indicator re-polls the new account cleanly.
    #    Best-effort + lazy import: never let this break the switch.
    try:
        from app.core.usage_limits import usage_limits_monitor
        usage_limits_monitor.bump_generation()
    except Exception as e:  # noqa: BLE001
        logger.debug(f"Could not bump usage generation on switch: {e}")

    return {"success": True, "active_account_id": account_id}


async def add_account(
    code: str,
    state: str,
    name: Optional[str] = None,
    make_active: bool = False,
) -> Dict[str, Any]:
    """Exchange an OAuth code and store it as a NEW account (never writes file).

    Adding an account exchanges the code directly WITHOUT touching the on-disk
    credentials file, so the currently active account is never clobbered. If
    ``make_active`` is set, the switch path then writes the new credential to
    disk exactly like an explicit switch.
    """
    if not encryption.is_encryption_ready():
        return {"success": False, "error": "encryption_locked"}

    tokens, error = await direct_oauth.exchange_code_for_tokens(code, state)
    if error or tokens is None:
        return {"success": False, "error": error or "exchange_failed"}

    oauth_dict = {
        "accessToken": tokens.access_token,
        "refreshToken": tokens.refresh_token,
        "expiresAt": tokens.expires_at,
        "scopes": tokens.scopes,
        "subscriptionType": None,
    }
    fingerprint = _fingerprint(oauth_dict)
    encrypted = encryption.encrypt_value(json.dumps(oauth_dict))

    account_id = uuid.uuid4().hex
    account_name = name or tokens.account_email or f"Account {db.count_claude_accounts() + 1}"

    row = db.create_claude_account(
        account_id=account_id,
        name=account_name,
        credentials=encrypted,
        credential_fingerprint=fingerprint,
        expires_at=tokens.expires_at,
        has_refresh_token=bool(tokens.refresh_token),
        email=tokens.account_email,
        subscription_type=None,
    )

    result: Dict[str, Any] = {"success": True, "account": row}

    if make_active:
        switch = switch_account(account_id)
        if not switch.get("success"):
            # The row exists; surface the switch error but keep the account.
            result["switch_error"] = switch.get("error")
        else:
            result["active_account_id"] = account_id

    return result


def seed_from_credentials_file() -> Optional[str]:
    """Create a "Default account" row from an existing credentials file.

    Idempotent, never raises. Only runs when the table is empty AND the file has
    an access token AND encryption is ready (we won't store plaintext). Sets the
    per-install active pointer to the seeded row. Returns the new account id or
    None.
    """
    try:
        if not encryption.is_encryption_ready():
            return None
        if db.count_claude_accounts() > 0:
            return None
        oauth_dict = _read_file_oauth()
        if not oauth_dict or not oauth_dict.get("accessToken"):
            return None

        fingerprint = _fingerprint(oauth_dict)
        encrypted = encryption.encrypt_value(json.dumps(oauth_dict))
        account_id = uuid.uuid4().hex

        db.create_claude_account(
            account_id=account_id,
            name="Default account",
            credentials=encrypted,
            credential_fingerprint=fingerprint,
            expires_at=_oauth_expires_at(oauth_dict),
            has_refresh_token=bool(oauth_dict.get("refreshToken")),
            email=None,
            subscription_type=oauth_dict.get("subscriptionType"),
        )
        _set_active_account_id(account_id)
        _reset_mtime_cache()
        logger.info("Seeded default Claude account from existing credentials file")
        return account_id
    except Exception as e:  # noqa: BLE001
        logger.warning(f"seed_from_credentials_file failed (ignored): {e}")
        return None


def rename_account(account_id: str, name: str) -> Dict[str, Any]:
    """Rename an account. Returns {"success", account?|error}."""
    row = db.rename_claude_account(account_id, name)
    if row is None:
        return {"success": False, "error": "not_found"}
    return {"success": True, "account": row}


def delete_account(account_id: str) -> Dict[str, Any]:
    """Delete an account with the active-account rules.

    - Deleting the active account while OTHER accounts exist → error
      ``active_with_siblings`` (API maps to 409 "switch first").
    - Deleting the SOLE active account → allowed; acts as a logout (delete the
      credentials file, clear the pointer, delete the row).
    - Deleting an inactive account → just delete the row.
    """
    row = db.get_claude_account(account_id)
    if row is None:
        return {"success": False, "error": "not_found"}

    # ACCEPTED cross-install TOCTOU: count-then-act on a shared DB (same class as
    # the automation reconciler). Deleting a row that ANOTHER install has active
    # leaves that install a ghost pointer, which degrades gracefully — its
    # sync_back returns the no_active-equivalent and the credentials file is
    # never touched.
    active_id = get_active_account_id()
    is_active = active_id == account_id
    total = db.count_claude_accounts()

    if is_active and total > 1:
        return {"success": False, "error": "active_with_siblings"}

    if is_active and total == 1:
        # Sole active account → logout semantics.
        creds_file = _credentials_file_path()
        try:
            if creds_file.exists():
                creds_file.unlink()
        except OSError as e:
            logger.warning(f"Could not delete credentials file on account delete: {e}")
        _set_active_account_id(None)
        direct_oauth.invalidate_credentials_cache()

    db.delete_claude_account(account_id)
    return {"success": True}


def get_active_account_summary() -> Optional[Dict[str, Any]]:
    """Return ``{"id", "name"}`` for the active account, or None."""
    active_id = get_active_account_id()
    if not active_id:
        return None
    row = db.get_claude_account(active_id)
    if row is None:
        return None
    return {"id": row["id"], "name": row["name"]}
