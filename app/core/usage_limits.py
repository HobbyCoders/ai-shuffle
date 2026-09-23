"""
Claude usage-limit monitor.

Fetches authoritative subscription usage from the Claude OAuth usage endpoint
(``GET https://api.anthropic.com/api/oauth/usage``) using the app's stored
Claude OAuth credential, normalises every rate-limit window into a compact
snapshot, caches it in memory, and lets callers broadcast it to connected
clients as a ``usage_snapshot`` WebSocket event.

Two data sources feed the frontend's per-window store (both are defined in
``app.core.usage_sources``):
  1. This poller's snapshot (all windows at once, at most every ~5 min). The
     OAuth usage endpoint is an unofficial, subscription-specific endpoint.
  2. Live SDK ``rate_limit`` events, which refine a single window as it changes.

Ordinary reads never wait on the upstream endpoint: a stale read returns the
cached snapshot and starts one shared background refresh. Every refresh goes
through the durable, cross-worker request budget in ``usage_polling``, and a
refused credential is not re-probed until the token changes.

Both key on the same window-type strings (``five_hour``, ``seven_day``,
``seven_day_opus``, ``seven_day_sonnet``, ``seven_day_overage_included``,
``overage``) so they update the same slot.

Security: the OAuth access token is used only as a Bearer header and is NEVER
logged, printed, or included in the snapshot.
"""

import asyncio
import logging
import math
import os
import time
from typing import Any, Dict, List, Optional

import httpx

from app.core.oauth import direct_oauth
from app.core.usage_sources import CLAUDE_OAUTH_USAGE_URL
from app.db.async_database import run_sync

logger = logging.getLogger(__name__)

USAGE_ENDPOINT = CLAUDE_OAUTH_USAGE_URL
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

# Minimum spacing between background refresh attempts started by ordinary
# reads. The durable budget already bounds upstream requests; this bounds the
# token-refresh check and the budget claim (a peer round trip on enrolled
# workers) that every attempt costs.
_ATTEMPT_SPACING_SECONDS = 60

# Short TTL for the assembled cluster view (peer snapshot rows + installs map).
# Hot GETs/broadcasts within this window reuse the cached DB reads; the cache is
# invalidated whenever THIS process persists its own row so a local change
# surfaces at once. Bounds the per-event and per-GET DB cost.
_CLUSTER_CACHE_TTL_SECONDS = 4.0


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


def _used_to_remaining(value: Any) -> float:
    """Convert a raw endpoint percentage into quota REMAINING (0-100).

    ``GET /api/oauth/usage`` reports every window's ``utilization`` (and every
    ``limits[].percent``) as percent USED — verified live: a weekly window that
    had reset 34 minutes earlier reported 0. Our snapshot contract is percent
    REMAINING (the same orientation ``apply_live_event`` flips SDK events into),
    so flip it here, clamped to 0-100.

    Raises TypeError/ValueError on a non-numeric input, which callers already
    treat as "drop this window".
    """
    used = float(value)
    return 100.0 - max(0.0, min(100.0, used))


