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


@pytest.mark.asyncio
async def test_live_cli_unified_windows_reach_usage_snapshot(monitor):
    from fluid.engine.delegated_proto import StreamJsonMapper

    mapper = StreamJsonMapper(session_id="s", turn_id="t", model="sonnet", user_node_id="n")
    # Resets still ahead: a passed reset is served as refilled (see TestRollOver).
    five_hour_reset, weekly_reset = int(time.time()) + 3600, int(time.time()) + 4 * 86400
    events = mapper.feed({"type": "rate_limit_event", "rate_limit_info": {
        "status": "allowed", "rateLimitType": "five_hour", "resetsAt": five_hour_reset,
        "unifiedWindows": {
            "five_hour": {"utilization": 0.06, "resetsAt": five_hour_reset},
            "seven_day": {"utilization": 0.4, "resetsAt": weekly_reset},
            "seven_day_overage_included": {"utilization": 0.83, "resetsAt": weekly_reset},
        },
    }}).events
    for event in events:
        monitor.apply_live_event(event.kind, event.utilization, event.resets_at, status=event.status)
    snapshot = await monitor.get_snapshot()
    assert snapshot["available"] is True
    assert {w["type"]: w["utilization"] for w in snapshot["windows"]} == {
        "five_hour": 94.0, "seven_day": 60.0, "seven_day_overage_included": 17.0,
    }



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



# ---------------------------------------------------------------------------
# rejected verdict + observed_at on unchanged repeats
# ---------------------------------------------------------------------------

def _own_row():
    from app.db import database as db
    return next(r for r in db.get_all_usage_snapshots()
                if r["instance_id"] == settings.effective_instance_id)


class TestRejectedStatus:
    def test_rejected_without_utilization_marks_the_window_exhausted(self, monitor):
        before = int(time.time() * 1000)
        generation = monitor.current_generation()
        snap = monitor.apply_live_event("five_hour", None, "1900000000", status="rejected")
        assert snap is not None
        (w,) = snap["windows"]
        # 100% used → 0% remaining, dated like any other reading.
        assert w["type"] == "five_hour" and w["utilization"] == 0.0
        assert w["resets_at"] == "1900000000" and w["observed_at"] >= before
        assert snap["available"] is True and snap["generation"] > generation

    def test_rejected_overrides_a_stale_utilization(self, monitor):
        monitor.apply_live_event("five_hour", 0.4, "a")
        snap = monitor.apply_live_event("five_hour", 0.4, "a", status="rejected")
        assert snap is not None and snap["windows"][0]["utilization"] == 0.0

    def test_other_verdicts_without_utilization_stay_ignored(self, monitor):
        assert monitor.apply_live_event("five_hour", None, "a", status="allowed") is None
        assert monitor.apply_live_event("five_hour", None, "a", status="allowed_warning") is None
        assert monitor.apply_live_event(None, None, "a", status="rejected") is None
        assert monitor._windows == []

    @pytest.mark.asyncio
    async def test_rejected_is_persisted_and_broadcast(self, monitor, mock_db):
        monitor._broadcast = AsyncMock()
        await monitor.apply_live_event_and_broadcast("seven_day", None, "b", status="rejected")
        row = _own_row()
        assert row["windows"][0]["type"] == "seven_day"
        assert row["windows"][0]["utilization"] == 0.0
        assert isinstance(row["windows"][0]["observed_at"], int)
        monitor._broadcast.assert_awaited_once()


