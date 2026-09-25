"""
Tests for the Claude usage-limits monitor (app/core/usage_limits.py).

Covers:
  * UsageLimitsMonitor.fetch — makes no request; keeps live windows.
  * apply_live_event — update an existing window vs append a new one, and the
    used → remaining flip that keeps live events on the snapshot's convention.
  * get_snapshot — a read never refreshes (Claude usage is passive).
"""

import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.core import usage_limits
from app.core.config import settings
from app.core.usage_limits import UsageLimitsMonitor


# ---------------------------------------------------------------------------
# UsageLimitsMonitor.fetch
# ---------------------------------------------------------------------------

class TestFetchWithoutToken:
    """The Claude CLI's OAuth token is never read, so no usage request is made."""

    @pytest.mark.asyncio
    async def test_refresh_makes_no_request_and_keeps_live_windows(self, monitor):
        monitor.apply_live_event("five_hour", 0.25, None)
        snap = await monitor.fetch()
        assert snap["available"] is True
        assert snap["windows"][0]["type"] == "five_hour"

    @pytest.mark.asyncio
    async def test_refresh_without_live_windows_is_unavailable(self, monitor):
        snap = await monitor.fetch()
        assert snap["available"] is False and snap["windows"] == []
        # Nothing was attempted upstream, so no attempt or error is reported.
        assert snap["attempted_at"] is None and snap["last_error_kind"] is None

    def test_no_oauth_usage_endpoint_or_parser_remains(self):
        for name in ("USAGE_ENDPOINT", "_OAUTH_BETA_HEADER", "parse_usage_response",
                     "httpx", "schedule_usage_refresh"):
            assert not hasattr(usage_limits, name), name

@pytest.fixture
def monitor():
    return UsageLimitsMonitor()



# ---------------------------------------------------------------------------
# generation guard (stale in-flight poll ordering)
# ---------------------------------------------------------------------------

class TestGenerationGuard:
    def test_snapshot_includes_generation(self, monitor):
        assert monitor._snapshot()["generation"] == monitor.current_generation()

    def test_initial_generation_seeded_from_epoch(self):
        """A fresh monitor (e.g. after a backend restart) must start at an
        epoch-derived generation, NOT 0/near-0. Otherwise an open browser tab
        holding a pre-restart generation would drop every post-restart snapshot
        (they all carry a generation, so the frontend's no-generation escape
        hatch never fires) and the indicator would wedge stale until a reload.
        """
        fresh = UsageLimitsMonitor()
        assert fresh.current_generation() >= int(time.time()) - 5
        # Strictly greater than any plausible pre-restart bump counter.
        assert fresh.current_generation() > 1_000_000


# ---------------------------------------------------------------------------
# apply_live_event / get_snapshot
# ---------------------------------------------------------------------------

