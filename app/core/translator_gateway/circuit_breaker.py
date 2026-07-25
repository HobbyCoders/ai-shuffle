"""Install-wide per-provider circuit breaker for the translator gateway.

WHY THIS EXISTS (2026-07-22 rate-limit storm, forensics 2026-07-25)
-------------------------------------------------------------------
A single runaway fan-out burned a user's whole weekly provider quota:
**3,172 upstream POSTs in ~11 minutes — 1,422 x HTTP 200 and 1,750 x HTTP 429**
— every one of them AFTER the parent turn had already ended, from ~122 distinct
background tasks spread across multiple ``claude.exe`` processes.

The critical finding: **the retry loop lives inside the CLI processes, not in
this gateway.** ``upstream.stream_responses_events`` retries only on a Codex 401
(token rotation) and otherwise raises ``UpstreamError`` — so each of those 1,750
rate-limit responses was a SEPARATE inbound request from a CLI that decided to
retry on its own. Killing the app-side tasks does not help either: both
registered root tasks reached terminal status at 20:06:43 and 410 more upstream
POSTs still flowed over the following two minutes.

The gateway is therefore the ONLY chokepoint that sees all of that traffic, and
refusing at the gateway is the only thing that can actually stop it.

DESIGN (decided, not to be redesigned here)
-------------------------------------------
* **Trip condition:** the FIRST HTTP 429 from an upstream provider trips the
  breaker for that ``provider_id``. Only 429 — never 401 (that is the existing
  Codex token-rotation retry path and must keep working untouched) and never
  5xx.
* **Scope: INSTALL-WIDE** (user decision, 2026-07-25). While tripped, EVERY
  request routed to that provider is refused regardless of which session or turn
  it came from. Deliberate: a wide fan-out spread across many sessions is
  exactly the failure mode above, so a session-scoped breaker would not have
  prevented it.
* **Duration: time-based cooldown.** The upstream ``Retry-After`` header is
  honoured when present (both the delta-seconds and the HTTP-date forms). When
  absent/unusable a default is used. The cooldown is CAPPED so a hostile or
  malformed value cannot lock a provider out indefinitely.
* **While tripped:** the router fails fast WITHOUT calling upstream, surfacing a
  normal ``UpstreamError``-shaped 429 through the existing error path.
* **The breaker REFUSES; it never reroutes.** No account rotation, no failover,
  no provider switching (standing ruling P10).

State is in-memory and process-local, which is sufficient for the failure mode
above (all the traffic arrives at this one gateway process). Persisting the
cooldown beside the credential so it survives a restart and is shared across
installs is a later chunk (P3-G): the persistence seam is the
:class:`ProviderCircuitBreaker` class — swap the ``_state`` dict for a
DB-backed store and every caller below is unchanged.

Kill switch: ``PROVIDER_CIRCUIT_BREAKER_ENABLED=false`` restores the previous
behaviour exactly (nothing is recorded, nothing is ever refused).
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Dict, Optional

from app.core.config import settings

logger = logging.getLogger(__name__)

# Fallbacks used only if the settings fields are somehow absent (e.g. an older
# Settings object injected by a test double). The real defaults live in
# app/core/config.py.
_FALLBACK_DEFAULT_COOLDOWN = 60.0
_FALLBACK_MAX_COOLDOWN = 300.0


# ─── Public state object ─────────────────────────────────────────────────────

@dataclass(frozen=True)
class BreakerTrip:
    """One provider's open-circuit window.

    ``until`` is an absolute ``time.time()`` epoch. ``status_code`` is recorded
    for future diagnostics; today it is always 429 (the only trip condition).
    """

    provider_id: str
    tripped_at: float
    until: float
    status_code: int = 429

    def remaining_seconds(self, now: Optional[float] = None) -> float:
        """Seconds left on the cooldown (never negative)."""
        current = time.time() if now is None else now
        return max(0.0, self.until - current)

    def until_iso(self) -> str:
        """``until`` as an ISO-8601 UTC timestamp (for user-facing messages)."""
        return (
            datetime.fromtimestamp(self.until, tz=timezone.utc)
            .isoformat(timespec="seconds")
            .replace("+00:00", "Z")
        )


# ─── Settings access (read at call time so a flag flip takes effect live) ────

def _enabled() -> bool:
    return bool(getattr(settings, "provider_circuit_breaker_enabled", True))


def _default_cooldown() -> float:
    raw = getattr(
        settings, "provider_circuit_breaker_cooldown_seconds", _FALLBACK_DEFAULT_COOLDOWN
    )
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return _FALLBACK_DEFAULT_COOLDOWN
    return value if value > 0 else _FALLBACK_DEFAULT_COOLDOWN


def _max_cooldown() -> float:
    raw = getattr(
        settings,
        "provider_circuit_breaker_max_cooldown_seconds",
        _FALLBACK_MAX_COOLDOWN,
    )
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return _FALLBACK_MAX_COOLDOWN
    return value if value > 0 else _FALLBACK_MAX_COOLDOWN


# ─── Retry-After parsing (RFC 9110 §10.2.3: delta-seconds OR HTTP-date) ──────

def parse_retry_after(
    value: Optional[str], *, now: Optional[float] = None
) -> Optional[float]:
    """Parse a ``Retry-After`` header into a positive number of seconds.

    Supports BOTH standard forms:
      * delta-seconds — ``Retry-After: 120``
      * HTTP-date     — ``Retry-After: Wed, 21 Oct 2015 07:28:00 GMT``

    Returns ``None`` for a missing, malformed, or non-positive value; the caller
    then falls back to the configured default cooldown. A non-positive value is
    treated as "no usable value" rather than "no cooldown" so a hostile
    ``Retry-After: 0`` cannot switch the protection off.
    """
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None

    # Form 1: delta-seconds.
    try:
        seconds = float(text)
    except ValueError:
        pass
    else:
        return seconds if seconds > 0 else None

    # Form 2: HTTP-date.
    try:
        when = parsedate_to_datetime(text)
    except (TypeError, ValueError):
        return None
    if when is None:
        return None
    if when.tzinfo is None:  # A date with no zone is GMT per RFC 9110.
        when = when.replace(tzinfo=timezone.utc)
    current = time.time() if now is None else now
    delta = when.timestamp() - current
    return delta if delta > 0 else None


def cooldown_seconds(
    retry_after: Optional[str], *, now: Optional[float] = None
) -> float:
    """Resolve the cooldown for a 429: ``Retry-After`` if usable, else default.

    Always clamped to the configured ceiling so a hostile or malformed
    ``Retry-After`` (``Retry-After: 999999999``) cannot lock a provider out
    indefinitely.
    """
    parsed = parse_retry_after(retry_after, now=now)
    resolved = parsed if parsed is not None else _default_cooldown()
    return min(resolved, _max_cooldown())


# ─── The breaker ─────────────────────────────────────────────────────────────

class ProviderCircuitBreaker:
    """Thread-safe, install-wide, per-provider open-circuit state.

    Safe under concurrent access from the gateway's async request handlers and
    from any other thread: every read and write of ``_state`` happens under one
    lock, and expired entries are pruned on read.
    """

    def __init__(self) -> None:
        self._state: Dict[str, BreakerTrip] = {}
        self._lock = threading.Lock()

    # -- record -------------------------------------------------------------

    def record_rate_limited(
        self,
        provider_id: Optional[str],
        retry_after: Optional[str] = None,
        *,
        now: Optional[float] = None,
    ) -> Optional[BreakerTrip]:
        """Trip the breaker for ``provider_id`` after an upstream HTTP 429.

        Returns the resulting :class:`BreakerTrip`, or ``None`` when the breaker
        is disabled or there is no provider id. Repeated 429s EXTEND but never
        shorten an open window (the later ``until`` wins), so a burst of
        concurrent 429s carrying a short ``Retry-After`` cannot walk back a
        longer cooldown already in force.
        """
        if not provider_id or not _enabled():
            return None
        current = time.time() if now is None else now
        cooldown = cooldown_seconds(retry_after, now=current)
        candidate = BreakerTrip(
            provider_id=provider_id,
            tripped_at=current,
            until=current + cooldown,
            status_code=429,
        )
        with self._lock:
            existing = self._state.get(provider_id)
            if existing is not None and existing.until >= candidate.until:
                # Already open for at least as long; keep the longer window.
                return existing
            self._state[provider_id] = candidate
        logger.warning(
            "provider circuit breaker OPEN: provider=%s cooldown=%.0fs until=%s "
            "(upstream returned 429; refusing all gateway requests to this "
            "provider install-wide until then)",
            provider_id,
            cooldown,
            candidate.until_iso(),
        )
        return candidate

    # -- query --------------------------------------------------------------

    def check(
        self, provider_id: Optional[str], *, now: Optional[float] = None
    ) -> Optional[BreakerTrip]:
        """Return the open :class:`BreakerTrip` for ``provider_id``, else ``None``.

        Expired windows clear themselves here — there is no sweeper thread.
        Returns ``None`` unconditionally while the breaker is disabled.
        """
        if not provider_id or not _enabled():
            return None
        current = time.time() if now is None else now
        with self._lock:
            trip = self._state.get(provider_id)
            if trip is None:
                return None
            if current >= trip.until:
                # Cooldown elapsed — close the circuit.
                self._state.pop(provider_id, None)
                return None
            return trip

    def is_tripped(
        self, provider_id: Optional[str], *, now: Optional[float] = None
    ) -> bool:
        return self.check(provider_id, now=now) is not None

    # -- reset (tests / operator recovery) ----------------------------------

    def reset(self, provider_id: Optional[str] = None) -> None:
        """Clear one provider's window, or ALL of them when ``provider_id`` is None."""
        with self._lock:
            if provider_id is None:
                self._state.clear()
            else:
                self._state.pop(provider_id, None)