class TestObservedAtOnRepeat:
    @pytest.mark.asyncio
    async def test_repeat_persists_only_observed_at_coalesced_per_window(
        self, monitor, mock_db, monkeypatch,
    ):
        # The wall clock is controlled: ``observed_at`` is epoch milliseconds
        # and the row only takes a strictly later one, so on a fast runner a
        # real clock can give a repeat the same millisecond as the first
        # reading (issue #1928).
        clock = [time.time()]
        monkeypatch.setattr(usage_limits.time, "time", lambda: clock[0])
        monitor._broadcast = AsyncMock()
        await monitor.apply_live_event_and_broadcast("five_hour", 0.42, "a")
        await monitor.apply_live_event_and_broadcast("seven_day", 0.10, "b")
        first = _own_row()
        generation = first["generation"]
        assert monitor._broadcast.await_count == 2

        # Same values an hour later: the shared row learns the newer
        # observation time without a new generation or a changed reading.
        clock[0] += 3600
        for key in monitor._observed_persisted:
            monitor._observed_persisted[key] -= 3600
        await monitor.apply_live_event_and_broadcast("five_hour", 0.42, "a")
        row = _own_row()
        five = next(w for w in row["windows"] if w["type"] == "five_hour")
        seven = next(w for w in row["windows"] if w["type"] == "seven_day")
        mem = next(w for w in monitor._windows if w["type"] == "five_hour")
        assert five["observed_at"] == mem["observed_at"]
        assert five["utilization"] == 58.0 and five["resets_at"] == "a"
        # Only that window's time moved; the row keeps its generation.
        assert seven == next(w for w in first["windows"] if w["type"] == "seven_day")
        assert row["generation"] == generation == monitor.current_generation()
        assert row["fetched_at"] >= five["observed_at"]
        assert monitor._broadcast.await_count == 3

        # Further repeats within the minute: in memory only, no write, no fanout.
        touch = MagicMock(wraps=monitor._persist_observed)
        monitor._persist_observed = touch
        for _ in range(5):
            clock[0] += 1
            await monitor.apply_live_event_and_broadcast("five_hour", 0.42, "a")
        assert touch.call_count == 0 and monitor._broadcast.await_count == 3
        assert next(w for w in monitor._windows if w["type"] == "five_hour")["observed_at"] \
            >= five["observed_at"]

        # The other window has its own interval: its first repeat after a
        # minute still writes.
        clock[0] += 60
        monitor._observed_persisted["seven_day"] -= 3600
        await monitor.apply_live_event_and_broadcast("seven_day", 0.10, "b")
        assert touch.call_count == 1 and monitor._broadcast.await_count == 4
        assert next(w for w in _own_row()["windows"] if w["type"] == "seven_day")["observed_at"] \
            == int(clock[0] * 1000)

    @pytest.mark.asyncio
    async def test_repeat_right_after_a_full_persist_does_not_write(self, monitor, mock_db):
        monitor._broadcast = AsyncMock()
        monitor._persist_observed = MagicMock(return_value=True)
        await monitor.apply_live_event_and_broadcast("five_hour", 0.42, "a")
        await monitor.apply_live_event_and_broadcast("five_hour", 0.42, "a")
        monitor._persist_observed.assert_not_called()
        assert monitor._broadcast.await_count == 1


class TestTouchObservedRow:
    def test_touch_never_moves_time_back_and_keeps_the_rest(self, mock_db):
        from app.db import database as db
        windows = [
            {"type": "five_hour", "utilization": 40.0, "resets_at": "a", "observed_at": 2000},
            {"type": "seven_day", "utilization": 90.0, "resets_at": "b", "observed_at": 1000},
        ]
        db.upsert_usage_snapshot("inst-t", windows, True, 7, 2000)
        assert db.touch_usage_snapshot_observed("inst-t", "five_hour", 1500) is False
        assert db.touch_usage_snapshot_observed("inst-t", "seven_day", 3000) is True
        assert db.touch_usage_snapshot_observed("nobody", "seven_day", 3000) is False
        row = next(r for r in db.get_all_usage_snapshots() if r["instance_id"] == "inst-t")
        assert row["windows"] == [windows[0], {**windows[1], "observed_at": 3000}]
        assert row["generation"] == 7 and row["available"] is True
        assert row["fetched_at"] == 3000


