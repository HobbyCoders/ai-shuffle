"""
Claude usage-limit monitor.

AI Shuffle does not poll any Claude usage endpoint. Claude usage comes from
the running CLI alone (``claude_rate_limit_events`` in
``app.core.usage_sources``), by two routes into this one monitor:

* the ``rate_limit`` events the CLI emits in its own stream. Claude Code
  sends one only when a window moves a whole point or its limit status
  changes, so a window that holds still is never reported again;
* AI Shuffle's Claude Code mod (``fluid/engine/claude_mod``), which posts
  every rate-limit window the CLI measured after each main-thread turn and
  when a window moves (``apply_mod_report_and_broadcast``). That re-dates
  windows that held still, so they do not read as stale while a session runs.

Each window records which route reported it (``source``) and when
(``observed_at``); a reading older than the one on file is dropped, whatever
its route. The monitor keeps the windows in memory, persists them per install
and broadcasts them to connected clients as a ``usage_snapshot`` WebSocket
event. With no Claude session running nothing reports, and the last reading
stays on file (rolled over at each reset, below). The full picture lives on
the user's own usage page (https://claude.ai/settings/usage), which the usage
widget links to.

Windows key on the CLI's ``rate_limit_type`` strings (``five_hour``,
``seven_day``, ``seven_day_opus``, ``seven_day_sonnet``,
``seven_day_overage_included``, ``overage``). Snapshot utilization is quota
REMAINING, 0-100.

``seven_day_overage_included`` is the weekly Fable limit: a per-model bucket
the CLI reports only on responses that carry it (Fable turns), so an
Opus-only week never reports it again. Every window whose ``resets_at`` has
passed is therefore served as refilled (100% remaining, the past reset kept so
clients can say it awaits the next report) instead of as its pre-reset
reading.

On a worker network each device keeps its own database. The ``usage_snapshots``
row is a shared network collection (``app.network.policy``), so a peer serves
the windows of the device that owns a session; ``device_id`` on each cluster
entry lets clients match a session's ``owner_device_id``.

Several devices can be signed in to the same Claude account, and each one's
reading only moves when that device runs a Claude turn. Each row therefore
carries ``account_key``: an opaque hash of the account the device's CLI is
signed in to (``claude auth status``: org id and email), never the email, org
id or any token. The cluster snapshot merges the rows that share a key into
one ``accounts`` entry, window by window, keeping the reading with the newest
``observed_at`` and naming the device that reported it. Rows with no key
(not signed in, or written by an older build) or a key of their own stay
separate, exactly as before; the per-device entries are always served too.
"""

import asyncio
import hashlib
import logging
import math
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from app.db.async_database import run_sync

logger = logging.getLogger(__name__)

# Short TTL for the assembled cluster view (peer snapshot rows + installs map).
# Hot GETs/broadcasts within this window reuse the cached DB reads; the cache is
# invalidated whenever THIS process persists its own row so a local change
# surfaces at once. Bounds the per-event and per-GET DB cost.
_CLUSTER_CACHE_TTL_SECONDS = 4.0

# An unchanged repeat reading still advances the window's ``observed_at`` in
# memory, but writes it to the shared row (and fans it out) at most this often
# per window: a tool-use loop reports every window on every API round-trip.
_OBSERVED_PERSIST_INTERVAL_SECONDS = 60.0

# The CLI's verdict on a window it has cut off. Such an event may carry no
# utilization; the window is exhausted either way.
_REJECTED = "rejected"

# Network collection carrying each device's ``usage_snapshots`` row.
NETWORK_COLLECTION = "usage_snapshots"

# Each window's ``source``: the route that last reported it.
SOURCE_CLI_STREAM = "cli_stream"
SOURCE_CLAUDE_MOD = "claude_mod"

# Domain prefix hashed with the account identity (``account_key_for``).
_ACCOUNT_KEY_DOMAIN = "ai-shuffle:claude-usage-account:v1:"