class TestLiveEvents:
    def test_updates_existing_window(self, monitor):
        monitor._windows = [{"type": "five_hour", "utilization": 10.0, "resets_at": "a"}]
        snap = monitor.apply_live_event("five_hour", 0.55, "b")
        assert snap is not None
        w = next(w for w in snap["windows"] if w["type"] == "five_hour")
        # 55% used → 45% remaining, the convention every snapshot window carries.
        assert w["utilization"] == pytest.approx(45.0) and w["resets_at"] == "b"
        assert len(snap["windows"]) == 1  # replaced, not appended

    def test_appends_new_window(self, monitor):
        snap = monitor.apply_live_event("seven_day_opus", 0.30, "c")
        assert [w["type"] for w in snap["windows"]] == ["seven_day_opus"]
        assert snap["windows"][0]["utilization"] == pytest.approx(70.0)
        assert snap["available"] is True

    def test_normalizes_fraction_utilization_to_remaining_percent(self, monitor):
        # SDK sends a 0.0-1.0 USED fraction; the snapshot stores 0-100 REMAINING.
        snap = monitor.apply_live_event("five_hour", 0.25, "a")
        assert snap["windows"][0]["utilization"] == 75.0

    def test_over_limit_fraction_is_not_read_as_percent(self, monitor):
        # 105% of the window used -> nothing left (clamped), not ~99% left.
        snap = monitor.apply_live_event("five_hour", 1.05, "1900000000")
        assert snap["windows"][0]["utilization"] == 0.0

    def test_event_dates_only_its_own_window(self, monitor):
        # The snapshot's fetched_at moves with any window; observed_at does not.
        old = int(time.time() * 1000) - 86_400_000
        monitor._windows = [{"type": "seven_day", "utilization": 60.0, "resets_at": None,
                             "observed_at": old}]
        before = int(time.time() * 1000)
        monitor.apply_live_event("five_hour", 0.5, None)
        # An unchanged repeat is not persisted but still dates the window.
        monitor.apply_live_event("five_hour", 0.5, None)
        windows = {w["type"]: w for w in monitor._snapshot()["windows"]}
        assert windows["seven_day"]["observed_at"] == old
        assert windows["five_hour"]["observed_at"] >= before

    def test_ignores_incomplete_event(self, monitor):
        assert monitor.apply_live_event(None, 5.0, "x") is None
        assert monitor.apply_live_event("five_hour", None, "x") is None
        assert monitor.apply_live_event("five_hour", "nan", "x") is None

    @pytest.mark.asyncio
    async def test_get_snapshot_never_refreshes(self, monitor):
        monitor._windows = [{"type": "five_hour", "utilization": 1.0, "resets_at": None}]
        monitor._fetched_at = time.time() - 3600
        monitor._available = True
        snap = await monitor.get_snapshot()
        assert not monitor.refreshing
        assert snap["windows"][0]["utilization"] == 1.0


# ---------------------------------------------------------------------------
# Per-install persistence (usage_snapshots table)
# ---------------------------------------------------------------------------

