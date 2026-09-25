"""Install-wide, per-provider rate-limit circuit breaker.

* **Trip condition:** the FIRST HTTP 429 for a ``provider_id`` trips it. Never
  401 (Codex token rotation must keep working) and never 5xx.
* **Scope: install-wide.** While tripped, the provider counts as rate limited
  for every session and turn: a runaway fan-out spans many sessions, so a
  session-scoped breaker would not contain it.
* **Cooldown:** honours ``Retry-After`` (delta-seconds or HTTP-date), with a
  default when absent and a cap so a hostile or malformed value cannot lock a
  provider out indefinitely.
* **The breaker refuses; it never reroutes.** No account rotation, no
  failover, no provider switching.

Recording seam: ``harness_adapter._usage_capture_http_client``, which sees every
native-leg response. The delegated (claude CLI) leg is not covered: its HTTP
traffic happens inside the CLI child process, where the app has no hook.
Consumer today: ``provider_agent._await_worker_idle`` fails a worker fast while
its provider is tripped. There is no pre-dispatch refusal, so the breaker is a
record-and-signal surface rather than a request gate.

State is in-memory and process-local. :class:`ProviderCircuitBreaker` is the
seam for a persisted store: swap its ``_state`` dict for a DB-backed one and
every caller is unchanged.

Kill switch: ``PROVIDER_CIRCUIT_BREAKER_ENABLED=false`` (nothing is recorded,
nothing is reported as tripped).
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

    Safe under concurrent access from the native legs' HTTP response hooks and
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
# Every native provider leg runs in this one process, so a single instance is
# the install-wide breaker. A persisted backing store would slot in behind this
# same surface.

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
    """The message surfaced to a caller whose request is refused.

    States the provider, that it is rate-limited, and exactly when it clears —
    and says the request was NOT sent upstream, so a human reading the output
    knows no further quota was spent.
    """
    remaining = trip.remaining_seconds(now)
    return (
        f"Provider '{trip.provider_id}' is rate limited: the upstream returned "
        f"HTTP 429, so this install has stopped sending it requests. This "
        f"request was NOT sent upstream. The block clears in "
        f"{remaining:.0f}s (at {trip.until_iso()}); retry after that."
    )