def account_key_for(status: Any) -> Optional[str]:
    """Opaque key naming the Claude account a CLI status is signed in to.

    Built from the org id and the email ``claude auth status`` printed (both
    when present, else whichever is present), lower-cased and hashed, so equal
    accounts give equal keys while the row carries neither value. None when
    the CLI is not signed in or reports neither. Never reads a token or the
    credential file.
    """
    if status is None or getattr(status, "signed_in", None) is not True:
        return None
    parts = []
    org_id = getattr(status, "org_id", None)
    if isinstance(org_id, str) and org_id.strip():
        parts.append("org:" + org_id.strip().lower())
    email = getattr(status, "email", None)
    if isinstance(email, str) and email.strip():
        parts.append("email:" + email.strip().lower())
    if not parts:
        return None
    digest = hashlib.sha256((_ACCOUNT_KEY_DOMAIN + "|".join(parts)).encode("utf-8"))
    return digest.hexdigest()[:32]


def _observed_stamp(window: Dict[str, Any]) -> float:
    """A window's ``observed_at`` for comparison; an undated one sorts first."""
    seen = window.get("observed_at")
    if isinstance(seen, bool) or not isinstance(seen, (int, float)) or not math.isfinite(seen):
        return -1.0
    return float(seen)


def merge_account_snapshots(snapshots: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """One entry per Claude account reported by two or more devices.

    Window by window, the reading with the newest ``observed_at`` wins, and the
    window names the device that reported it (``instance_id``, ``device_id``,
    ``device_name``). Entries without an ``account_key``, or with a key no other
    entry shares, are left out: they stay the separate per-device buckets.
    """
    groups: Dict[str, List[Dict[str, Any]]] = {}
    for entry in snapshots:
        key = entry.get("account_key")
        if isinstance(key, str) and key:
            groups.setdefault(key, []).append(entry)
    accounts: List[Dict[str, Any]] = []
    for key, members in groups.items():
        if len(members) < 2:
            continue
        best: Dict[str, Tuple[float, Dict[str, Any]]] = {}
        for member in members:
            for window in member.get("windows") or []:
                if not isinstance(window, dict) or not window.get("type"):
                    continue
                stamp = _observed_stamp(window)
                current = best.get(window["type"])
                if current is not None and current[0] >= stamp:
                    continue
                best[window["type"]] = (stamp, {
                    **window,
                    "instance_id": member.get("instance_id"),
                    "device_id": member.get("device_id"),
                    "device_name": member.get("device_name"),
                })
        fetched = [m["fetched_at"] for m in members
                   if isinstance(m.get("fetched_at"), int) and not isinstance(m.get("fetched_at"), bool)]
        generations = [m["generation"] for m in members
                       if isinstance(m.get("generation"), int) and not isinstance(m.get("generation"), bool)]
        accounts.append({
            "account_key": key,
            "instance_ids": [m.get("instance_id") for m in members],
            "device_ids": [m.get("device_id") for m in members if m.get("device_id")],
            "windows": [w for _stamp, w in best.values()],
            "fetched_at": max(fetched) if fetched else None,
            "available": any(bool(m.get("available")) for m in members),
            "generation": max(generations) if generations else None,
        })
    return accounts


def reset_epoch_seconds(resets_at: Any) -> Optional[float]:
    """``resets_at`` (epoch s, epoch ms, numeric string or ISO 8601) as epoch seconds."""
    if resets_at is None or isinstance(resets_at, bool):
        return None
    if isinstance(resets_at, (int, float)):
        stamp = float(resets_at)
    elif isinstance(resets_at, str):
        text = resets_at.strip()
        if not text:
            return None
        try:
            stamp = float(text)
        except ValueError:
            try:
                parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
            except ValueError:
                return None
            # Backend timestamps are UTC; a zone-less one is not local time.
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return parsed.timestamp()
    else:
        return None
    if not math.isfinite(stamp) or stamp <= 0:
        return None
    return stamp / 1000.0 if stamp >= 1e12 else stamp


def roll_over_windows(windows: List[Dict[str, Any]], now: Optional[float] = None) -> List[Dict[str, Any]]:
    """Windows with every passed reset shown as refilled (100% remaining).

    A reading taken before its window reset is not the current value. The
    reset time stays so a client can say the window awaits its next report;
    ``observed_at`` stays the time of the last real report. Returns new dicts
    only for the windows that changed.
    """
    current = time.time() if now is None else now
    out: List[Dict[str, Any]] = []
    for w in windows:
        if isinstance(w, dict):
            reset = reset_epoch_seconds(w.get("resets_at"))
            if reset is not None and reset <= current and w.get("utilization") != 100.0:
                w = {**w, "utilization": 100.0}
        out.append(w)
    return out


class UsageLimitsMonitor:
    """In-memory holder for the latest Claude usage windows the CLI reported."""

    def __init__(self) -> None:
        self._windows: List[Dict[str, Any]] = []
        # Time of the last CLI observation (changed or not), epoch seconds.
        self._fetched_at: float = 0.0
        self._available: bool = False
        # Whether this install's persisted row has been adopted after startup.
        self._seeded: bool = False
        # Monotonic tag on every snapshot so clients can drop out-of-order
        # snapshots.
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
        self._refresh_task: asyncio.Task | None = None
        # Monotonic start of the last refresh, for the forced-refresh cooldown.
        self._refresh_started: float | None = None
        # Per window type: monotonic time its ``observed_at`` last went to the
        # shared row (full persist or an observed-only touch), for coalescing.
        self._observed_persisted: Dict[str, float] = {}
        # Window type whose unchanged repeat is due an observed-only write, set
        # by ``apply_live_event`` for the async wrapper to carry out.
        self._observed_due: Optional[str] = None
        # This install's network device id (installs registry), when known.
        self._self_device_id: Optional[str] = None
        # Set by ``apply_mod_report`` when an unchanged report re-dated the
        # windows and the shared row is due the new dates.
        self._mod_persist_due: bool = False
        # ``account_key_for`` of the CLI's sign-in, once a status could read it.
        self._account_key: Optional[str] = None
        self._account_key_known: bool = False

    def current_generation(self) -> int:
        return self._generation

    def _roll_over(self) -> None:
        """Drop pre-reset readings from the cache once their reset has passed."""
        self._windows = roll_over_windows(self._windows)

    def _snapshot(self) -> Dict[str, Any]:
        self._roll_over()
        return {
            "windows": list(self._windows),
            "fetched_at": self._fetched_at or None,
            # Kept for older clients. Nothing is polled, so there is no
            # upstream attempt or error to report.
            "attempted_at": None,
            "last_error_kind": None,
            "available": self._available,
            "generation": self._generation,
        }

    @property
    def refreshing(self) -> bool:
        return self._refresh_task is not None and not self._refresh_task.done()

    def request_refresh(self, cooldown: float = 0.0) -> asyncio.Task | None:
        """Start (or join) the single background refresh + broadcast.

        Returns None without refreshing within ``cooldown`` seconds of the last
        refresh: the windows only change through live events, which persist and
        broadcast on their own.
        """
        if self.refreshing:
            return self._refresh_task
        now = time.monotonic()
        if self._refresh_started is not None and now - self._refresh_started < cooldown:
            return None
        try:
            self._refresh_task = asyncio.create_task(self._refresh_quietly())
        except RuntimeError:
            return None
        self._refresh_started = now
        return self._refresh_task

    async def _refresh_quietly(self) -> None:
        try:
            await self.refresh_and_broadcast()
        except Exception:  # noqa: BLE001 — background refresh is best-effort
            logger.debug("Claude usage refresh failed")

    def _current_account_key(self, probe: bool) -> Optional[str]:
        """This device's Claude account key, from the CLI status cache.

        ``probe`` runs ``claude auth status`` when nothing is cached yet
        (blocking; only from ``run_sync`` paths). A status that could not read
        the sign-in keeps the last known key.
        """
        status = None
        try:
            from app.core import claude_cli_status
            status = claude_cli_status.peek()
            if status is None and probe:
                status = claude_cli_status.get_status()
        except Exception as e:  # noqa: BLE001 — the key is best-effort
            logger.debug("Claude account key unavailable: %s", e)
        if status is not None and getattr(status, "signed_in", None) is not None:
            self._account_key = account_key_for(status)
            self._account_key_known = True
        return self._account_key

    def _persist_snapshot(self) -> None:
        """Persist this install's snapshot row to the shared DB (best-effort).

        Keyed by ``effective_instance_id`` so peers can serve every install's
        usage. Wrapped so a DB hiccup never breaks the poll or a
        live-event application. Synchronous psycopg2 — async callers MUST run
        this through ``run_sync`` so it never blocks the event loop.
        """
        self._roll_over()
        try:
            from app.core.config import settings
            from app.db import database as db
            fetched_ms = int(self._fetched_at * 1000) if self._fetched_at else None
            db.upsert_usage_snapshot(
                settings.effective_instance_id,
                self._windows,
                self._available,
                self._generation,
                fetched_ms,
                account_key=self._current_account_key(probe=True),
            )
            # Our own row changed — drop the cluster cache so the next assembly
            # re-reads and reflects it immediately.
            self._cluster_cache = None
            now = time.monotonic()
            for w in self._windows:
                if w.get("type"):
                    self._observed_persisted[w["type"]] = now
        except Exception as e:  # noqa: BLE001 — persistence must never break the poll
            logger.debug("Usage-snapshot persist skipped: %s", e)
            return
        _publish_to_network()

    def _persist_observed(self, window_type: str, observed_at: int) -> bool:
        """Write only one window's newer ``observed_at`` to this install's row.

        Best-effort; returns whether the row changed. Synchronous — async
        callers run it through ``run_sync``.
        """
        try:
            from app.core.config import settings
            from app.db import database as db
            touched = db.touch_usage_snapshot_observed(
                settings.effective_instance_id, window_type, observed_at,
            )
        except Exception as e:  # noqa: BLE001 — persistence must never break the stream
            logger.debug("Usage-snapshot observed_at write skipped: %s", e)
            return False
        if touched:
            self._cluster_cache = None
            _publish_to_network()
        return bool(touched)

    def _seed(self, rows: List[Dict[str, Any]], self_id: str) -> None:
        """Adopt this install's persisted reading once after startup.

        Without this a restarted process would serve, and persist, an empty
        snapshot over the last windows the CLI reported.
        """
        if self._seeded:
            return
        self._seeded = True
        row = next((r for r in rows if r.get("instance_id") == self_id), None)
        if row and row.get("windows") and not self._windows:
            self._windows = list(row["windows"])
            self._available = bool(row.get("available"))
            self._fetched_at = (row.get("fetched_at") or 0) / 1000

    def _seed_from_db(self) -> None:
        if self._seeded:
            return
        try:
            from app.core.config import settings
            from app.db import database as db
            self._seed(db.get_all_usage_snapshots(), settings.effective_instance_id)
        except Exception as e:  # noqa: BLE001 — seeding is best-effort
            logger.debug("Usage-snapshot seed skipped: %s", e)

    async def fetch(self) -> Dict[str, Any]:
        """Persist the current windows and return the snapshot. Never raises.

        Makes no request: the Claude windows come only from the rate-limit
        events the CLI reports in its own stream (``apply_live_event``).
        """
        await run_sync(self._seed_from_db)
        if not self._windows:
            self._available = False
        await run_sync(self._persist_snapshot)
        return self._snapshot()

    async def get_snapshot(self) -> Dict[str, Any]:
        """Return the in-memory snapshot; Claude usage is passive, so reads never refresh."""
        return self._snapshot()

    async def get_cluster_snapshot(self) -> Dict[str, Any]:
        """Return the LOCAL snapshot plus every install's persisted snapshot.

        Reads all persisted snapshots and joins the installs registry for
        ``device_name``/``online``. Keeps the legacy top-level fields
        (``windows``/``fetched_at``/``available``/``generation``) intact for old
        clients, and adds:

          * ``snapshots`` — one entry per install (self always included), each
            ``{instance_id, device_name, online, windows, fetched_at,
            available, generation}``.
          * ``self_instance_id`` — this install's ``effective_instance_id``.

        Rows whose ``instance_id`` is not in the installs registry are skipped
        UNLESS the row is self (so a brand-new install with no registry row yet
        still reports its own numbers).
        """
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
        try:
            publishers = db.get_usage_snapshot_publishers()
        except Exception as e:  # noqa: BLE001 — no sync tables off a network
            logger.debug("Usage publisher lookup skipped: %s", e)
            publishers = {}

        own_install = installs_map.get(self_id)
        if own_install is None:
            # A network member's runner row is keyed by its device id; the
            # instance-id alias exists only when its membership recorded a
            # legacy instance id, which some joins left empty. Without this
            # the self entry carried no device id and clients could not match
            # sessions owned by this device to its own usage.
            own_device = _network_self_device_id()
            own_install = installs_map.get(own_device) if own_device else None
            if own_install is None and own_device:
                own_install = {"id": own_device, "device_name": None, "online": True}
        self._self_device_id = own_install.get("id") if own_install else None
        snapshots: List[Dict[str, Any]] = []
        for row in rows:
            iid = row.get("instance_id")
            if iid == self_id:
                install = own_install
            else:
                # The device that signed and published the row names it first:
                # a member whose join recorded no legacy instance id has no
                # instance-id alias in the registry, so its row would
                # otherwise be dropped as a departed install.
                publisher = publishers.get(iid) if iid else None
                if publisher and publisher == self._self_device_id:
                    # A row this device published under an earlier instance
                    # id is stale, not a second device: leave it to the
                    # legacy lookup, which drops it as before.
                    publisher = None
                install =(installs_map.get(publisher) if publisher else None) or \
                    (installs_map.get(iid) if iid else None)
            if install is None and iid != self_id:
                # An install that has left the registry — drop its stale row.
                continue
            snapshots.append({
                "instance_id": iid,
                # The registry keys a network device by its device id AND its
                # legacy instance id; sessions name their owner by device id.
                "device_id": install.get("id") if install else None,
                "device_name": install.get("device_name") if install else None,
                "online": bool(install.get("online")) if install else False,
                "windows": roll_over_windows(row.get("windows") or [], now),
                "fetched_at": row.get("fetched_at"),
                "available": bool(row.get("available")),
                "generation": row.get("generation"),
                "account_key": row.get("account_key"),
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
        from app.core.config import settings
        self_id = settings.effective_instance_id

        snapshots = self._read_cluster_rows(self_id)
        if snapshots is not None:
            self._seed(snapshots, self_id)
        base = self._snapshot()
        if snapshots is None:
            # DB read failed — return the plain legacy snapshot only.
            return base

        # Memory is authoritative for our own entry: the (cached) DB row can
        # lag a just-applied live event.
        own = next((s for s in snapshots if s["instance_id"] == self_id), None)
        account_key = self._current_account_key(probe=False)
        if not self._account_key_known:
            account_key = (own or {}).get("account_key")
        own = {
            "instance_id": self_id,
            "device_id": (own or {}).get("device_id") or self._self_device_id,
            "device_name": own["device_name"] if own else None,
            "online": own["online"] if own else False,
            "windows": base["windows"],
            "fetched_at": int(self._fetched_at * 1000) if self._fetched_at else None,
            "available": self._available,
            "generation": self._generation,
            "account_key": account_key,
        }
        base["snapshots"] = [s for s in snapshots if s["instance_id"] != self_id] + [own]
        base["self_instance_id"] = self_id
        base["self_device_id"] = own["device_id"]
        base["accounts"] = merge_account_snapshots(base["snapshots"])
        return base

    def apply_live_event(
        self,
        rate_limit_type: Optional[str],
        utilization: Optional[float],
        resets_at: Optional[str],
        status: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        """Refine a single window from a live CLI ``rate_limit`` event (in-memory).

        Returns the updated snapshot when the reading MATERIALLY changed (i.e.
        needs persist + broadcast), else None — an identical repeat (same type,
        utilization and resets_at) only advances the window's ``observed_at``
        in memory, so a tool-use loop's per-round-trip events don't each cost a
        DB upsert + cluster fanout; at most once a minute per window such a
        repeat is flagged for an observed-only write (``_observed_due``). A
        ``rejected`` status marks the window exhausted (0% remaining) even
        without a utilization. Does NOT persist or broadcast — async callers do
        that via ``run_sync`` (see ``apply_live_event_and_broadcast`` /
        ``refresh_and_broadcast``).
        """
        self._observed_due = None
        if not rate_limit_type:
            return None
        if status == _REJECTED:
            util = 0.0
        else:
            if utilization is None:
                return None
            try:
                util = float(utilization)
            except (TypeError, ValueError):
                return None
            if not math.isfinite(util):
                return None
            # CLI rate_limit events report utilization as a 0.0-1.0 USED fraction
            # (above 1 when over the limit); the snapshot carries 0-100 REMAINING.
            util = 100.0 - min(100.0, max(0.0, util * 100))

        now = time.time()
        # Each event observes one window; ``observed_at`` (epoch ms) lets
        # clients tell a fresh window from one last reported long ago.
        updated = {"type": rate_limit_type, "utilization": util, "resets_at": resets_at,
                   "observed_at": int(now * 1000), "source": SOURCE_CLI_STREAM}
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
        self._fetched_at = now
        if not changed:
            mono = time.monotonic()
            last = self._observed_persisted.get(rate_limit_type)
            if last is None or mono - last >= _OBSERVED_PERSIST_INTERVAL_SECONDS:
                # Claimed now, before the write, so concurrent repeats within
                # the interval do not each write.
                self._observed_persisted[rate_limit_type] = mono
                self._observed_due = rate_limit_type
            return None
        self._generation = max(self._generation + 1, int(now))
        return self._snapshot()

    def apply_mod_report(
        self,
        windows: List[Dict[str, Any]],
        observed_at: float,
    ) -> Optional[Dict[str, Any]]:
        """Apply the windows AI Shuffle's Claude Code mod reported (in-memory).

        ``windows`` are ``{"kind", "percent_used", "resets_at"}`` as fluid's
        ``parse_plan_usage_report`` bounded them (percent USED, 0-100 or
        past 100 on a spend limit; reset in epoch seconds or None), measured
        at ``observed_at`` (epoch seconds). A window already on file with a
        later ``observed_at`` keeps its reading, whichever route reported it.

        Returns the snapshot when a reading changed (persist + broadcast), else
        None. An unchanged report still re-dates its windows in memory and, at
        most once a minute, flags the shared row for the new dates
        (``_mod_persist_due``). Does NOT persist or broadcast.
        """
        self._mod_persist_due = False
        observed_ms = int(observed_at * 1000)
        applied: List[str] = []
        changed = False
        for window in windows:
            if not isinstance(window, dict):
                continue
            kind = window.get("kind")
            used = window.get("percent_used")
            if not isinstance(kind, str) or not kind:
                continue
            if isinstance(used, bool) or not isinstance(used, (int, float)) or not math.isfinite(used):
                continue
            resets_at = window.get("resets_at")
            if isinstance(resets_at, bool) or not isinstance(resets_at, (int, type(None))):
                resets_at = None
            util = round(100.0 - min(100.0, max(0.0, float(used))), 1)
            index = next((i for i, w in enumerate(self._windows) if w.get("type") == kind), None)
            current = self._windows[index] if index is not None else None
            if current is not None:
                seen = current.get("observed_at")
                if isinstance(seen, int) and seen > observed_ms:
                    continue  # a newer reading is on file
                if current.get("utilization") != util or current.get("resets_at") != resets_at:
                    changed = True
            else:
                changed = True
            updated = {"type": kind, "utilization": util, "resets_at": resets_at,
                       "observed_at": observed_ms, "source": SOURCE_CLAUDE_MOD}
            if index is None:
                self._windows.append(updated)
            else:
                self._windows[index] = updated
            applied.append(kind)
        if not applied:
            return None
        self._available = True
        self._fetched_at = max(self._fetched_at, observed_at)
        if changed:
            self._generation = max(self._generation + 1, int(time.time()))
            return self._snapshot()
        mono = time.monotonic()
        if any(
            self._observed_persisted.get(kind) is None
            or mono - self._observed_persisted[kind] >= _OBSERVED_PERSIST_INTERVAL_SECONDS
            for kind in applied
        ):
            for kind in applied:
                self._observed_persisted[kind] = mono
            self._mod_persist_due = True
        return None

    async def apply_mod_report_and_broadcast(
        self, windows: List[Dict[str, Any]], observed_at: float,
    ) -> None:
        """Apply a mod report and, when it changed or re-dated the row, persist and broadcast.

        The same store, row, peer publication and broadcast as the stream's
        events (``apply_live_event_and_broadcast``).
        """
        await run_sync(self._seed_from_db)
        snapshot = self.apply_mod_report(windows, observed_at)
        due, self._mod_persist_due = self._mod_persist_due, False
        if snapshot is None and not due:
            return
        await run_sync(self._persist_snapshot)
        await self._broadcast(await run_sync(self._cluster_snapshot))

    async def _broadcast(self, snapshot: Dict[str, Any]) -> None:
        """Fan the snapshot out to all connected clients (best-effort)."""
        try:
            from app.core.sync_engine import sync_engine
            await sync_engine.broadcast_usage_snapshot(snapshot)
        except Exception as e:  # noqa: BLE001 — broadcast is best-effort
            logger.debug("Usage-snapshot broadcast skipped: %s", e)

    async def refresh_and_broadcast(self) -> Dict[str, Any]:
        """Persist the latest snapshot and broadcast it.

        Broadcasts the enriched CLUSTER snapshot (every install's data), not just this install's, so all clients render the correct reset
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
        status: Optional[str] = None,
    ) -> None:
        """Apply a live event and, if it changed anything, broadcast the update.

        Used by every streaming path (headless/automation, interactive, and
        offloaded WS) to persist the live refinement into the shared
        ``usage_snapshots`` row — the only cross-install propagation channel, as
        the yielded WS ``rate_limit`` event reaches only clients connected
        directly to this install. An unchanged repeat writes only the window's
        ``observed_at`` (coalesced to once a minute per window) and broadcasts
        under the same generation, so every device shows the latest
        observation time.
        """
        # Adopt the persisted row first: after a restart the first event would
        # otherwise persist only its own windows over the stored ones (the
        # weekly Fable reading among them), and they vanish from every client.
        await run_sync(self._seed_from_db)
        snapshot = self.apply_live_event(rate_limit_type, utilization, resets_at, status=status)
        if snapshot is not None:
            # Persist (so peers see the live change) then broadcast the enriched
            # cluster shape. Both touch the DB — offload off the event loop.
            await run_sync(self._persist_snapshot)
            await self._broadcast(await run_sync(self._cluster_snapshot))
            return
        due, self._observed_due = self._observed_due, None
        if due is None:
            return
        observed_at = next(
            (w.get("observed_at") for w in self._windows if w.get("type") == due), None,
        )
        if not isinstance(observed_at, int):
            return
        if await run_sync(self._persist_observed, due, observed_at):
            await self._broadcast(await run_sync(self._cluster_snapshot))

    async def peer_changed_and_broadcast(self) -> None:
        """A network peer's usage row landed here: show it on every open client."""
        self._cluster_cache = None
        await self._broadcast(await run_sync(self._cluster_snapshot))


async def claude_mod_usage_sink(report: Any) -> None:
    """Take a plan usage report from a delegated Claude session's mod.

    ``report`` is fluid's ``PlanUsageReport``, already validated and bounded
    by the bridge route that received it. Never raises into the bridge.
    """
    try:
        windows = [
            {"kind": w.kind, "percent_used": w.percent_used, "resets_at": w.resets_at}
            for w in report.windows
        ]
        await usage_limits_monitor.apply_mod_report_and_broadcast(windows, report.measured_at)
    except Exception as e:  # noqa: BLE001 — a usage write must never reach the turn
        logger.debug("Claude mod usage report skipped: %s", e)


def _network_self_device_id() -> Optional[str]:
    """This worker's network device id, or None off a network (best-effort)."""
    try:
        from app.network import runners
        return runners.self_device_id()
    except Exception as e:  # noqa: BLE001 — a network fault must never break usage reads
        logger.debug("Network device id unavailable: %s", e)
        return None


def _publish_to_network() -> None:
    """Ask the network scanner to publish this device's usage row (best-effort).

    A no-op off a worker network; the scanner compares content hashes, so an
    unchanged row publishes nothing.
    """
    try:
        from app.network import transport
        transport.request_scan([NETWORK_COLLECTION])
    except Exception as e:  # noqa: BLE001 — publishing must never break the stream
        logger.debug("Usage-snapshot network publish skipped: %s", e)


# Global singleton.
usage_limits_monitor = UsageLimitsMonitor()