# ---------------------------------------------------------------------------
# Passed resets, the weekly Fable bucket, and the network copy
# ---------------------------------------------------------------------------
#
# Sanitized ``rate_limit_event`` frames in the shape Claude Code 2.1.288 emits
# (its stream schema: ``unifiedWindows`` with ``five_hour``, ``seven_day`` and
# ``seven_day_overage_included``, the last "present only for accounts whose
# responses carry that window"). The Fable limit is that per-model bucket, so
# an Opus turn's frame has no ``seven_day_overage_included`` at all.

HOUR, DAY = 3600, 86400


def _opus_frame(now, five_hour_used=0.01, weekly_used=0.05):
    return {"type": "rate_limit_event", "rate_limit_info": {
        "status": "allowed", "rateLimitType": "five_hour", "resetsAt": int(now) + 4 * HOUR,
        "unifiedWindows": {
            "five_hour": {"utilization": five_hour_used, "resetsAt": int(now) + 4 * HOUR},
            "seven_day": {"utilization": weekly_used, "resetsAt": int(now) + 6 * DAY},
        },
    }}


def _fable_frame(now, fable_used, fable_reset):
    frame = _opus_frame(now)
    frame["rate_limit_info"]["unifiedWindows"]["seven_day_overage_included"] = {
        "utilization": fable_used, "resetsAt": fable_reset,
    }
    return frame


def _feed(monitor, frame):
    from fluid.engine.delegated_proto import StreamJsonMapper

    mapper = StreamJsonMapper(session_id="s", turn_id="t", model="opus", user_node_id="n")
    for event in mapper.feed(frame).events:
        monitor.apply_live_event(event.kind, event.utilization, event.resets_at, status=event.status)


def _by_type(windows):
    return {w["type"]: w for w in windows}


class TestRollOver:
    @pytest.mark.parametrize("resets_at,expected", [
        (1_790_000_000, 1_790_000_000.0),
        (1_790_000_000_000, 1_790_000_000.0),
        ("1790000000", 1_790_000_000.0),
        ("2026-09-21T14:13:20Z", 1_790_000_000.0),
        ("2026-09-21T14:13:20", 1_790_000_000.0),  # zone-less is UTC
        (None, None), ("", None), ("soon", None), (True, None), (0, None),
    ])
    def test_reset_epoch_seconds(self, resets_at, expected):
        assert usage_limits.reset_epoch_seconds(resets_at) == expected

    @pytest.mark.asyncio
    async def test_opus_frames_keep_fable_until_its_reset_then_serve_it_refilled(self, monitor):
        now = time.time()
        _feed(monitor, _fable_frame(now, 0.19, int(now) + 2 * DAY))
        assert _by_type((await monitor.get_snapshot())["windows"])["seven_day_overage_included"][
            "utilization"] == 81.0

        # Opus turns only: their frames carry no Fable window, so the last
        # Fable reading stands while its week is still running.
        _feed(monitor, _opus_frame(now))
        fable = _by_type((await monitor.get_snapshot())["windows"])["seven_day_overage_included"]
        assert fable["utilization"] == 81.0

        # The weekly reset passes with no Fable turn: 100% left, not 81%.
        with patch.object(usage_limits.time, "time", return_value=now + 3 * DAY):
            windows = _by_type((await monitor.get_snapshot())["windows"])
        fable = windows["seven_day_overage_included"]
        assert fable["utilization"] == 100.0
        # The reset stays so clients can say it awaits the next report, and the
        # observation time stays the last real report.
        assert fable["resets_at"] == int(now) + 2 * DAY
        assert fable["observed_at"] <= int(now * 1000) + 1000
        # The cache itself no longer holds the pre-reset reading.
        assert _by_type(monitor._windows)["seven_day_overage_included"]["utilization"] == 100.0

    @pytest.mark.asyncio
    async def test_post_reset_fable_report_of_zero_reads_full(self, monitor):
        now = time.time()
        _feed(monitor, _fable_frame(now, 0.19, int(now) - HOUR))
        _feed(monitor, _fable_frame(now, 0.0, int(now) + 7 * DAY))
        fable = _by_type((await monitor.get_snapshot())["windows"])["seven_day_overage_included"]
        assert fable["utilization"] == 100.0 and fable["resets_at"] == int(now) + 7 * DAY

    @pytest.mark.asyncio
    async def test_post_reset_fable_window_without_utilization_never_resurrects_old_value(self, monitor):
        now = time.time()
        _feed(monitor, _fable_frame(now, 0.19, int(now) - HOUR))
        # A frame whose Fable window carries no utilization is not a reading.
        _feed(monitor, _fable_frame(now, None, int(now) + 7 * DAY))
        fable = _by_type((await monitor.get_snapshot())["windows"])["seven_day_overage_included"]
        assert fable["utilization"] == 100.0

    @pytest.mark.asyncio
    async def test_persisted_row_drops_expired_values(self, monitor, mock_db):
        from app.db import database as db
        now = time.time()
        monitor._broadcast = AsyncMock()
        await monitor.apply_live_event_and_broadcast(
            "seven_day_overage_included", 0.19, int(now) + 60)
        with patch.object(usage_limits.time, "time", return_value=now + 120):
            await monitor.fetch()
        row = next(r for r in db.get_all_usage_snapshots()
                   if r["instance_id"] == settings.effective_instance_id)
        assert _by_type(row["windows"])["seven_day_overage_included"]["utilization"] == 100.0

    def test_cluster_read_serves_a_stored_expired_value_as_refilled(self, monitor, mock_db):
        from app.db import database as db
        now = time.time()
        # A row persisted by an older build (or a peer) before the reset.
        db.register_install("peer-1", "DesktopWorker", "http://p:1", "docker", "1.0", {})
        db.upsert_usage_snapshot("peer-1", [
            {"type": "five_hour", "utilization": 40.0, "resets_at": int(now) + HOUR, "observed_at": 1},
            {"type": "seven_day_overage_included", "utilization": 19.0,
             "resets_at": int(now) - DAY, "observed_at": 1},
        ], True, 5, 1)
        peer = next(s for s in monitor._cluster_snapshot()["snapshots"] if s["instance_id"] == "peer-1")
        windows = _by_type(peer["windows"])
        assert windows["five_hour"]["utilization"] == 40.0
        assert windows["seven_day_overage_included"]["utilization"] == 100.0