class TestPersistence:
    @pytest.mark.asyncio
    async def test_no_credential_persists_unavailable_row(self, monitor, mock_db):
        from app.db import database as db
        await monitor.fetch()

        rows = db.get_all_usage_snapshots()
        assert len(rows) == 1
        assert rows[0]["instance_id"] == settings.effective_instance_id
        assert rows[0]["available"] is False
        assert rows[0]["windows"] == []
        # Snapshots carry no Claude account identity.
        assert not any(k.startswith("account_") for k in rows[0])

    @pytest.mark.asyncio
    async def test_apply_live_event_and_broadcast_persists(self, monitor, mock_db):
        # apply_live_event is now pure in-memory; the async broadcast wrapper is
        # what persists (through run_sync) and fans out.
        from app.db import database as db
        monitor._broadcast = AsyncMock()
        await monitor.apply_live_event_and_broadcast("five_hour", 0.25, "a")

        rows = db.get_all_usage_snapshots()
        assert len(rows) == 1
        assert rows[0]["instance_id"] == settings.effective_instance_id
        assert rows[0]["available"] is True
        # SDK used-fraction normalized to a remaining percent, then persisted.
        assert rows[0]["windows"][0]["utilization"] == 75.0
        monitor._broadcast.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_restart_does_not_wipe_persisted_claude_windows(self, mock_db):
        from app.db import database as db
        before = UsageLimitsMonitor()
        before._broadcast = AsyncMock()
        await before.apply_live_event_and_broadcast("seven_day", 0.40, "1900000000")

        # Process restart: a fresh monitor has no in-memory windows.
        after = UsageLimitsMonitor()
        await after.fetch()

        row = next(r for r in db.get_all_usage_snapshots()
                   if r["instance_id"] == settings.effective_instance_id)
        assert row["available"] is True
        assert [w["type"] for w in row["windows"]] == ["seven_day"]
        cluster = UsageLimitsMonitor()._cluster_snapshot()
        assert cluster["available"] is True and cluster["windows"][0]["type"] == "seven_day"

    def test_cluster_self_entry_reflects_memory_over_cached_row(self, monitor, mock_db):
        self_id = settings.effective_instance_id
        monitor._persist_snapshot()
        monitor._cluster_snapshot()  # warm the cache with the empty row
        monitor.apply_live_event("five_hour", 0.25, "a")
        own = next(s for s in monitor._cluster_snapshot()["snapshots"] if s["instance_id"] == self_id)
        assert own["available"] is True and own["windows"][0]["utilization"] == 75.0
        assert own["generation"] == monitor.current_generation()

    def test_apply_live_event_is_in_memory_only(self, monitor, mock_db):
        # The sync refiner must NOT touch the DB — persistence is the async
        # wrapper's job now (Fix 3: no sync DB on the event loop).
        from app.db import database as db
        snap = monitor.apply_live_event("five_hour", 0.42, "a")
        assert snap is not None  # changed → caller should persist/broadcast
        assert db.get_all_usage_snapshots() == []

    @pytest.mark.asyncio
    async def test_unchanged_reading_skips_persist_and_broadcast(self, monitor, mock_db):
        # A tool-use loop emits one rate_limit event per API round-trip; identical
        # repeats must not each cost a DB upsert + cluster fanout (Fix 4).
        monitor._persist_snapshot = MagicMock()
        monitor._broadcast = AsyncMock()

        # First reading: a new window → changed → persist + broadcast.
        await monitor.apply_live_event_and_broadcast("five_hour", 0.42, "a")
        assert monitor._persist_snapshot.call_count == 1
        assert monitor._broadcast.await_count == 1

        # Identical repeat → no persist/broadcast, but the observation time and
        # generation semantics hold: fetched_at advances, generation does not.
        monitor._fetched_at -= 600
        generation = monitor.current_generation()
        await monitor.apply_live_event_and_broadcast("five_hour", 0.42, "a")
        assert monitor._persist_snapshot.call_count == 1
        assert monitor._broadcast.await_count == 1
        assert time.time() - monitor._fetched_at < 5
        assert monitor.current_generation() == generation

        # A materially changed reading → persists + broadcasts immediately
        # under a newer generation, so clients drop older in-flight snapshots.
        await monitor.apply_live_event_and_broadcast("five_hour", 0.55, "a")
        assert monitor._persist_snapshot.call_count == 2
        assert monitor._broadcast.await_count == 2
        assert monitor.current_generation() > generation

    def test_cluster_cache_invalidated_on_own_persist(self, monitor, mock_db):
        from app.db import database as db
        self_id = settings.effective_instance_id

        # Seed + assemble once to populate the short-TTL cluster cache.
        monitor._windows = [{"type": "five_hour", "utilization": 10.0, "resets_at": None}]
        db.upsert_usage_snapshot(
            self_id, windows=monitor._windows, available=True, generation=1, fetched_at=1,
        )
        first = monitor._cluster_snapshot()
        assert monitor._cluster_cache is not None
        assert any(s["instance_id"] == self_id for s in first["snapshots"])

        # A peer row written directly to the DB is masked while the cache is warm.
        db.register_install("peer-x", "Peer", "http://p:1", "docker", "1", {})
        db.upsert_usage_snapshot("peer-x", windows=[], available=True, generation=1, fetched_at=1)
        cached = monitor._cluster_snapshot()
        assert not any(s["instance_id"] == "peer-x" for s in cached["snapshots"])

        # Persisting our OWN row invalidates the cache → peer becomes visible.
        monitor._persist_snapshot()
        after = monitor._cluster_snapshot()
        assert any(s["instance_id"] == "peer-x" for s in after["snapshots"])


    def test_cluster_snapshot_omits_keys_on_db_read_failure(self, monitor):
        # Fix 1: a DB read failure must NOT emit `snapshots: []` alongside valid
        # top-level windows (the frontend's modern branch would wipe all buckets).
        monitor._windows = [{"type": "five_hour", "utilization": 10.0, "resets_at": None}]
        monitor._available = True
        monitor._fetched_at = time.time()

        with patch("app.db.database.get_installs_map", side_effect=RuntimeError("db down")):
            snap = monitor._cluster_snapshot()

        # Legacy fields remain valid for the legacy fold.
        assert snap["available"] is True
        assert snap["windows"][0]["type"] == "five_hour"
        assert snap["generation"] == monitor.current_generation()
        # Cluster keys omitted entirely.
        assert "snapshots" not in snap
        assert "self_instance_id" not in snap

