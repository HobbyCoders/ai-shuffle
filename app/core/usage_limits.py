"""
Claude usage-limit monitor.

Fetches authoritative subscription usage from the Claude OAuth usage endpoint
(``GET https://api.anthropic.com/api/oauth/usage``) using the app's stored
Claude OAuth credential, normalises every rate-limit window into a compact
snapshot, caches it in memory, and lets callers broadcast it to connected
clients as a ``usage_snapshot`` WebSocket event.

Two data sources feed the frontend's per-window store:
  1. This poller's snapshot (all windows at once, every ~5 min + on demand).
  2. Live SDK ``rate_limit`` events, which refine a single window as it changes.

Both key on the same window-type strings (``five_hour``, ``seven_day``,
``seven_day_opus``, ``seven_day_sonnet``, ``seven_day_overage_included``,
``overage``) so they update the same slot.

Security: the OAuth access token is used only as a Bearer header and is NEVER
logged, printed, or included in the snapshot.
"""

import logging
import math
import time
from typing import Any, Dict, List, Optional

import httpx

from app.core.oauth import direct_oauth

logger = logging.getLogger(__name__)

USAGE_ENDPOINT = "https://api.anthropic.com/api/oauth/usage"
# The CLI sends this beta header; the endpoint answers with or without it, but
# we mirror the CLI to stay forward-compatible.
_OAUTH_BETA_HEADER = "oauth-2025-04-20"

# Canonical window types, in the order we want them surfaced. These match both
# the top-level keys in the usage response AND the SDK ``rate_limit`` event's
# ``rate_limit_type`` values, so a live event updates the same slot.
KNOWN_WINDOW_TYPES = (
    "five_hour",
    "seven_day",
    "seven_day_opus",
    "seven_day_sonnet",
    "seven_day_overage_included",
    "overage",
)

# Top-level typed keys carrying ``{utilization, resets_at, ...}`` blocks.
_TOP_LEVEL_TYPES = (
    "five_hour",
    "seven_day",
    "seven_day_opus",
    "seven_day_sonnet",
    "seven_day_overage_included",
)

# How long a cached snapshot is considered fresh for on-demand REST reads.
_SNAPSHOT_TTL_SECONDS = 300


def _limit_entry_to_type(entry: Dict[str, Any]) -> Optional[str]:
    """Map a normalised ``limits[]`` entry to a canonical window type.

    The usage response also carries a UI-oriented ``limits`` array; some windows
    (notably the scoped Fable weekly limit) only appear there, not as a
    top-level typed key. We fold those in so nothing is dropped.
    """
    kind = entry.get("kind")
    if kind == "session":
        return "five_hour"
    if kind == "weekly_all":
        return "seven_day"
    if kind == "weekly_scoped":
        scope = entry.get("scope") or {}
        model = ((scope.get("model") or {}).get("display_name") or "").lower()
        if "opus" in model:
            return "seven_day_opus"
        if "sonnet" in model:
            return "seven_day_sonnet"
        if "fable" in model:
            return "seven_day_overage_included"
    return None


