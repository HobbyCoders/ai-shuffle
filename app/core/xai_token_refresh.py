"""xAI / SuperGrok OAuth token-refresh worker.

Sits between the pure flow logic (``app/core/xai_oauth.py``) and the encrypted
storage (``app/db/model_providers.set_codex_token_bundle`` / ``get_codex_tokens``).
The Codex columns are reused as generic OAuth blobs; Grok enrollment writes
``auth_mode='grok'``.

Responsibilities mirror ``codex_token_refresh``:

* Decide WHEN a Grok provider's access token needs refreshing (8-day proactive
  interval, or JWT ``exp`` within 5 minutes).
* Serialize refreshes per provider via an asyncio lock + the existing durable
  ``codex_refresh_lease_*`` columns.
* Decrypt, refresh via ``xai_oauth.refresh_token``, re-encrypt, persist with
  ``auth_mode='grok'``.
* Permanent failure → ``needs_reauth=True`` (bundle preserved) + notification.

Only Grok-subscription rows (``provider_type=="grok"`` or ``auth_mode=="grok"``)
are scanned. Codex rows are left to ``codex_token_refresh``.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Dict, Optional

from app.core import encryption, xai_oauth
from app.core.config import settings

logger = logging.getLogger(__name__)

PROACTIVE_REFRESH_AFTER_SECONDS = 8 * 24 * 3600  # 8 days
EXP_SKEW_SECONDS = 5 * 60  # refresh when the access-token exp is within 5 min

CLAIM_TTL_SECONDS = 120
CLAIM_ATTEMPTS_ON_401 = 3
CLAIM_RETRY_SLEEP_SECONDS = 2

_locks: Dict[str, asyncio.Lock] = {}
_locks_guard = asyncio.Lock()


async def _get_lock(provider_id: str) -> asyncio.Lock:
    """Return the (stable) refresh lock for a provider, creating it once."""
    lock = _locks.get(provider_id)
    if lock is not None:
        return lock
    async with _locks_guard:
        lock = _locks.get(provider_id)
        if lock is None:
            lock = asyncio.Lock()
            _locks[provider_id] = lock
        return lock


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _parse_iso(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except (ValueError, TypeError):
        return None


def _access_token_exp(access_token: Optional[str]) -> Optional[datetime]:
    """Read the ``exp`` claim from the access-token JWT, if present."""
    if not access_token:
        return None
    claims = xai_oauth._decode_jwt_payload(access_token)
    exp = claims.get("exp")
    if isinstance(exp, (int, float)):
        try:
            return datetime.fromtimestamp(exp, tz=timezone.utc)
        except (ValueError, OverflowError, OSError):
            return None
    return None


def _needs_refresh(bundle: dict, access_token_plaintext: Optional[str]) -> bool:
    """True when the 8-day interval has elapsed or JWT exp is within 5 min."""
    now = _now()
    last_refresh = _parse_iso(bundle.get("codex_last_refresh"))
    if last_refresh is None:
        return True
    if (now - last_refresh).total_seconds() >= PROACTIVE_REFRESH_AFTER_SECONDS:
        return True
    exp = _access_token_exp(access_token_plaintext)
    if exp is not None and (exp - now).total_seconds() <= EXP_SKEW_SECONDS:
        return True
    return False


def _is_grok_subscription_row(row: dict) -> bool:
    return row.get("provider_type") == "grok" or row.get("auth_mode") == "grok"


async def _persist_rotated_bundle(
    provider_id: str,
    new_bundle: xai_oauth.TokenBundle,
    prior_account_id: Optional[str],
) -> None:
    """Re-encrypt and persist the rotated bundle atomically (off the loop)."""
    from app.db.async_database import run_sync
    from app.db import database as db

    enc_access = encryption.encrypt_value(new_bundle.access_token)
    enc_refresh = encryption.encrypt_value(new_bundle.refresh_token)
    account_id = new_bundle.account_id or prior_account_id
    id_token_exp = _access_token_exp(new_bundle.id_token or new_bundle.access_token)
    await run_sync(
        db.set_codex_token_bundle,
        provider_id,
        access_token=enc_access,
        refresh_token=enc_refresh,
        account_id=account_id,
        id_token_expires_at=id_token_exp.isoformat() if id_token_exp else None,
        last_refresh=_now().isoformat(),
        needs_reauth=False,
        auth_mode="grok",
    )


async def _mark_needs_reauth(provider_id: str) -> None:
    """Set ``needs_reauth=TRUE`` while PRESERVING the stored token bundle."""
    from app.db.async_database import run_sync
    from app.db import database as db

    stored = await run_sync(db.get_codex_tokens, provider_id)
    if not stored:
        return
    await run_sync(
        db.set_codex_token_bundle,
        provider_id,
        access_token=stored.get("codex_access_token"),
        refresh_token=stored.get("codex_refresh_token"),
        account_id=stored.get("codex_account_id"),
        id_token_expires_at=stored.get("codex_id_token_expires_at"),
        last_refresh=stored.get("codex_last_refresh"),
        needs_reauth=True,
        auth_mode="grok",
    )


async def _notify_reauth(provider_id: str) -> None:
    """Surface a reconnect prompt through the unified notification funnel."""
    try:
        from app.services.notification_service import emit_notification
        await emit_notification(
            "admin",
            severity="error",
            message=(
                "A Grok provider needs to be reconnected — its SuperGrok login "
                "expired and could not be refreshed. Sign in again to restore it."
            ),
            title="Grok reconnect required",
            category="grok_reauth",
            surface="banner",
            persist=True,
        )
    except Exception:  # noqa: BLE001
        logger.debug("grok reauth notification emit failed", exc_info=True)


def _stored_access_differs(
    stored: dict, stale_access_token: Optional[str]
) -> bool:
    """True when the stored access token is readable and no longer the 401 token."""
    if stale_access_token is None:
        return False
    enc_access = stored.get("codex_access_token")
    try:
        cur_access = encryption.decrypt_value(enc_access) if enc_access else None
    except Exception:  # noqa: BLE001
        cur_access = None
    return cur_access is not None and cur_access != stale_access_token


async def _release_claim(provider_id: str, owner: str) -> None:
    """Drop our durable refresh lease — best-effort, never raises."""
    from app.db.async_database import run_sync
    from app.db import database as db

    try:
        await run_sync(db.release_codex_refresh_claim, provider_id, owner)
    except Exception:  # noqa: BLE001 — TTL expiry is the backstop
        logger.warning(
            "grok refresh: could not release claim for provider %s "
            "(lease will expire in %ss)", provider_id, CLAIM_TTL_SECONDS,
            exc_info=True,
        )


def _usable_grok_bundle(stored: Optional[dict]) -> bool:
    """A stored bundle we may auto-refresh: has a refresh blob, is grok mode,
    and is not already flagged dead."""
    if not stored or not stored.get("codex_refresh_token"):
        return False
    if stored.get("auth_mode") != "grok":
        return False
    if stored.get("needs_reauth"):
        return False
    return True


async def _do_refresh_locked(provider_id: str, stored: dict, client) -> bool:
    """Perform the actual refresh assuming the per-provider lock is HELD."""
    from app.db.async_database import run_sync
    from app.db import database as db

    try:
        refresh_plain = encryption.decrypt_value(stored["codex_refresh_token"])
    except Exception:
        logger.error(
            "grok refresh: could not decrypt refresh token for provider %s "
            "(encryption locked or key mismatch)", provider_id,
        )
        return False

    try:
        new_bundle = await xai_oauth.refresh_token(refresh_plain, client=client)
    except xai_oauth.XaiAuthError as e:
        if e.is_permanent:
            fresh = await run_sync(db.get_codex_tokens, provider_id)
            if fresh is None:
                logger.warning(
                    "grok refresh: permanent failure (%s) for provider %s, but "
                    "the provider is gone — nothing to mark", e.code, provider_id,
                )
                return False
            if str(fresh.get("codex_last_refresh")) != str(
                stored.get("codex_last_refresh")
            ):
                logger.info(
                    "grok refresh: %s for provider %s, but a peer install "
                    "rotated the bundle in the meantime — the stored credential "
                    "is current; NOT flagging needs_reauth", e.code, provider_id,
                )
                return True
            logger.warning(
                "grok refresh: permanent failure (%s) for provider %s — "
                "marking needs_reauth", e.code, provider_id,
            )
            await _mark_needs_reauth(provider_id)
            await _notify_reauth(provider_id)
            return False
        logger.warning(
            "grok refresh: transient failure (%s) for provider %s", e.code,
            provider_id,
        )
        return False

    await _persist_rotated_bundle(
        provider_id, new_bundle, prior_account_id=stored.get("codex_account_id")
    )
    logger.info("grok refresh: rotated token bundle for provider %s", provider_id)
    return True


async def refresh_provider(
    provider_id: str,
    *,
    force: bool = False,
    client=None,
) -> bool:
    """Refresh one provider's Grok token if needed (or ``force``).

    Returns ``True`` if a refresh was performed, ``False`` if skipped.
    """
    from app.db.async_database import run_sync
    from app.db import database as db

    lock = await _get_lock(provider_id)
    async with lock:
        stored = await run_sync(db.get_codex_tokens, provider_id)
        if not _usable_grok_bundle(stored):
            return False

        if not force:
            enc_access = stored.get("codex_access_token")
            try:
                access_plain = (
                    encryption.decrypt_value(enc_access) if enc_access else None
                )
            except Exception:  # noqa: BLE001
                access_plain = None
            if not _needs_refresh(stored, access_plain):
                return False

        owner = settings.effective_instance_id
        claimed = await run_sync(
            db.try_claim_codex_refresh, provider_id, owner, CLAIM_TTL_SECONDS
        )
        if not claimed:
            logger.debug(
                "grok refresh: claim lost to a peer install for provider %s — "
                "skipping (the holder is refreshing it)", provider_id,
            )
            return False

        try:
            stored = await run_sync(db.get_codex_tokens, provider_id)
            if not _usable_grok_bundle(stored):
                return False
            if not force:
                enc_access = stored.get("codex_access_token")
                try:
                    access_plain = (
                        encryption.decrypt_value(enc_access) if enc_access else None
                    )
                except Exception:  # noqa: BLE001
                    access_plain = None
                if not _needs_refresh(stored, access_plain):
                    return False

            return await _do_refresh_locked(provider_id, stored, client)
        finally:
            await _release_claim(provider_id, owner)


async def refresh_on_401_once(
    provider_id: str,
    *,
    stale_access_token: Optional[str] = None,
    client=None,
) -> bool:
    """Refresh-on-401 hook for GrokSubscriptionAuth.

    A single forced refresh after an upstream 401, serialized per provider.
    Returns ``True`` when a fresh token is available (we rotated it, or a peer
    did) so the caller retries the upstream once.
    """
    from app.db.async_database import run_sync
    from app.db import database as db

    lock = await _get_lock(provider_id)
    async with lock:
        stored = await run_sync(db.get_codex_tokens, provider_id)
        if not _usable_grok_bundle(stored):
            return False

        if _stored_access_differs(stored, stale_access_token):
            return True

        owner = settings.effective_instance_id
        claimed = False
        for attempt in range(CLAIM_ATTEMPTS_ON_401):
            claimed = await run_sync(
                db.try_claim_codex_refresh, provider_id, owner, CLAIM_TTL_SECONDS
            )
            if claimed:
                break
            if attempt == CLAIM_ATTEMPTS_ON_401 - 1:
                break
            await asyncio.sleep(CLAIM_RETRY_SLEEP_SECONDS)
            stored = await run_sync(db.get_codex_tokens, provider_id)
            if not _usable_grok_bundle(stored):
                return False
            if _stored_access_differs(stored, stale_access_token):
                return True
        if not claimed:
            logger.warning(
                "grok refresh: 401 handler could not claim provider %s after "
                "%d attempts — a peer install holds the refresh lease",
                provider_id, CLAIM_ATTEMPTS_ON_401,
            )
            return False

        try:
            stored = await run_sync(db.get_codex_tokens, provider_id)
            if not _usable_grok_bundle(stored):
                return False
            if _stored_access_differs(stored, stale_access_token):
                return True
            return await _do_refresh_locked(provider_id, stored, client)
        finally:
            await _release_claim(provider_id, owner)


async def refresh_due_providers(*, client=None) -> int:
    """Scan every enabled Grok-subscription provider and refresh those due.

    Returns the number of providers actually refreshed. Never raises.
    """
    from app.db.async_database import run_sync
    from app.db import database as db

    providers = await run_sync(db.get_enabled_model_providers)
    refreshed = 0
    for row in providers or []:
        if not _is_grok_subscription_row(row):
            continue
        if row.get("needs_reauth"):
            continue
        provider_id = row.get("id")
        if not provider_id:
            continue
        try:
            if await refresh_provider(provider_id, client=client):
                refreshed += 1
        except Exception:  # noqa: BLE001 — one provider must not sink the sweep
            logger.exception(
                "grok refresh: unexpected error refreshing provider %s", provider_id
            )
    return refreshed
