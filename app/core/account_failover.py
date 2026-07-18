"""Auto-switch the active Claude account when a turn hits a usage limit.

When a Claude turn ends with a usage-limit API error (``api_error_status ==
429``) and the per-install toggle is enabled, this module rotates the active
saved Claude account to another saved account and tells the caller to retry the
turn on a resumed session — so the user never has to intervene.

This is the single home for:

* the toggle (a per-install ``system_settings`` string KV, mirroring the
  active-account pointer key in :mod:`app.core.claude_accounts`),
* the selection policy (:func:`_select_next_account` — pure and unit-testable),
* the concurrency guard (a module lock + a short debounce so two sessions that
  429 back-to-back rotate credentials exactly once), and
* the switch-and-prepare-retry seam (:func:`attempt_failover`).

Cross-install note: an offloaded / cloud turn runs this seam on the WORKER
install (the machine that actually spawns the SDK subprocess and owns the
on-disk credentials), gated by that install's own toggle — the toggle is
per-install by construction.

Out of scope: proactive utilization-based switching before a limit is hit; this
module reacts only to a realized 429.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Set

from app.core import claude_accounts
from app.core import encryption
from app.core.usage_limits import schedule_usage_refresh
from app.db import claude_accounts as accounts_db
from app.db import settings as settings_db
from app.db import usage_snapshots as usage_db
from app.db.async_database import run_sync

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Concurrency guard (module-level, per-process)
# ---------------------------------------------------------------------------
# A single lock serializes the switch decision so two sessions that 429 in the
# same instant don't each rotate credentials. ``_last_switch_at`` (monotonic)
# plus the debounce window lets a session that failed just AFTER another session
# already rotated piggyback on the fresh account instead of rotating again.
_switch_lock = asyncio.Lock()
_last_switch_at: float = 0.0
_DEBOUNCE_SECONDS = 15.0


# ---------------------------------------------------------------------------
# Toggle storage (per-install system_settings KV)
# ---------------------------------------------------------------------------

def _toggle_key() -> str:
    """The per-install system_settings key holding the auto-switch toggle."""
    from app.core.config import settings
    return f"claude_auto_switch_on_limit:{settings.effective_instance_id}"


def get_auto_switch_enabled() -> bool:
    """Return whether auto-switch-on-limit is enabled for this install.

    Absent key → ``False`` (default OFF). Never raises.
    """
    try:
        return settings_db.get_system_setting(_toggle_key()) == "true"
    except Exception as e:  # noqa: BLE001
        logger.warning(f"Could not read auto-switch toggle: {e}")
        return False


def set_auto_switch_enabled(enabled: bool) -> None:
    """Persist the auto-switch-on-limit toggle for this install."""
    settings_db.set_system_setting(_toggle_key(), "true" if enabled else "false")


# ---------------------------------------------------------------------------
# Error classification
# ---------------------------------------------------------------------------

def is_usage_limit_error(metadata: Optional[Dict[str, Any]]) -> bool:
    """True iff this turn's metadata carries a 429 usage-limit API error.

    Handles ``api_error_status`` arriving as an int or a numeric string. Never
    raises — a malformed metadata blob simply yields ``False``.
    """
    if not metadata:
        return False
    try:
        return int(metadata.get("api_error_status")) == 429
    except (TypeError, ValueError):
        return False


# ---------------------------------------------------------------------------
# Eligibility
# ---------------------------------------------------------------------------

def _session_uses_provider(session_id: Optional[str]) -> bool:
    """True when the session's profile is bound to a non-Anthropic model provider
    (``config.provider`` is set).

    R6 (binding): account failover rotates Claude *accounts* on an Anthropic 429.
    A provider session's 429 comes from the gateway, not from an Anthropic-account
    limit — switching accounts + evicting the session would be pure damage (and
    the switched account's credentials are irrelevant to the gateway). This gate
    lets ``attempt_failover`` no-op for provider sessions. Never raises; on any
    lookup failure it treats the session as native (fail toward existing
    behavior).
    """
    if not session_id:
        return False
    try:
        # Prefer the LIVE pooled subprocess's env over the profile config: a
        # mid-session profile edit (add/remove provider) would otherwise invert
        # the decision — removing a provider mid-session lets a gateway 429
        # rotate Claude accounts + evict the pooled client, and adding one
        # suppresses legitimate native failover. peek() = no LRU promotion.
        from app.core.session_pool import get_session_pool
        state = get_session_pool().peek(session_id)
        if state and state.connect_options:
            env = state.connect_options.env or {}
            return "ANTHROPIC_BASE_URL" in env  # KEY PRESENCE (provider overlay always sets it)

        # Not pooled — fall back to the profile config.
        from app.db import database
        session = database.get_session(session_id)
        if not session:
            return False
        profile_id = session.get("profile_id")
        if not profile_id:
            return False
        profile = database.get_profile(profile_id)
        config = (profile or {}).get("config") or {}
        return bool(config.get("provider"))
    except Exception as e:  # noqa: BLE001 — provider check must never break failover
        logger.debug(
            "provider-session check failed for %s (treating as native): %s",
            session_id, e,
        )
        return False


def failover_eligible() -> bool:
    """Whether a failover could run right now (cheap pre-check).

    Enabled AND encryption ready (we must decrypt the target credential) AND at
    least two saved accounts exist (there has to be somewhere to switch to).
    Never raises.
    """
    try:
        if not get_auto_switch_enabled():
            return False
        if not encryption.is_encryption_ready():
            return False
        return accounts_db.count_claude_accounts() >= 2
    except Exception as e:  # noqa: BLE001
        logger.warning(f"failover_eligible check failed (treating as ineligible): {e}")
        return False


# ---------------------------------------------------------------------------
# Selection policy (pure, unit-testable)
# ---------------------------------------------------------------------------

def _parse_ts(value: Any) -> Optional[datetime]:
    """Best-effort parse of an ISO-8601 / epoch timestamp to an aware datetime."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        # Heuristic: treat very large numbers as epoch milliseconds.
        secs = value / 1000.0 if value > 1e12 else float(value)
        try:
            return datetime.fromtimestamp(secs, tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None
    if isinstance(value, str):
        raw = value.strip()
        if not raw:
            return None
        try:
            dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            return None
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    return None


def _window_is_exhausting(window: Dict[str, Any], now: datetime) -> bool:
    """A window that currently blocks usage: at/over 100% (or rejected) and its
    reset is still in the future."""
    if not isinstance(window, dict):
        return False
    util = window.get("utilization")
    status = window.get("status")
    try:
        exhausted = (util is not None and float(util) >= 100.0)
    except (TypeError, ValueError):
        exhausted = False
    if status == "rejected":
        exhausted = True
    if not exhausted:
        return False
    resets_at = _parse_ts(window.get("resets_at"))
    # No reset time → treat as currently blocking (conservative).
    if resets_at is None:
        return True
    return resets_at > now


def _can_authenticate(row: Dict[str, Any], now: datetime) -> bool:
    """False only when the token is definitely dead: expired AND no refresh token."""
    has_refresh = bool(row.get("has_refresh_token"))
    if has_refresh:
        return True
    expires = _parse_ts(row.get("expires_at"))
    if expires is None:
        # Unknown expiry with no refresh token — give it the benefit of the doubt.
        return True
    return expires > now


def _select_next_account(attempted: Set[str]) -> Optional[Dict[str, Any]]:
    """Pick the best account to switch to, or None if there is no candidate.

    Steps:
      1. candidates = all accounts minus ``attempted`` ids
      2. drop rows that cannot authenticate (expired AND no refresh token)
      3. rank: (0) has a usage snapshot with NO currently-exhausting window;
               (1) no snapshot data at all; (2) known-exhausted (still eligible)
      4. tie-break within a rank: oldest ``last_used_at`` first (LRU)
    """
    now = datetime.now(timezone.utc)

    rows = accounts_db.get_all_claude_accounts()

    # Build newest-snapshot-per-account map. There is at most one snapshot per
    # install; several installs may report the same account, so keep the one
    # with the largest fetched_at.
    snapshots_by_account: Dict[str, Dict[str, Any]] = {}
    try:
        for snap in usage_db.get_all_usage_snapshots():
            acct = snap.get("account_id")
            if not acct:
                continue
            existing = snapshots_by_account.get(acct)
            if existing is None or (snap.get("fetched_at") or 0) >= (existing.get("fetched_at") or 0):
                snapshots_by_account[acct] = snap
    except Exception as e:  # noqa: BLE001
        logger.debug(f"Could not load usage snapshots for selection: {e}")

    def _rank(row: Dict[str, Any]) -> int:
        snap = snapshots_by_account.get(row.get("id"))
        if snap is None:
            return 1  # unknown — no snapshot data
        windows = snap.get("windows") or []
        if any(_window_is_exhausting(w, now) for w in windows):
            return 2  # known-exhausted (kept eligible: snapshot may be stale)
        return 0  # snapshot exists and shows headroom

    def _last_used_key(row: Dict[str, Any]) -> float:
        dt = _parse_ts(row.get("last_used_at"))
        # Never-used (None) sorts oldest → most eligible for LRU.
        return dt.timestamp() if dt is not None else 0.0

    candidates = [
        r for r in rows
        if r.get("id") not in attempted and _can_authenticate(r, now)
    ]
    if not candidates:
        return None

    candidates.sort(key=lambda r: (_rank(r), _last_used_key(r)))
    return candidates[0]


# ---------------------------------------------------------------------------
# Failover decision + execution
# ---------------------------------------------------------------------------

@dataclass
class FailoverDecision:
    retry: bool
    account_id: Optional[str] = None
    account_name: Optional[str] = None
    switched: bool = False


async def _notify(
    api_user_id: Optional[str],
    session_id: Optional[str],
    *,
    severity: str,
    message: str,
) -> None:
    """Emit a user-facing notification through the unified funnel. Never raises."""
    try:
        from app.services.notification_service import emit_notification
        await emit_notification(
            api_user_id or "admin",
            severity=severity,
            message=message,
            category="account_failover",
            surface="toast",
            session_id=session_id,
            persist=True,
        )
    except Exception:  # noqa: BLE001
        logger.debug("account_failover notification emit failed", exc_info=True)


async def _evict_pooled_session(session_id: Optional[str]) -> None:
    """Remove the session's pooled SDK client so the retry respawns with the
    freshly-written host credentials.

    In-place credential refresh is unreliable: the pool's
    ``refresh_subprocess_credentials._copy_if_newer`` skips propagation when the
    incoming ``expiresAt`` is <= the current one, which is exactly what happens
    when switching to an account whose token expiry is older than the exhausted
    one. Evicting the client forces a clean respawn that reads the file fresh.
    """
    if not session_id:
        return
    try:
        from app.core.session_pool import get_session_pool
        await get_session_pool().remove_broken(session_id)
    except Exception as e:  # noqa: BLE001
        logger.warning(f"Could not evict pooled session {session_id} on failover: {e}")


async def attempt_failover(
    session_id: Optional[str],
    api_user_id: Optional[str],
    attempted: Set[str],
) -> FailoverDecision:
    """Try to rotate the active account and prepare a retry of the current turn.

    ``attempted`` is the per-turn set of account ids this turn has already tried
    (and 429'd on). It is MUTATED here (the active account and the chosen target
    are added) so the caller's loop terminates: every rotation removes one more
    candidate, and when none remain we stop.

    Returns a :class:`FailoverDecision`. ``retry=True`` tells the caller to
    re-run the turn on the (now credential-switched or piggybacked) session.
    """
    # R6: never rotate Claude accounts for a non-Anthropic provider session. A
    # gateway 429 is not an Anthropic-account limit — switching accounts and
    # evicting the pooled client would strand the user for no benefit. Runs the
    # sync DB reads off the event loop.
    if await run_sync(_session_uses_provider, session_id):
        logger.info(
            "Failover skipped for session %s: bound to a non-Anthropic model "
            "provider — a gateway 429 is not an Anthropic-account limit, so not "
            "switching accounts.",
            session_id,
        )
        return FailoverDecision(retry=False)

    # Unified eligibility gate (enabled + encryption ready + >=2 accounts),
    # matching query_engine's toast-suppression check so a single-account
    # install with the toggle on returns silently here (only the generic
    # rate-limit toast shows) instead of emitting a contradictory "all accounts
    # exhausted" toast. Runs the sync DB reads off the event loop.
    if not await run_sync(failover_eligible):
        return FailoverDecision(retry=False)

    global _last_switch_at

    async with _switch_lock:
        current_active = await run_sync(claude_accounts.get_active_account_id)
        now = time.monotonic()
        debounced = (now - _last_switch_at) < _DEBOUNCE_SECONDS

        # Piggyback path: another session already rotated creds within the
        # debounce window, and the current active account is one THIS turn has
        # not yet failed on. Don't switch again — just evict our pooled client so
        # the retry picks up the account that other session just switched to.
        if debounced and current_active and current_active not in attempted:
            # Piggybacking IS trying this account: record it so that if the
            # retry 429s too, the next call switches instead of piggybacking
            # onto the same exhausted account until the debounce expires.
            attempted.add(current_active)
            target = await run_sync(accounts_db.get_claude_account, current_active)
            name = target.get("name") if target else None
            await _evict_pooled_session(session_id)
            logger.info(
                "Failover piggyback: session %s reuses account %s rotated by a "
                "concurrent switch (no re-switch)",
                session_id, current_active,
            )
            return FailoverDecision(
                retry=True, account_id=current_active,
                account_name=name, switched=False,
            )

        # This turn has failed on the current active account — record it and
        # select the next account to try.
        if current_active:
            attempted.add(current_active)

        # switch_account now refreshes the target's expired/near-expiry token
        # before activating it (see claude_accounts._ensure_target_fresh). A
        # target whose refresh token is revoked (or a transient refresh failure)
        # returns success=False — don't abort the whole failover on that, mark it
        # attempted and try the NEXT candidate so one dead account can't strand a
        # user who has other healthy accounts. The last switch error is kept so
        # the exhausted-path notification can be specific.
        target_name: Optional[str] = None
        last_switch_error: Optional[str] = None
        while True:
            target = await run_sync(_select_next_account, attempted)
            if target is None:
                await _notify(
                    api_user_id, session_id,
                    severity="warning",
                    message=(
                        "Usage limit reached on all saved accounts — will resume "
                        "when a window resets."
                        if last_switch_error is None else
                        "Usage limit reached — couldn't switch to another account "
                        "(some may need to be re-added). Manual attention may be needed."
                    ),
                )
                logger.info(
                    "Failover exhausted: no eligible account remaining for session "
                    "%s (attempted=%d, last_error=%s)",
                    session_id, len(attempted), last_switch_error,
                )
                return FailoverDecision(retry=False)

            target_id = target["id"]
            target_name = target.get("name")

            result = await run_sync(claude_accounts.switch_account, target_id)
            if result.get("success"):
                break

            # This candidate couldn't be activated (e.g. expired with a revoked
            # refresh token). Record it and loop to the next candidate.
            last_switch_error = result.get("error")
            attempted.add(target_id)
            logger.warning(
                "Failover switch to account %s failed (%s) for session %s; "
                "trying next candidate",
                target_id, last_switch_error, session_id,
            )

        _last_switch_at = time.monotonic()
        attempted.add(target_id)

    # Outside the lock: credential is on disk. Evict the pooled client so the
    # retry respawns reading the freshly-written host credentials.
    await _evict_pooled_session(session_id)
    schedule_usage_refresh()

    # The active account changed under a running Claude-in-Chrome integration:
    # the container browser is still signed in to the OLD account, so browser
    # tools would now fail with "different claude.ai account". Clear the stale
    # pairing pin and surface a reconnect prompt. Best-effort; never raises.
    try:
        from app.core.chrome_rebind import rebind_browser_after_account_switch
        await rebind_browser_after_account_switch(
            session_id=session_id,
            api_user_id=api_user_id,
            new_account_name=target_name,
        )
    except Exception:  # noqa: BLE001 — must never break the failover retry
        logger.debug("chrome rebind after failover switch failed", exc_info=True)

    await _notify(
        api_user_id, session_id,
        severity="info",
        message=f"Usage limit reached — switched to {target_name}, retrying…",
    )
    logger.info(
        "Failover: switched session %s to account %s (%s), retrying",
        session_id, target_id, target_name,
    )
    return FailoverDecision(
        retry=True, account_id=target_id, account_name=target_name, switched=True,
    )


async def execute_query_with_failover(**kwargs) -> Dict[str, Any]:
    """Run ``execute_query``; on a usage-limit 429, auto-switch the active Claude
    account (if enabled) and retry on the resumed session.

    Shared by every non-streaming caller (the query API routes, automations,
    connectors, and the ScheduleWakeup builtin) so an eligible 429 — whose
    generic rate-limit toast is suppressed upstream — actually retries instead
    of failing silently. The per-turn ``attempted`` set bounds the loop (every
    rotation removes one candidate). The first call persists the user message;
    retries do not (it was already persisted and the resumed session replays it).

    ``execute_query`` is imported lazily to avoid an import cycle
    (query_engine ↔ account_failover).
    """
    from app.core.query_engine import execute_query

    attempted: Set[str] = set()
    while True:
        result = await execute_query(**kwargs, persist_user_message=not attempted)
        kwargs["session_id"] = result["session_id"]
        if not is_usage_limit_error(result.get("metadata")):
            return result
        decision = await attempt_failover(
            result["session_id"], kwargs.get("api_user_id"), attempted
        )
        if not decision.retry:
            return result
