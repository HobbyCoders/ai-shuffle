"""
Tests for the Claude usage-limits monitor (app/core/usage_limits.py).

Covers:
  * parse_usage_response — the real endpoint shape, the used → remaining flip
    every reading goes through, the scoped-Fable window that only appears in the
    ``limits`` array, overage from ``extra_usage``, and malformed/missing fields.
  * UsageLimitsMonitor.fetch — happy path, missing credential (graceful empty),
    HTTP error and network error (keep last snapshot, warn once).
  * apply_live_event — update an existing window vs append a new one, and the
    used → remaining flip that keeps live events on the snapshot's convention.
  * get_snapshot — on-demand fetch when stale.
"""

import logging
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.core.config import settings
from app.core.usage_limits import (
    UsageLimitsMonitor,
    parse_usage_response,
)


# Trimmed copy of a real ``GET /api/oauth/usage`` 200 response.
REAL_RESPONSE = {
    "five_hour": {"utilization": 12.0, "resets_at": "2026-07-09T05:00:00+00:00"},
    "seven_day": {"utilization": 17.0, "resets_at": "2026-07-13T23:00:00+00:00"},
    "seven_day_opus": None,
    "seven_day_sonnet": None,
    "seven_day_overage_included": None,
    "extra_usage": {"is_enabled": False, "utilization": None},
    "limits": [
        {"kind": "session", "percent": 12, "resets_at": "2026-07-09T05:00:00+00:00", "scope": None},
        {"kind": "weekly_all", "percent": 17, "resets_at": "2026-07-13T23:00:00+00:00", "scope": None},
        {
            "kind": "weekly_scoped",
            "percent": 24,
            "resets_at": "2026-07-13T23:00:00+00:00",
            "scope": {"model": {"id": None, "display_name": "Fable"}, "surface": None},
        },
    ],
}


# ---------------------------------------------------------------------------
# parse_usage_response
# ---------------------------------------------------------------------------

class TestParse:
    def test_parses_top_level_and_scoped_fable(self):
        windows = parse_usage_response(REAL_RESPONSE)
        by_type = {w["type"]: w for w in windows}

        # The endpoint reports percent USED; the snapshot carries REMAINING.
        assert by_type["five_hour"]["utilization"] == 88.0  # 12% used
        assert by_type["five_hour"]["resets_at"] == "2026-07-09T05:00:00+00:00"
        assert by_type["seven_day"]["utilization"] == 83.0  # 17% used
        # Fable weekly limit only exists in the limits[] array (weekly_scoped)
        # and must be surfaced as seven_day_overage_included.
        assert by_type["seven_day_overage_included"]["utilization"] == 76.0  # 24% used

    def test_converts_used_percent_to_remaining(self):
        # A window barely touched (1% used) must read as 99% LEFT, not 1% —
        # the bug this conversion fixes (a window that had just reset showed 0).
        windows = parse_usage_response({"five_hour": {"utilization": 1.0, "resets_at": None}})
        assert windows[0]["utilization"] == 99.0

        # A freshly reset window (0% used) is fully available.
        assert parse_usage_response({"five_hour": {"utilization": 0.0}})[0]["utilization"] == 100.0
        # An exhausted window has nothing left.
        assert parse_usage_response({"five_hour": {"utilization": 100.0}})[0]["utilization"] == 0.0
        # Out-of-range readings clamp instead of producing a negative remainder.
        assert parse_usage_response({"five_hour": {"utilization": 140.0}})[0]["utilization"] == 0.0
        assert parse_usage_response({"five_hour": {"utilization": -5.0}})[0]["utilization"] == 100.0

    def test_limits_array_percent_is_also_converted(self):
        data = {"limits": [{"kind": "session", "percent": 30, "resets_at": "y", "scope": None}]}
        windows = parse_usage_response(data)
        assert windows[0]["utilization"] == 70.0

    def test_top_level_wins_over_limits_array(self):
        data = {
            "five_hour": {"utilization": 40.0, "resets_at": "x"},
            "limits": [{"kind": "session", "percent": 5, "resets_at": "y", "scope": None}],
        }
        windows = parse_usage_response(data)
        by_type = {w["type"]: w for w in windows}
        assert by_type["five_hour"]["utilization"] == 60.0  # 40% used, not the limits[] 5

    def test_overage_from_extra_usage_when_enabled(self):
        data = {"extra_usage": {"is_enabled": True, "utilization": 8.0}}
        windows = parse_usage_response(data)
        by_type = {w["type"]: w for w in windows}
        assert by_type["overage"]["utilization"] == 92.0  # 8% used

    def test_extra_usage_disabled_is_omitted(self):
        data = {"extra_usage": {"is_enabled": False, "utilization": None}}
        assert parse_usage_response(data) == []

    def test_null_and_missing_blocks_are_skipped(self):
        data = {
            "five_hour": None,
            "seven_day": {"utilization": None, "resets_at": "z"},
            "seven_day_opus": {"utilization": 3.0, "resets_at": "w"},
        }
        windows = parse_usage_response(data)
        assert [w["type"] for w in windows] == ["seven_day_opus"]

    def test_empty_and_malformed_do_not_raise(self):
        assert parse_usage_response({}) == []
        assert parse_usage_response({"limits": "not-a-list"}) == []
        assert parse_usage_response({"limits": [None, 5, {"kind": "weird"}]}) == []
        # A non-numeric utilization is dropped, not fatal.
        assert parse_usage_response({"five_hour": {"utilization": "oops"}}) == []

    def test_output_ordered_by_known_types(self):
        data = {
            "seven_day": {"utilization": 1.0},
            "five_hour": {"utilization": 2.0},
        }
        windows = parse_usage_response(data)
        # five_hour precedes seven_day per KNOWN_WINDOW_TYPES ordering.
        assert [w["type"] for w in windows] == ["five_hour", "seven_day"]


