"""Codex / ChatGPT-subscription OAuth token-refresh worker (phase-2 Chunk B.3).

Sits between the pure flow logic (``app/core/codex_oauth.py``) and the encrypted
storage (``app/db/model_providers.set_codex_token_bundle`` / ``get_codex_tokens``).
Responsibilities:

* Decide WHEN a provider's Codex access token needs refreshing:
  - proactively when ``codex_last_refresh`` is older than **8 days** (F3);
  - when the access-token JWT ``exp`` is within **5 minutes** (F3);
  - on demand, exactly once, after an upstream 401 (``refresh_on_401_once`` — the
    future gateway upstream client calls this).
* Serialize refreshes **per provider**, at two levels. The Codex refresh token
  ROTATES, so two concurrent refreshes of the SAME provider race and one gets
  ``refresh_token_reused`` → permanent credential death (plan G9 / risk
  R-refresh; with one account per provider, amendment A1, nothing else stands
  between a double-refresh and a dead credential).
  - An asyncio lock serializes refreshes WITHIN one process.
  - A durable lease on the provider row
    (``codex_refresh_lease_owner`` / ``codex_refresh_lease_expires_at``, claimed
    via ``db.try_claim_codex_refresh``) serializes them ACROSS installs sharing
    one database — the in-process lock is blind to peers (observed 2026-08-16:
    stray dev instances pointed at the production DATABASE_URL all ran this
    worker against the same provider row).
* Decrypt the stored blobs, refresh, RE-ENCRYPT, and persist the rotated bundle
  ATOMICALLY via a single ``set_codex_token_bundle`` call (the DB layer writes
  access+refresh in one UPDATE — no torn write between the two rotated tokens).
* On a permanent failure code (``refresh_token_expired`` / ``_reused`` /
  ``_invalidated``) set ``needs_reauth=TRUE`` (PRESERVING the stored bundle),
  stop refreshing that provider, and surface a reconnect prompt through the
  unified notification funnel.

The periodic trigger is wired into the app's background-task startup
(``app/core/startup.start_background_tasks``), mirroring the usage-limits /
notes-sync workers.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Dict, Optional

from app.core import codex_oauth, encryption
from app.core.config import settings

logger = logging.getLogger(__name__)

# Refresh policy thresholds (F3).
PROACTIVE_REFRESH_AFTER_SECONDS = 8 * 24 * 3600  # 8 days
EXP_SKEW_SECONDS = 5 * 60  # refresh when the access-token exp is within 5 min

# TTL of the durable cross-install refresh lease. Generous on purpose: it
# outlives any plausible refresh POST, so the only case where the TTL matters is
# an install that CRASHED mid-refresh — then a peer may retry after two minutes
# instead of the row staying leased forever.
CLAIM_TTL_SECONDS = 120
# Bounded claim retry for the 401 path only (a caller is blocked on the answer):
# lose the claim → wait → re-read → maybe the holder already rotated for us.
CLAIM_ATTEMPTS_ON_401 = 3
CLAIM_RETRY_SLEEP_SECONDS = 2

# Per-provider refresh locks. Keyed by provider_id so two concurrent refreshes of
# the SAME provider serialize (rotating refresh token — a double POST is fatal),
# while different providers refresh independently. Created lazily; never removed
# (the count is bounded by the number of Codex providers, which is tiny).
_locks: Dict[str, asyncio.Lock] = {}
# Guards creation of a per-provider lock so two coroutines racing to refresh a
# not-yet-seen provider get the SAME lock object (otherwise each would mint its
# own and the serialization would be defeated).
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
    claims = codex_oauth._decode_jwt_payload(access_token)
    exp = claims.get("exp")
    if isinstance(exp, (int, float)):
        try:
            return datetime.fromtimestamp(exp, tz=timezone.utc)
        except (ValueError, OverflowError, OSError):
            return None
    return None


def _needs_refresh(bundle: dict, access_token_plaintext: Optional[str]) -> bool:
    """Decide whether a stored Codex bundle should be proactively refreshed.

    ``bundle`` is the public/non-secret fields from ``get_codex_tokens``;
    ``access_token_plaintext`` is the decrypted access token (for its ``exp``).
    Returns True when either the 8-day proactive interval has elapsed OR the
    access-token ``exp`` is within the 5-minute skew.
    """
    now = _now()
    # Proactive 8-day interval since last refresh.
    last_refresh = _parse_iso(bundle.get("codex_last_refresh"))
    if last_refresh is None:
        return True
    if (now - last_refresh).total_seconds() >= PROACTIVE_REFRESH_AFTER_SECONDS:
        return True
    # Access-token exp within 5 minutes.
    exp = _access_token_exp(access_token_plaintext)
    if exp is not None and (exp - now).total_seconds() <= EXP_SKEW_SECONDS:
        return True
    return False


async def _persist_rotated_bundle(
    provider_id: str,
    new_bundle: codex_oauth.TokenBundle,
    prior_account_id: Optional[str],
) -> None:
    """Re-encrypt and persist the rotated bundle atomically (off the loop)."""
    from app.db.async_database import run_sync
    from app.db import database as db

    enc_access = encryption.encrypt_value(new_bundle.access_token)
    enc_refresh = encryption.encrypt_value(new_bundle.refresh_token)
    # A refresh response may omit the id_token → keep the previously stored
    # account id rather than nulling it.
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
    )


async def _mark_needs_reauth(provider_id: str) -> None:
    """Set ``needs_reauth=TRUE`` while PRESERVING the stored token bundle.

    ``set_codex_token_bundle`` re-writes the blobs, so re-supply the existing
    (still-encrypted) blobs unchanged and flip only the flag. This keeps the last
    known credential around for display/debug while stopping refresh attempts.
    """
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
    )


async def _notify_reauth(provider_id: str) -> None:
    """Surface a reconnect prompt through the unified notification funnel.

    Mirrors ``account_failover._notify`` — best-effort, never raises.
    """
    try:
        from app.services.notification_service import emit_notification
        await emit_notification(
            "admin",
            severity="error",
            message=(
                "A ChatGPT/Codex provider needs to be reconnected — its login "
                "expired and could not be refreshed. Sign in again to restore it."
            ),
            title="Codex reconnect required",
            category="codex_reauth",
            surface="banner",
            persist=True,
        )
    except Exception:  # noqa: BLE001
        logger.debug("codex reauth notification emit failed", exc_info=True)


def _stored_access_differs(
    stored: dict, stale_access_token: Optional[str]
) -> bool:
    """True when the STORED access token is readable and no longer the one that
    got the 401 — i.e. somebody (another coroutine here, or a peer install)
    already rotated the bundle, so no further POST is warranted.

    Returns False when no stale token was supplied or the stored blob cannot be
    decrypted (an undecryptable blob must not be mistaken for a rotation).
    """
    if stale_access_token is None:
        return False
    enc_access = stored.get("codex_access_token")
    try:
        cur_access = encryption.decrypt_value(enc_access) if enc_access else None
    except Exception:  # noqa: BLE001
        cur_access = None
    return cur_access is not None and cur_access != stale_access_token


async def _release_claim(provider_id: str, owner: str) -> None:
    """Drop our durable refresh lease — best-effort, never raises.

    A failed release is not a correctness problem: the lease carries a TTL, so a
    row we cannot clear simply frees itself in ``CLAIM_TTL_SECONDS``.
    """
    from app.db.async_database import run_sync
    from app.db import database as db

    try:
        await run_sync(db.release_codex_refresh_claim, provider_id, owner)
    except Exception:  # noqa: BLE001 — TTL expiry is the backstop
        logger.warning(
            "codex refresh: could not release claim for provider %s "
            "(lease will expire in %ss)", provider_id, CLAIM_TTL_SECONDS,
            exc_info=True,
        )


def _usable_codex_bundle(stored: Optional[dict]) -> bool:
    """A stored bundle we may auto-refresh: has a refresh blob, is chatgpt mode,
    and is not already flagged dead."""
    if not stored or not stored.get("codex_refresh_token"):
        return False
    if stored.get("auth_mode") != "chatgpt":
        return False
    if stored.get("needs_reauth"):
        return False
    return True


async def _do_refresh_locked(provider_id: str, stored: dict, client) -> bool:
    """Perform the actual refresh assuming the per-provider lock is HELD.

    Decrypt the refresh token → ``codex_oauth.refresh_tokens`` (ROTATES) →
    re-encrypt + persist the rotated bundle atomically. Permanent failure codes
    flip ``needs_reauth`` (bundle preserved) + notify — UNLESS the row's
    ``codex_last_refresh`` moved while our POST was in flight, which means a peer
    install rotated the bundle underneath us and the "permanent" failure is
    self-inflicted, not a dead credential. Returns True iff a rotated bundle was
    persisted (or a peer's rotation left us with a fresh one).
    """
    from app.db.async_database import run_sync
    from app.db import database as db

    try:
        refresh_plain = encryption.decrypt_value(stored["codex_refresh_token"])
    except Exception:
        logger.error(
            "codex refresh: could not decrypt refresh token for provider %s "
            "(encryption locked or key mismatch)", provider_id,
        )
        return False

    try:
        new_bundle = await codex_oauth.refresh_tokens(refresh_plain, client=client)
    except codex_oauth.CodexAuthError as e:
        if e.is_permanent:
            # Before condemning the credential, check whether a PEER install
            # rotated the bundle while our POST was in flight: if
            # codex_last_refresh moved, the token we sent was already superseded
            # and "reused" is self-inflicted — the stored credential is fine.
            fresh = await run_sync(db.get_codex_tokens, provider_id)
            if fresh is None:
                logger.warning(
                    "codex refresh: permanent failure (%s) for provider %s, but "
                    "the provider is gone — nothing to mark", e.code, provider_id,
                )
                return False
            if str(fresh.get("codex_last_refresh")) != str(
                stored.get("codex_last_refresh")
            ):
                logger.info(
                    "codex refresh: %s for provider %s, but a peer install "
                    "rotated the bundle in the meantime — the stored credential "
                    "is current; NOT flagging needs_reauth", e.code, provider_id,
                )
                return True
            logger.warning(
                "codex refresh: permanent failure (%s) for provider %s — "
                "marking needs_reauth", e.code, provider_id,
            )
            await _mark_needs_reauth(provider_id)
            await _notify_reauth(provider_id)
            return False
        logger.warning(
            "codex refresh: transient failure (%s) for provider %s", e.code,
            provider_id,
        )
        return False

    await _persist_rotated_bundle(
        provider_id, new_bundle, prior_account_id=stored.get("codex_account_id")
    )
    logger.info("codex refresh: rotated token bundle for provider %s", provider_id)
    return True


async def refresh_provider(
    provider_id: str,
    *,
    force: bool = False,
    client=None,
) -> bool:
    """Refresh one provider's Codex token if needed (or ``force``).

    Serialized by the per-provider lock (in-process) AND the durable lease on the
    provider row (cross-install). Steps: read the stored bundle → decide (unless
    ``force``) → claim the row → re-read → refresh + persist → release. On a
    permanent failure code: mark ``needs_reauth`` (bundle preserved) + notify +
    stop.

    Returns ``True`` if a refresh was performed, ``False`` if skipped (not due,
    no bundle, already flagged ``needs_reauth``, or the claim went to a peer).
    """
    from app.db.async_database import run_sync
    from app.db import database as db

    lock = await _get_lock(provider_id)
    async with lock:
        # Re-read INSIDE the lock: a refresh that completed while we waited may
        # have rotated the token and cleared the due condition. This is what
        # makes a second concurrent caller a no-op instead of a double POST.
        stored = await run_sync(db.get_codex_tokens, provider_id)
        if not _usable_codex_bundle(stored):
            return False

        if not force:
            enc_access = stored.get("codex_access_token")
            try:
                access_plain = (
                    encryption.decrypt_value(enc_access) if enc_access else None
                )
            except Exception:  # noqa: BLE001 — bad access blob shouldn't block refresh
                access_plain = None
            if not _needs_refresh(stored, access_plain):
                return False

        # Durable cross-install claim: the lock above only covers THIS process.
        owner = settings.effective_instance_id
        claimed = await run_sync(
            db.try_claim_codex_refresh, provider_id, owner, CLAIM_TTL_SECONDS
        )
        if not claimed:
            logger.debug(
                "codex refresh: claim lost to a peer install for provider %s — "
                "skipping (the holder is refreshing it)", provider_id,
            )
            return False

        try:
            # Re-read AFTER winning the claim — the cross-install analogue of the
            # re-read-inside-the-lock above: a peer may have refreshed between our
            # read and our claim, so the due condition can already be cleared.
            stored = await run_sync(db.get_codex_tokens, provider_id)
            if not _usable_codex_bundle(stored):
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
    """Refresh-on-401 hook for the (future) gateway upstream client.

    A single forced refresh after an upstream 401, serialized per provider so a
    burst of 401s across concurrent requests does exactly ONE refresh, not N.
    When ``stale_access_token`` is supplied (the token that got the 401), a
    caller who enters the lock AFTER a peer already rotated sees the stored access
    token no longer matches and returns ``True`` WITHOUT a second POST — the
    rotating refresh token means a redundant POST would be wasteful and could
    trip reuse detection. Returns ``True`` when a fresh token is available (we
    rotated it, or a peer did) so the caller retries the upstream once.

    The same reasoning extends across installs via the durable claim: a caller
    that loses the claim waits briefly and re-reads instead of POSTing, because
    the peer holding it is about to rotate the very token we would have sent.
    """
    from app.db.async_database import run_sync
    from app.db import database as db

    lock = await _get_lock(provider_id)
    async with lock:
        stored = await run_sync(db.get_codex_tokens, provider_id)
        if not _usable_codex_bundle(stored):
            return False

        # If a peer 401-handler already refreshed while we waited on the lock, the
        # stored access token will differ from the one that got the 401 — skip the
        # redundant POST and let the caller retry with the fresh token.
        if _stored_access_differs(stored, stale_access_token):
            return True

        # Durable cross-install claim. Unlike the periodic sweep, a 401 handler
        # has a caller waiting on an answer, so losing the claim is worth a short
        # bounded wait: the peer that holds it is refreshing THIS provider, and
        # its rotated token is exactly what our caller needs.
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
            if not _usable_codex_bundle(stored):
                return False
            if _stored_access_differs(stored, stale_access_token):
                # The claim holder rotated: the caller can retry upstream now.
                return True
        if not claimed:
            logger.warning(
                "codex refresh: 401 handler could not claim provider %s after "
                "%d attempts — a peer install holds the refresh lease",
                provider_id, CLAIM_ATTEMPTS_ON_401,
            )
            return False

        try:
            # Re-read AFTER winning the claim: a peer may have rotated between
            # our last read and the claim.
            stored = await run_sync(db.get_codex_tokens, provider_id)
            if not _usable_codex_bundle(stored):
                return False
            if _stored_access_differs(stored, stale_access_token):
                return True
            return await _do_refresh_locked(provider_id, stored, client)
        finally:
            await _release_claim(provider_id, owner)


async def refresh_due_providers(*, client=None) -> int:
    """Scan every enabled Codex provider and refresh those that are due.

    Returns the number of providers actually refreshed. Never raises — a single
    provider's failure is logged and does not stop the sweep.
    """
    from app.db.async_database import run_sync
    from app.db import database as db

    providers = await run_sync(db.get_enabled_model_providers)
    refreshed = 0
    for row in providers or []:
        if row.get("auth_mode") != "chatgpt":
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
                "codex refresh: unexpected error refreshing provider %s", provider_id
            )
    return refreshed