class TestSeedBeforeLiveEvent:
    @pytest.mark.asyncio
    async def test_first_event_after_restart_keeps_the_stored_fable_window(self, mock_db):
        from app.db import database as db
        now = time.time()
        before = UsageLimitsMonitor()
        before._broadcast = AsyncMock()
        await before.apply_live_event_and_broadcast(
            "seven_day_overage_included", 0.30, int(now) + 2 * DAY)

        # Restart, then an Opus turn reports first: its frame has no Fable window.
        after = UsageLimitsMonitor()
        after._broadcast = AsyncMock()
        await after.apply_live_event_and_broadcast("five_hour", 0.02, int(now) + 4 * HOUR)

        row = next(r for r in db.get_all_usage_snapshots()
                   if r["instance_id"] == settings.effective_instance_id)
        windows = _by_type(row["windows"])
        assert windows["seven_day_overage_included"]["utilization"] == 70.0
        assert windows["five_hour"]["utilization"] == 98.0


class TestNetworkCopy:
    def test_usage_row_is_a_shared_collection_that_a_transfer_may_lack(self):
        from app.network import policy

        coll = policy.collection("usage_snapshots")
        assert coll.mode == policy.MODE_SHARED and coll.keys == ("instance_id",)
        assert "windows" in coll.json_fields and coll.shared_row is None
        # A publisher on an older build serves no page for it.
        assert "usage_snapshots" in policy.SNAPSHOT_OPTIONAL_COLLECTIONS
        assert "usage_snapshots" in policy.SCANNED_COLLECTIONS

    @pytest.mark.asyncio
    async def test_persisting_asks_the_network_to_publish(self, monitor, mock_db):
        monitor._broadcast = AsyncMock()
        with patch("app.network.transport.request_scan") as scan:
            await monitor.apply_live_event_and_broadcast("five_hour", 0.25, None)
        scan.assert_called_with(["usage_snapshots"])

    @pytest.mark.asyncio
    async def test_a_peer_row_landing_rebroadcasts(self, monitor):
        monitor._broadcast = AsyncMock()
        monitor._cluster_cache = (time.time() + 60, [])
        with patch.object(monitor, "_cluster_snapshot", return_value={"windows": []}):
            await monitor.peer_changed_and_broadcast()
        assert monitor._cluster_cache is None
        monitor._broadcast.assert_awaited_once_with({"windows": []})

    def test_applied_peer_row_schedules_the_broadcast(self):
        from app.core.global_events import global_events
        from app.network import startup

        loop = MagicMock()
        loop.is_closed.return_value = False
        with patch.object(global_events, "_loop", loop, create=True), \
                patch("asyncio.run_coroutine_threadsafe") as run:
            startup._on_applied([("usage_snapshots", "peer-1", "upsert")])
        assert run.call_count == 1
        run.call_args.args[0].close()  # never awaited in this test

    def test_cluster_entries_carry_device_ids(self, monitor, mock_db):
        from app.db import database as db
        db.register_install("dev_peer", "DesktopWorker", "http://p:1", "docker", "1.0", {})
        db.upsert_usage_snapshot("dev_peer", [], True, 1, 1)
        installs = {
            "dev_peer": {"id": "dev_peer", "device_name": "DesktopWorker", "online": False},
            settings.effective_instance_id: {"id": "dev_self", "device_name": "Unraid", "online": True},
        }
        with patch.object(db, "get_installs_map", return_value=installs):
            snap = monitor._cluster_snapshot()
        by_id = {s["instance_id"]: s for s in snap["snapshots"]}
        assert by_id["dev_peer"]["device_id"] == "dev_peer"
        # An offline peer's row is still served (with its age), never dropped.
        assert by_id["dev_peer"]["online"] is False and by_id["dev_peer"]["available"] is True
        assert by_id[settings.effective_instance_id]["device_id"] == "dev_self"
        assert snap["self_device_id"] == "dev_self"

    def test_self_device_id_without_a_legacy_instance_alias(self, monitor, mock_db):
        """A member whose join recorded no legacy instance id is keyed only by
        its device id; the self entry still names that device, so sessions it
        owns resolve to its own usage instead of an unreported other device."""
        from app.db import database as db
        db.upsert_usage_snapshot(settings.effective_instance_id, [], True, 1, 1)
        installs = {
            "dev_self": {"id": "dev_self", "device_name": "DesktopWorker", "online": True},
            "dev_peer": {"id": "dev_peer", "device_name": "Unraid", "online": True},
            "ai-shuffle-peer": {"id": "dev_peer", "device_name": "Unraid", "online": True},
        }
        with patch.object(db, "get_installs_map", return_value=installs), \
                patch("app.network.runners.self_device_id", return_value="dev_self"):
            snap = monitor._cluster_snapshot()
        own = next(s for s in snap["snapshots"] if s["instance_id"] == settings.effective_instance_id)
        assert own["device_id"] == "dev_self"
        assert own["device_name"] == "DesktopWorker"
        assert snap["self_device_id"] == "dev_self"

    def test_self_device_id_stays_empty_off_a_network(self, monitor, mock_db):
        from app.db import database as db
        with patch.object(db, "get_installs_map", return_value={}), \
                patch("app.network.runners.self_device_id", return_value=None):
            snap = monitor._cluster_snapshot()
        assert snap["self_device_id"] is None
        assert snap["snapshots"][-1]["instance_id"] == settings.effective_instance_id