def parse_usage_response(data: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Normalise the raw usage payload into a list of window dicts.

    Each window: ``{"type": str, "utilization": float, "resets_at": str|None}``.
    ``utilization`` is a percentage (0-100) of quota REMAINING — the endpoint
    reports percent USED, so every reading is flipped on the way in (see
    ``_used_to_remaining``). Windows with no reading are omitted.
    """
    windows: Dict[str, Dict[str, Any]] = {}

    # 1. Top-level typed keys (authoritative float utilization).
    for wtype in _TOP_LEVEL_TYPES:
        block = data.get(wtype)
        if isinstance(block, dict) and block.get("utilization") is not None:
            try:
                windows[wtype] = {
                    "type": wtype,
                    "utilization": _used_to_remaining(block["utilization"]),
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
                "utilization": _used_to_remaining(pct),
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
                "utilization": _used_to_remaining(extra["utilization"]),
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
        # Short-TTL cache of the assembled cluster view: (expires_at, snapshots).
        # Invalidated on our own persist so local changes are never masked.
        self._cluster_cache: Optional[tuple[float, List[Dict[str, Any]]]] = None
        # Last upstream attempt (distinct from ``_fetched_at``, the last
        # success) and why it failed, for the usage API.
        self._attempted_at: float = 0.0
        self._last_error: str | None = None
        # Negative capability cache: digest of the token the endpoint refused.
        self._denied_fingerprint: str | None = None
        self._refresh_task: asyncio.Task | None = None

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
            "attempted_at": self._attempted_at or None,
            "last_error_kind": self._last_error,
            "available": self._available,
            "generation": self._generation,
        }

    @property
    def refreshing(self) -> bool:
        return self._refresh_task is not None and not self._refresh_task.done()

    def request_refresh(self) -> asyncio.Task | None:
        """Start (or join) the single background refresh + broadcast."""
        if self.refreshing:
            return self._refresh_task
        try:
            self._refresh_task = asyncio.create_task(self._refresh_quietly())
        except RuntimeError:
            return None
        return self._refresh_task

    async def _refresh_quietly(self) -> None:
        try:
            await self.refresh_and_broadcast()
        except Exception:  # noqa: BLE001 — background refresh is best-effort
            logger.debug("Claude usage refresh failed")

    def _resolve_account_meta(self) -> Dict[str, Optional[str]]:
        """Best-effort {account_id, account_email, account_name} for this install.

        Resolved through ``claude_accounts.resolve_active_account_row()``, which
        repairs an active pointer left dangling by a cross-install delete. All
        three fields come back None when there is no account row to name — a
        legacy single-account / API-key install (no pointer), or a dangle we
        could not repair. Deliberately all-or-nothing: stamping a snapshot with
        an account_id that resolves to no email/name only produces unattributable
        rows in the cluster usage view. Never raises — callers persist Nones on
        any error.
        """
        meta: Dict[str, Optional[str]] = {
            "account_id": None, "account_email": None, "account_name": None,
        }
        try:
            from app.core import claude_accounts
            row = claude_accounts.resolve_active_account_row()
            if row:
                meta["account_id"] = row.get("id")
                meta["account_email"] = row.get("email")
                meta["account_name"] = row.get("name")
        except Exception:  # noqa: BLE001 — metadata is best-effort
            pass
        return meta

    def _persist_snapshot(self) -> None:
        """Persist this install's snapshot row to the shared DB (best-effort).

        Keyed by ``effective_instance_id`` so peers can serve every install's
        per-account usage. Wrapped so a DB hiccup never breaks the poll or a
        live-event application. Synchronous psycopg2 — async callers MUST run
        this through ``run_sync`` so it never blocks the event loop.
        """
        try:
            from app.core.config import settings
            from app.db import database as db
            meta = self._resolve_account_meta()
            fetched_ms = int(self._fetched_at * 1000) if self._fetched_at else None
            db.upsert_usage_snapshot(
                settings.effective_instance_id,
                self._windows,
                self._available,
                self._generation,
                fetched_ms,
                account_id=meta["account_id"],
                account_email=meta["account_email"],
                account_name=meta["account_name"],
            )
            # Our own row changed — drop the cluster cache so the next assembly
            # re-reads and reflects it immediately.
            self._cluster_cache = None
        except Exception as e:  # noqa: BLE001 — persistence must never break the poll
            logger.debug("Usage-snapshot persist skipped: %s", e)

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
        self._attempted_at = time.time()

        token = direct_oauth.get_access_token()
        if not token:
            # No OAuth credential (e.g. API-key mode or logged out). Report
            # empty gracefully; this is an expected steady state, so no logging.
            self._available = False
            self._windows = []
            self._last_error = "missing_credentials"
            # Still persist so API-key-mode installs are represented in the
            # cluster snapshot (empty windows, available=False).
            await run_sync(self._persist_snapshot)
            return self._snapshot()

        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "anthropic-beta": _OAUTH_BETA_HEADER,
        }
        from app.core import usage_sources
        from app.core.usage_polling import (
            UsageDeferred,
            capability_fingerprint,
            usage_get,
        )

        fingerprint = capability_fingerprint("claude_oauth_usage", headers)
        if fingerprint == self._denied_fingerprint:
            # Refused for this exact token: no budget claim, no upstream probe.
            return self._snapshot()
        try:
            try:
                resp = await usage_get(
                    USAGE_ENDPOINT, headers=headers,
                    interval=usage_sources.interval_for("claude_oauth_usage", USAGE_ENDPOINT),
                )
            except UsageDeferred:
                return self._snapshot()
            if resp.status_code != 200:
                status = resp.status_code
                if status in usage_sources.DENIED_STATUSES:
                    self._last_error = "billing_access_denied"
                elif status in usage_sources.UNSUPPORTED_STATUSES:
                    self._last_error = "unsupported"
                else:
                    self._last_error = "unavailable"
                if self._last_error != "unavailable":
                    self._denied_fingerprint = fingerprint
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
            self._last_error = None
            # Persist this install's snapshot (stamped with the active account)
            # so peers can serve the correct per-account reset time for offloaded
            # sessions. Best-effort — never break the poll on a DB error.
            await run_sync(self._persist_snapshot)
            # Ride along on the poll to keep the active account's subscription
            # tier current: at most ONE profile GET per hour (self-throttled),
            # which heals rows whose subscription_type is still NULL and tracks
            # a plan change. Best-effort — never affects the usage snapshot.
            #
            # Suppressed under TESTING (conftest sets TESTING=1, mirroring
            # app.core.startup_reconcile): this reads the REAL on-disk
            # credential — which the test host has — so an unpatched
            # monitor.fetch() test would issue a live authenticated request.
            # ensure_subscription_type() itself is unit-tested directly.
            if os.environ.get("TESTING") != "1":
                try:
                    from app.core import claude_accounts
                    await claude_accounts.ensure_subscription_type()
                except Exception as e:  # noqa: BLE001 — advisory metadata only
                    logger.debug("Subscription-type refresh skipped: %s", e)
            logger.debug("Usage-limits snapshot updated: %d window(s)", len(self._windows))
            return self._snapshot()

        except Exception as e:  # noqa: BLE001 — keep last snapshot on any error
            self._last_error = "unavailable"
            if not self._warned:
                logger.warning("Usage-limits fetch error: %s (keeping last snapshot)", e)
                self._warned = True
            return self._snapshot()

    async def get_snapshot(self, max_age: float = _SNAPSHOT_TTL_SECONDS) -> Dict[str, Any]:
        """Return the cached snapshot; start a background refresh when stale.

        Never waits on the upstream endpoint. Concurrent stale reads share one
        refresh task, and attempts are spaced by ``_ATTEMPT_SPACING_SECONDS``.
        """
        now = time.time()
        stale = not self._fetched_at or (now - self._fetched_at) > max_age
        if stale and now - self._attempted_at >= _ATTEMPT_SPACING_SECONDS:
            self.request_refresh()
        return self._snapshot()

    async def get_cluster_snapshot(
        self, max_age: float = _SNAPSHOT_TTL_SECONDS
    ) -> Dict[str, Any]:
        """Return the LOCAL snapshot plus every install's persisted snapshot.

        Starts a background refresh of this install's own reading when stale
        (see :meth:`get_snapshot`), then reads all persisted snapshots and joins the installs registry for
        ``device_name``/``online``. Keeps the legacy top-level fields
        (``windows``/``fetched_at``/``available``/``generation``) intact for old
        clients, and adds:

          * ``snapshots`` — one entry per install (self always included), each
            ``{instance_id, device_name, online, account_id, account_email,
            account_name, plan_type, windows, fetched_at, available,
            generation}``.
          * ``self_instance_id`` — this install's ``effective_instance_id``.

        Rows whose ``instance_id`` is not in the installs registry are skipped
        UNLESS the row is self (so a brand-new install with no registry row yet
        still reports its own numbers).
        """
        # Schedule (never await) a refresh of our own reading when stale.
        await self.get_snapshot(max_age)
        # Assembly reads the DB — offload it so it never blocks the event loop.
        return await run_sync(self._cluster_snapshot)

    async def get_cached_cluster_snapshot(self) -> Dict[str, Any]:
        """Return the assembled snapshot without fetching or persisting.

        Model-callable usage inspection must be a read of the last observed
        state.  In particular, it must not turn a tool call into an upstream
        billing request merely because the normal REST endpoint considers the
        local snapshot stale.
        """
        return await run_sync(self._cluster_snapshot)

    def _read_cluster_rows(self, self_id: str) -> Optional[List[Dict[str, Any]]]:
        """Read + assemble the per-install snapshot list from the DB (cached).

        Returns the list on success (self always included when it has a row), or
        ``None`` when the DB read FAILED so the caller can omit the cluster keys
        entirely rather than emit an empty list. Cached for
        ``_CLUSTER_CACHE_TTL_SECONDS`` and invalidated on our own persist.
        Synchronous — call via ``run_sync`` from async paths.
        """
        now = time.time()
        cached = self._cluster_cache
        if cached is not None and cached[0] > now:
            return cached[1]

        try:
            from app.db import database as db
            installs_map = db.get_installs_map()
            rows = db.get_all_usage_snapshots()
        except Exception as e:  # noqa: BLE001 — cluster read is best-effort
            logger.debug("Usage cluster snapshot read skipped: %s", e)
            return None

        # Subscription tier per Claude account, so each install's row can carry
        # the plan its OWN account is on (the local model registry only speaks
        # for the self install). Degrades to {} — a missing plan renders as no
        # subtext, which must never cost us the whole cluster read.
        plans: Dict[Any, Optional[str]] = {}
        try:
            for account in db.get_all_claude_accounts():
                account_id = account.get("id")
                if account_id:
                    plans[account_id] = account.get("subscription_type")
        except Exception as e:  # noqa: BLE001 — plan join is advisory
            logger.debug("Usage cluster plan join skipped: %s", e)

        snapshots: List[Dict[str, Any]] = []
        for row in rows:
            iid = row.get("instance_id")
            install = installs_map.get(iid)
            if install is None and iid != self_id:
                # An install that has left the registry — drop its stale row.
                continue
            snapshots.append({
                "instance_id": iid,
                "device_name": install.get("device_name") if install else None,
                "online": bool(install.get("online")) if install else False,
                "account_id": row.get("account_id"),
                "account_email": row.get("account_email"),
                "account_name": row.get("account_name"),
                # Subscription tier of THAT install's account (None when the
                # row carries no account, or its plan isn't known yet).
                "plan_type": plans.get(row.get("account_id")),
                "windows": row.get("windows") or [],
                "fetched_at": row.get("fetched_at"),
                "available": bool(row.get("available")),
                "generation": row.get("generation"),
            })
        self._cluster_cache = (now + _CLUSTER_CACHE_TTL_SECONDS, snapshots)
        return snapshots

    def _cluster_snapshot(self) -> Dict[str, Any]:
        """Assemble the enriched cluster snapshot from the CURRENT local state.

        Does no network I/O — callers refresh first if they need fresh data.
        Reads all persisted per-install snapshots and joins the installs
        registry. On a DB READ FAILURE the cluster keys (``snapshots`` /
        ``self_instance_id``) are OMITTED so the frontend falls back to the
        legacy single-install fold instead of wiping its buckets on an empty
        list. Synchronous — call via ``run_sync`` from async paths.
        """
        base = self._snapshot()

        from app.core.config import settings
        self_id = settings.effective_instance_id

        snapshots = self._read_cluster_rows(self_id)
        if snapshots is None:
            # DB read failed — return the plain legacy snapshot only.
            return base

        base["snapshots"] = snapshots
        base["self_instance_id"] = self_id
        return base

    def apply_live_event(
        self,
        rate_limit_type: Optional[str],
        utilization: Optional[float],
        resets_at: Optional[str],
    ) -> Optional[Dict[str, Any]]:
        """Refine a single window from a live SDK ``rate_limit`` event (in-memory).

        Returns the updated snapshot when the reading MATERIALLY changed (i.e.
        needs persist + broadcast), else None — an identical repeat (same type,
        utilization and resets_at) is a no-op so a tool-use loop's per-round-trip
        events don't each cost a DB upsert + cluster fanout. Does NOT persist or
        broadcast — async callers do that via ``run_sync`` (see
        ``apply_live_event_and_broadcast`` / ``refresh_and_broadcast``).
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
        # Polled snapshot windows carry quota REMAINING; SDK events carry quota
        # USED — flip so the persisted/broadcast snapshot stays consistent.
        util = 100.0 - util

        updated = {"type": rate_limit_type, "utilization": util, "resets_at": resets_at}
        replaced = False
        changed = True
        for i, w in enumerate(self._windows):
            if w.get("type") == rate_limit_type:
                # Compare against the last-applied (= last-persisted) reading.
                if w.get("utilization") == util and w.get("resets_at") == resets_at:
                    changed = False
                self._windows[i] = updated
                replaced = True
                break
        if not replaced:
            self._windows.append(updated)
        self._available = True
        if not changed:
            return None
        return self._snapshot()

    async def _broadcast(self, snapshot: Dict[str, Any]) -> None:
        """Fan the snapshot out to all connected clients (best-effort)."""
        try:
            from app.core.sync_engine import sync_engine
            await sync_engine.broadcast_usage_snapshot(snapshot)
        except Exception as e:  # noqa: BLE001 — broadcast is best-effort
            logger.debug("Usage-snapshot broadcast skipped: %s", e)

    async def refresh_and_broadcast(self) -> Dict[str, Any]:
        """Poller entry point: fetch the latest snapshot and broadcast it.

        Broadcasts the enriched CLUSTER snapshot (every install's per-account
        data), not just this install's, so all clients render the correct reset
        time for offloaded sessions.
        """
        await self.fetch()
        snapshot = await run_sync(self._cluster_snapshot)
        await self._broadcast(snapshot)
        return snapshot

    async def apply_live_event_and_broadcast(
        self,
        rate_limit_type: Optional[str],
        utilization: Optional[float],
        resets_at: Optional[str],
    ) -> None:
        """Apply a live event and, if it changed anything, broadcast the update.

        Used by every streaming path (headless/automation, interactive, and
        offloaded WS) to persist the live refinement into the shared
        ``usage_snapshots`` row — the only cross-install propagation channel, as
        the yielded WS ``rate_limit`` event reaches only clients connected
        directly to this install.
        """
        snapshot = self.apply_live_event(rate_limit_type, utilization, resets_at)
        if snapshot is not None:
            # Persist (so peers see the live change) then broadcast the enriched
            # cluster shape. Both touch the DB — offload off the event loop.
            await run_sync(self._persist_snapshot)
            await self._broadcast(await run_sync(self._cluster_snapshot))


# Global singleton.
usage_limits_monitor = UsageLimitsMonitor()


# Fire-and-forget usage re-poll tasks are retained here until they complete so
# the event loop doesn't garbage-collect a still-pending task ("Task was
# destroyed but it is pending!").
_pending_refresh_tasks: "set[asyncio.Task]" = set()


def schedule_usage_refresh() -> None:
    """Fire-and-forget an immediate usage re-poll + broadcast (best-effort).

    Used after an account switch (manual activate or auto-failover): the active
    account just changed, so the cached usage snapshot belongs to the old
    account. Kick off a fresh poll without blocking the caller on the network
    fetch. The created task is retained in a module-level set until it finishes
    (discarded via a done-callback) so it is not GC'd mid-flight. Never raises.
    """
    try:
        task = usage_limits_monitor.request_refresh()
    except Exception as e:  # noqa: BLE001
        logger.debug(f"Could not schedule usage refresh: {e}")
        return
    if task is None:
        return  # no running event loop (e.g. sync test context)
    _pending_refresh_tasks.add(task)
    task.add_done_callback(_pending_refresh_tasks.discard)