# ─── Process-wide singleton + module-level convenience API ───────────────────
# The gateway server and its request handlers all run in this one process, so a
# single instance is the install-wide breaker. (P3-G will swap the backing store
# for a persisted one behind this same surface.)

_BREAKER = ProviderCircuitBreaker()


def get_breaker() -> ProviderCircuitBreaker:
    return _BREAKER


def record_rate_limited(
    provider_id: Optional[str],
    retry_after: Optional[str] = None,
    *,
    now: Optional[float] = None,
) -> Optional[BreakerTrip]:
    return _BREAKER.record_rate_limited(provider_id, retry_after, now=now)


def check(
    provider_id: Optional[str], *, now: Optional[float] = None
) -> Optional[BreakerTrip]:
    return _BREAKER.check(provider_id, now=now)


def is_tripped(provider_id: Optional[str], *, now: Optional[float] = None) -> bool:
    return _BREAKER.is_tripped(provider_id, now=now)


def reset(provider_id: Optional[str] = None) -> None:
    _BREAKER.reset(provider_id)


# ─── User-facing refusal text ────────────────────────────────────────────────

def refusal_message(trip: BreakerTrip, *, now: Optional[float] = None) -> str:
    """The message surfaced to the CLI when a request is refused.

    States the provider, that it is rate-limited, and exactly when it clears —
    and says the request was NOT sent upstream, so a human reading the CLI
    output knows no further quota was spent.
    """
    remaining = trip.remaining_seconds(now)
    return (
        f"Provider '{trip.provider_id}' is rate limited: the upstream returned "
        f"HTTP 429, so this gateway has stopped sending it requests "
        f"install-wide. This request was NOT sent upstream. The block clears in "
        f"{remaining:.0f}s (at {trip.until_iso()}); retry after that."
    )