# ---------------------------------------------------------------------------
# Reports from AI Shuffle's Claude Code mod (apply_mod_report)
# ---------------------------------------------------------------------------


def _mod_window(kind, percent_used, resets_at=None):
    return {"kind": kind, "percent_used": percent_used, "resets_at": resets_at}


class TestClaudeModReport:
    def test_report_lands_as_remaining_with_its_route_and_time(self, monitor):
        now = time.time()
        reset = int(now) + 4 * HOUR
        snap = monitor.apply_mod_report(
            [_mod_window("five_hour", 23.5, reset), _mod_window("seven_day", 7.0)], now)
        assert snap is not None and snap["available"] is True
        windows = _by_type(snap["windows"])
        assert windows["five_hour"] == {
            "type": "five_hour", "utilization": 76.5, "resets_at": reset,
            "observed_at": int(now * 1000), "source": usage_limits.SOURCE_CLAUDE_MOD,
        }
        assert windows["seven_day"]["utilization"] == 93.0
        assert windows["seven_day"]["resets_at"] is None
        assert monitor._fetched_at == now

    def test_spend_limit_past_100_reads_exhausted(self, monitor):
        snap = monitor.apply_mod_report([_mod_window("five_hour", 140.0)], time.time())
        assert snap["windows"][0]["utilization"] == 0.0

    def test_stream_events_are_marked_with_their_route(self, monitor):
        snap = monitor.apply_live_event("five_hour", 0.25, None)
        assert snap["windows"][0]["source"] == usage_limits.SOURCE_CLI_STREAM

    def test_newer_reading_wins_whichever_route(self, monitor):
        now = time.time()
        monitor.apply_mod_report([_mod_window("five_hour", 10.0)], now)
        # The stream reports the same window afterwards: it replaces the mod's.
        monitor.apply_live_event("five_hour", 0.30, None)
        window = _by_type(monitor._windows)["five_hour"]
        assert window["utilization"] == 70.0
        assert window["source"] == usage_limits.SOURCE_CLI_STREAM
        stream_observed = window["observed_at"]

        # A mod report measured before that event arrives late: dropped.
        generation = monitor.current_generation()
        assert monitor.apply_mod_report([_mod_window("five_hour", 12.0)], now - 5) is None
        assert _by_type(monitor._windows)["five_hour"]["observed_at"] == stream_observed
        assert _by_type(monitor._windows)["five_hour"]["utilization"] == 70.0
        assert monitor.current_generation() == generation

        # A later one wins and names its route.
        snap = monitor.apply_mod_report([_mod_window("five_hour", 31.0)], now + 5)
        window = _by_type(snap["windows"])["five_hour"]
        assert window["utilization"] == 69.0
        assert window["source"] == usage_limits.SOURCE_CLAUDE_MOD
        assert window["observed_at"] == int((now + 5) * 1000)

    def test_unchanged_report_re_dates_and_flags_the_row_once_a_minute(self, monitor):
        now = time.time()
        assert monitor.apply_mod_report([_mod_window("five_hour", 10.0)], now) is not None
        generation = monitor.current_generation()

        # The first repeat after the full report is not due a write: the
        # monitor only marks the time it last wrote, so seed that here.
        monitor._observed_persisted["five_hour"] = time.monotonic()
        assert monitor.apply_mod_report([_mod_window("five_hour", 10.0)], now + 30) is None
        assert monitor._mod_persist_due is False
        assert _by_type(monitor._windows)["five_hour"]["observed_at"] == int((now + 30) * 1000)
        assert monitor.current_generation() == generation

        # A minute on, the same reading is due the shared row's new date.
        monitor._observed_persisted["five_hour"] -= 3600
        assert monitor.apply_mod_report([_mod_window("five_hour", 10.0)], now + 90) is None
        assert monitor._mod_persist_due is True
        assert monitor.current_generation() == generation

    @pytest.mark.parametrize("window", [
        {"kind": "", "percent_used": 5.0},
        {"kind": None, "percent_used": 5.0},
        {"kind": "five_hour", "percent_used": True},
        {"kind": "five_hour", "percent_used": float("nan")},
        {"kind": "five_hour", "percent_used": "5"},
        "five_hour",
    ])
    def test_malformed_windows_are_skipped(self, monitor, window):
        assert monitor.apply_mod_report([window], time.time()) is None
        assert monitor._windows == [] and monitor._available is False

    def test_non_integer_reset_is_dropped_not_kept(self, monitor):
        snap = monitor.apply_mod_report(
            [{"kind": "five_hour", "percent_used": 5.0, "resets_at": "soon"}], time.time())
        assert snap["windows"][0]["resets_at"] is None

    @pytest.mark.asyncio
    async def test_report_persists_and_broadcasts_like_a_stream_event(self, monitor, mock_db):
        monitor._broadcast = AsyncMock()
        now = time.time()
        reset = int(now) + 4 * HOUR
        await monitor.apply_mod_report_and_broadcast([_mod_window("five_hour", 40.0, reset)], now)
        row = _own_row()
        assert row["available"] is True
        assert row["windows"] == [{
            "type": "five_hour", "utilization": 60.0, "resets_at": reset,
            "observed_at": int(now * 1000), "source": usage_limits.SOURCE_CLAUDE_MOD,
        }]
        monitor._broadcast.assert_awaited_once()

        # An unchanged repeat within the minute: memory only.
        persist = MagicMock(wraps=monitor._persist_snapshot)
        monitor._persist_snapshot = persist
        await monitor.apply_mod_report_and_broadcast([_mod_window("five_hour", 40.0, reset)], now + 30)
        assert persist.call_count == 0 and monitor._broadcast.await_count == 1

        # A minute on, the shared row learns the new date, same generation.
        monitor._observed_persisted["five_hour"] -= 3600
        await monitor.apply_mod_report_and_broadcast([_mod_window("five_hour", 40.0, reset)], now + 90)
        assert persist.call_count == 1 and monitor._broadcast.await_count == 2
        row = _own_row()
        assert row["windows"][0]["observed_at"] == int((now + 90) * 1000)
        assert row["generation"] == monitor.current_generation()

    @pytest.mark.asyncio
    async def test_report_after_restart_keeps_the_stored_windows(self, mock_db):
        now = time.time()
        before = UsageLimitsMonitor()
        before._broadcast = AsyncMock()
        await before.apply_live_event_and_broadcast(
            "seven_day_overage_included", 0.30, int(now) + 2 * DAY)

        after = UsageLimitsMonitor()
        after._broadcast = AsyncMock()
        await after.apply_mod_report_and_broadcast([_mod_window("five_hour", 2.0)], now)
        windows = _by_type(_own_row()["windows"])
        assert windows["seven_day_overage_included"]["utilization"] == 70.0
        assert windows["five_hour"]["utilization"] == 98.0