# ---------------------------------------------------------------------------
# UsageLimitsMonitor.fetch
# ---------------------------------------------------------------------------

class TestFetchWithoutToken:
    """The Claude CLI's OAuth token is never read, so no usage request is made."""

    @pytest.mark.asyncio
    async def test_refresh_makes_no_request_and_keeps_live_windows(self, monitor):
        with patch("app.core.usage_limits.httpx.AsyncClient") as client:
            monitor.apply_live_event("five_hour", 0.25, None)
            snap = await monitor.fetch()
        client.assert_not_called()
        assert snap["available"] is True
        assert snap["windows"][0]["type"] == "five_hour"

    @pytest.mark.asyncio
    async def test_refresh_without_live_windows_is_unavailable(self, monitor):
        with patch("app.core.usage_limits.httpx.AsyncClient") as client:
            snap = await monitor.fetch()
        client.assert_not_called()
        assert snap["available"] is False and snap["windows"] == []

def _fake_httpx(status_code=200, json_body=None, raise_exc=None):
    """Build a patch target for httpx.AsyncClient used as an async ctx manager."""
    resp = MagicMock()
    resp.status_code = status_code
    resp.json.return_value = json_body or {}

    client = MagicMock()
    if raise_exc is not None:
        client.get = AsyncMock(side_effect=raise_exc)
    else:
        client.get = AsyncMock(return_value=resp)

    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=client)
    ctx.__aexit__ = AsyncMock(return_value=False)
    return MagicMock(return_value=ctx)


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

    def test_bump_generation_strictly_increases(self, monitor):
        g0 = monitor.current_generation()
        g1 = monitor.bump_generation()
        assert g1 > g0
        assert monitor.current_generation() == g1
        assert monitor._snapshot()["generation"] == g1
        # Monotonic across repeated bumps and never behind the epoch clock.
        g2 = monitor.bump_generation()
        assert g2 > g1
        assert g2 >= int(time.time()) - 5


# ---------------------------------------------------------------------------
# apply_live_event / get_snapshot
# ---------------------------------------------------------------------------

class TestLiveEvents:
    def test_updates_existing_window(self, monitor):
        monitor._windows = [{"type": "five_hour", "utilization": 10.0, "resets_at": "a"}]
        snap = monitor.apply_live_event("five_hour", 55.0, "b")
        assert snap is not None
        w = next(w for w in snap["windows"] if w["type"] == "five_hour")
        # 55% used → 45% remaining, the convention every snapshot window carries.
        assert w["utilization"] == 45.0 and w["resets_at"] == "b"
        assert len(snap["windows"]) == 1  # replaced, not appended

    def test_appends_new_window(self, monitor):
        snap = monitor.apply_live_event("seven_day_opus", 30.0, "c")
        assert [w["type"] for w in snap["windows"]] == ["seven_day_opus"]
        assert snap["windows"][0]["utilization"] == 70.0
        assert snap["available"] is True

    def test_normalizes_fraction_utilization_to_remaining_percent(self, monitor):
        # SDK sends a 0.0-1.0 USED fraction; the snapshot stores 0-100 REMAINING.
        snap = monitor.apply_live_event("five_hour", 0.25, "a")
        assert snap["windows"][0]["utilization"] == 75.0
        # A value already in percent (>1) skips the fraction scaling but still flips.
        snap2 = monitor.apply_live_event("seven_day", 17.0, "b")
        w = next(w for w in snap2["windows"] if w["type"] == "seven_day")
        assert w["utilization"] == 83.0

    def test_ignores_incomplete_event(self, monitor):
        assert monitor.apply_live_event(None, 5.0, "x") is None
        assert monitor.apply_live_event("five_hour", None, "x") is None
        assert monitor.apply_live_event("five_hour", "nan", "x") is None

    @pytest.mark.asyncio
    async def test_get_snapshot_uses_cache_when_fresh(self, monitor):
        import time as _t
        monitor._windows = [{"type": "five_hour", "utilization": 1.0, "resets_at": None}]
        monitor._fetched_at = _t.time()
        monitor._available = True
        snap = await monitor.get_snapshot(max_age=300)
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
        # Legacy single-account/API-key mode → no account metadata.
        assert rows[0]["account_id"] is None

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

        # Identical repeat → no-op.
        await monitor.apply_live_event_and_broadcast("five_hour", 0.42, "a")
        assert monitor._persist_snapshot.call_count == 1
        assert monitor._broadcast.await_count == 1

        # A materially changed reading → persists + broadcasts immediately.
        await monitor.apply_live_event_and_broadcast("five_hour", 0.55, "a")
        assert monitor._persist_snapshot.call_count == 2
        assert monitor._broadcast.await_count == 2

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