def parse_usage_response(data: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Normalise the raw usage payload into a list of window dicts.

    Each window: ``{"type": str, "utilization": float, "resets_at": str|None}``.
    ``utilization`` is a percentage (0-100). Windows with no reading are omitted.
    """
    windows: Dict[str, Dict[str, Any]] = {}

    # 1. Top-level typed keys (authoritative float utilization).
    for wtype in _TOP_LEVEL_TYPES:
        block = data.get(wtype)
        if isinstance(block, dict) and block.get("utilization") is not None:
            try:
                windows[wtype] = {
                    "type": wtype,
                    "utilization": float(block["utilization"]),
                    "resets_at": block.get("resets_at"),
                }
            except (TypeError, ValueError):
                continue

    # 2. ``limits`` array — fills gaps (e.g. scoped Fable weekly limit).
    for entry in data.get("limits") or []:
        if not isinstance(entry, dict):
            continue
        wtype = _limit_entry_to_type(entry)
        if not wtype or wtype in windows:
            continue
        pct = entry.get("percent")
        if pct is None:
            continue
        try:
            windows[wtype] = {
                "type": wtype,
                "utilization": float(pct),
                "resets_at": entry.get("resets_at"),
            }
        except (TypeError, ValueError):
            continue

    # 3. Overage / extra usage (only when actually enabled).
    extra = data.get("extra_usage") or {}
    if isinstance(extra, dict) and extra.get("is_enabled") and extra.get("utilization") is not None:
        try:
            windows["overage"] = {
                "type": "overage",
                "utilization": float(extra["utilization"]),
                "resets_at": None,
            }
        except (TypeError, ValueError):
            pass

    # Order windows by KNOWN_WINDOW_TYPES, then any extras alphabetically.
    ordered = [windows[t] for t in KNOWN_WINDOW_TYPES if t in windows]
    ordered += [w for k, w in sorted(windows.items()) if k not in KNOWN_WINDOW_TYPES]
    return ordered


class UsageLimitsMonitor:
    """In-memory holder + fetcher for the latest Claude usage snapshot."""

    def __init__(self) -> None:
        self._windows: List[Dict[str, Any]] = []
        self._fetched_at: float = 0.0
        self._available: bool = False
        # Log endpoint failures at WARNING only once per failure streak.
        self._warned: bool = False
        # Monotonic tag on every snapshot. Bumped when the active account
        # switches so an in-flight poll (started against the OLD account) can be
        # detected and discarded instead of overwriting the new account's data,
        # and so clients can drop out-of-order snapshots.
        #
        # Seeded from the epoch clock (NOT 0) so that after a backend restart
        # the very first generation already exceeds anything the previous
        # process broadcast. An open browser tab keeps its last-seen generation
        # in memory; a near-0 restart seed would make it drop every
        # post-restart snapshot and wedge stale until a manual reload.
        self._generation: int = int(time.time())

    def current_generation(self) -> int:
        return self._generation

    def bump_generation(self) -> int:
        """Invalidate any in-flight fetch and tag future snapshots.

        Called synchronously on a successful account switch. A poll that began
        before this bump will see its captured generation differ from the
        current one at commit time and discard its (now-stale) result.

        Strictly increases AND keeps pace with the epoch clock, so generations
        stay monotonic even across a rapid restart-then-switch sequence.
        """
        self._generation = max(self._generation + 1, int(time.time()))
        return self._generation

    def _snapshot(self) -> Dict[str, Any]:
        return {
            "windows": list(self._windows),
            "fetched_at": self._fetched_at or None,
            "available": self._available,
            "generation": self._generation,
        }

    async def fetch(self) -> Dict[str, Any]:
        """Fetch and cache the latest snapshot. Never raises.

        On no/expired credential returns an empty (``available: false``)
        snapshot without spamming logs. On endpoint failure keeps the last good
        snapshot and logs once at WARNING.
        """
        # Tag this poll with the generation it started under. A concurrent
        # account switch bumps the generation; if it changes before we commit,
        # this poll's result is stale and must be discarded (see below).
        gen = self._generation

        try:
            # Keep the on-disk token fresh (no-op in API-key mode / when valid).
            await direct_oauth.ensure_fresh_credentials()
        except Exception:  # noqa: BLE001 — never let refresh break the poll
            pass

        token = direct_oauth.get_access_token()
        if not token:
            # No OAuth credential (e.g. API-key mode or logged out). Report
            # empty gracefully; this is an expected steady state, so no logging.
            self._available = False
            self._windows = []
            return self._snapshot()

        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "anthropic-beta": _OAUTH_BETA_HEADER,
        }
        try:
            async with httpx.AsyncClient(timeout=20.0) as client:
                resp = await client.get(USAGE_ENDPOINT, headers=headers)
            if resp.status_code != 200:
                if not self._warned:
                    logger.warning(
                        "Usage-limits fetch failed: HTTP %s (keeping last snapshot)",
                        resp.status_code,
                    )
                    self._warned = True
                return self._snapshot()

            if gen != self._generation:
                # The active account switched while this poll was in flight.
                # Discard the (old account's) result rather than overwriting the
                # new account's data; the post-switch poll owns the snapshot.
                logger.debug(
                    "Usage-limits fetch discarded: generation changed %s -> %s",
                    gen, self._generation,
                )
                return self._snapshot()

            data = resp.json()
            self._windows = parse_usage_response(data)
            self._fetched_at = time.time()
            self._available = True
            self._warned = False
            logger.debug("Usage-limits snapshot updated: %d window(s)", len(self._windows))
            return self._snapshot()

        except Exception as e:  # noqa: BLE001 — keep last snapshot on any error
            if not self._warned:
                logger.warning("Usage-limits fetch error: %s (keeping last snapshot)", e)
                self._warned = True
            return self._snapshot()

    async def get_snapshot(self, max_age: float = _SNAPSHOT_TTL_SECONDS) -> Dict[str, Any]:
        """Return the cached snapshot, fetching first if stale or never-fetched."""
        if not self._fetched_at or (time.time() - self._fetched_at) > max_age:
            return await self.fetch()
        return self._snapshot()

    def apply_live_event(
        self,
        rate_limit_type: Optional[str],
        utilization: Optional[float],
        resets_at: Optional[str],
    ) -> Optional[Dict[str, Any]]:
        """Refine a single window from a live SDK ``rate_limit`` event.

        Returns the updated snapshot when a window changed, else None. Does not
        broadcast — callers decide (see ``refresh_and_broadcast`` / the headless
        query path).
        """
        if not rate_limit_type or utilization is None:
            return None
        try:
            util = float(utilization)
        except (TypeError, ValueError):
            return None
        if not math.isfinite(util):
            return None
        # SDK rate_limit events report utilization as a 0.0-1.0 fraction, while
        # the usage endpoint (and thus our snapshot) uses 0-100 percent. Normalise
        # so every window in the snapshot is on the same 0-100 scale.
        if util <= 1:
            util *= 100

        updated = {"type": rate_limit_type, "utilization": util, "resets_at": resets_at}
        replaced = False
        for i, w in enumerate(self._windows):
            if w.get("type") == rate_limit_type:
                self._windows[i] = updated
                replaced = True
                break
        if not replaced:
            self._windows.append(updated)
        self._available = True
        return self._snapshot()

    async def _broadcast(self, snapshot: Dict[str, Any]) -> None:
        """Fan the snapshot out to all connected clients (best-effort)."""
        try:
            from app.core.sync_engine import sync_engine
            await sync_engine.broadcast_usage_snapshot(snapshot)
        except Exception as e:  # noqa: BLE001 — broadcast is best-effort
            logger.debug("Usage-snapshot broadcast skipped: %s", e)

    async def refresh_and_broadcast(self) -> Dict[str, Any]:
        """Poller entry point: fetch the latest snapshot and broadcast it."""
        snapshot = await self.fetch()
        await self._broadcast(snapshot)
        return snapshot

    async def apply_live_event_and_broadcast(
        self,
        rate_limit_type: Optional[str],
        utilization: Optional[float],
        resets_at: Optional[str],
    ) -> None:
        """Apply a live event and, if it changed anything, broadcast the update.

        Used by the headless/automation query path, which has no per-session WS
        stream to yield the ``rate_limit`` event onto.
        """
        snapshot = self.apply_live_event(rate_limit_type, utilization, resets_at)
        if snapshot is not None:
            await self._broadcast(snapshot)


# Global singleton.
usage_limits_monitor = UsageLimitsMonitor()