class TestClaudeModUsageSink:
    @pytest.mark.asyncio
    async def test_sink_hands_the_bounded_report_to_the_monitor(self):
        from fluid.engine.claude_mod_usage import parse_plan_usage_report

        now = time.time()
        report = parse_plan_usage_report({
            "v": 1, "measuredAt": int(now * 1000),
            "rateLimits": [{"kind": "five_hour", "percentUsed": 23.46,
                            "resetsAt": int(now) + HOUR}],
        }, now=now)
        with patch.object(usage_limits.usage_limits_monitor, "apply_mod_report_and_broadcast",
                          new=AsyncMock()) as apply:
            await usage_limits.claude_mod_usage_sink(report)
        windows, measured_at = apply.await_args.args
        assert windows == [{"kind": "five_hour", "percent_used": 23.5,
                            "resets_at": int(now) + HOUR}]
        assert measured_at == report.measured_at

    @pytest.mark.asyncio
    async def test_sink_never_raises_into_the_bridge(self):
        with patch.object(usage_limits.usage_limits_monitor, "apply_mod_report_and_broadcast",
                          new=AsyncMock(side_effect=RuntimeError("db down"))):
            await usage_limits.claude_mod_usage_sink(MagicMock(windows=[], measured_at=1.0))
        # Not a report at all.
        await usage_limits.claude_mod_usage_sink(object())
