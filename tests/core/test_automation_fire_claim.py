"""
Phase 2.2 — cross-install scheduled-fire claim + atomic run accounting.

Two installs share one Postgres, every install schedules every enabled
automation, and the runner election deliberately fails open — so one
scheduled tick can be attempted on both installs. These tests prove:

* the ``automation_fires`` primary-key claim admits exactly one winner per
  (automation_id, fire_bucket) — including under the SQLite test shim, whose
  ``ON CONFLICT DO NOTHING`` + rowcount behaviour the claim relies on;
* the scheduled path is gated on the claim while manual triggers are not;
* the fire bucket names the scheduled tick identically on every install;
* run accounting is one atomic SQL UPDATE, not a lossy read-modify-write.
"""

from tests.conftest import async_twin
import asyncio
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.core.automation_manager import AutomationManager
from app.db import database


def _make_automation(automation_id: str, **overrides):
    """Create an enabled cron automation against the test database."""
    database.create_automation(
        automation_id=automation_id,
        name=f"Auto {automation_id}",
        trigger_type="cron",
        trigger_config={"expression": "0 * * * *"},
        prompt="do it",
        profile_id="default",
        **overrides,
    )
    database.update_automation(automation_id, is_enabled=True)
    return database.get_automation(automation_id)


def _capture_tracker(manager: AutomationManager, coros: list):
    """Replace the task factory so fires are captured instead of scheduled."""
    def fake_track(coro, *, name):
        coros.append((name, coro))
        return MagicMock()
    manager._create_tracked_task = fake_track


def _close_captured(coros: list) -> None:
    for _name, coro in coros:
        coro.close()


# =============================================================================
# Claim primitive (the shim must honour ON CONFLICT DO NOTHING + rowcount)
# =============================================================================


class TestClaimPrimitive:
    def test_first_claim_wins(self, mock_db):
        assert database.claim_automation_fire("auto-a", "cron:2026-07-06T12:00Z") is True

    def test_second_claim_for_same_bucket_loses(self, mock_db):
        assert database.claim_automation_fire("auto-a", "cron:2026-07-06T12:00Z") is True
        assert database.claim_automation_fire("auto-a", "cron:2026-07-06T12:00Z") is False

    def test_different_buckets_both_win(self, mock_db):
        assert database.claim_automation_fire("auto-a", "cron:2026-07-06T12:00Z") is True
        assert database.claim_automation_fire("auto-a", "cron:2026-07-06T13:00Z") is True

    def test_different_automations_share_a_bucket(self, mock_db):
        assert database.claim_automation_fire("auto-a", "cron:2026-07-06T12:00Z") is True
        assert database.claim_automation_fire("auto-b", "cron:2026-07-06T12:00Z") is True

    def test_parallel_claims_exactly_one_winner(self, tmp_path):
        """Two installs racing for the same tick: the PK admits one row.

        Two installs means two independent connections to one shared database,
        so this test uses a file-backed SQLite database with one connection
        per thread (the default in-memory shim shares a single sqlite3
        connection, which cannot be driven from two threads at once — that is
        a limitation of the C sqlite3 binding, not of the claim SQL)."""
        import sqlite3

        from app.db import base
        from app.db.schema_postgres import create_postgres_schema
        from tests.pg_shim import ShimConnection, ShimCursor

        db_file = str(tmp_path / "shared.db")
        setup = sqlite3.connect(db_file)
        setup.row_factory = sqlite3.Row
        create_postgres_schema(ShimCursor(setup.cursor()))
        setup.commit()
        setup.close()

        local = threading.local()
        opened = []

        def per_thread_connection():
            if not hasattr(local, "conn"):
                # check_same_thread=False only so the main thread can close
                # them afterwards; each connection is used by one thread.
                raw = sqlite3.connect(db_file, timeout=5, check_same_thread=False)
                raw.row_factory = sqlite3.Row
                opened.append(raw)
                local.conn = ShimConnection(raw)
            return local.conn

        barrier = threading.Barrier(2)

        def attempt(_):
            barrier.wait()
            return database.claim_automation_fire("auto-race", "cron:2026-07-06T09:00Z")

        try:
            with patch.object(base, "get_connection", per_thread_connection):
                with ThreadPoolExecutor(max_workers=2) as pool:
                    results = list(pool.map(attempt, range(2)))
        finally:
            for raw in opened:
                raw.close()
        assert sorted(results) == [False, True]

    def test_prune_removes_only_old_claims(self, mock_db):
        old = (datetime.now(timezone.utc) - timedelta(days=8)).isoformat()
        mock_db.execute(
            "INSERT INTO automation_fires (automation_id, fire_bucket, claimed_at) VALUES (?, ?, ?)",
            ("auto-old", "cron:2026-06-28T12:00Z", old),
        )
        assert database.claim_automation_fire("auto-new", "cron:2026-07-06T12:00Z") is True

        assert database.prune_automation_fires() == 1

        rows = mock_db.execute("SELECT automation_id FROM automation_fires").fetchall()
        assert [r["automation_id"] for r in rows] == ["auto-new"]


# =============================================================================
# Fire-bucket derivation (must be identical across installs for one tick)
# =============================================================================


class TestFireBucketDerivation:
    def test_cron_recovers_scheduled_minute_despite_jitter(self):
        early = datetime(2026, 7, 6, 11, 59, 59, 800000, tzinfo=timezone.utc)
        late = datetime(2026, 7, 6, 12, 0, 0, 300000, tzinfo=timezone.utc)
        assert (
            AutomationManager._fire_bucket("cron", {"expression": "0 * * * *"}, early)
            == AutomationManager._fire_bucket("cron", {"expression": "0 * * * *"}, late)
            == "cron:2026-07-06T12:00Z"
        )

    def test_cron_distinct_ticks_get_distinct_buckets(self):
        a = datetime(2026, 7, 6, 12, 0, 1, tzinfo=timezone.utc)
        b = datetime(2026, 7, 6, 12, 1, 1, tzinfo=timezone.utc)
        assert AutomationManager._fire_bucket("cron", {}, a) != AutomationManager._fire_bucket("cron", {}, b)

    def test_interval_same_window_same_bucket_across_phases(self):
        """Installs start their schedulers at different moments, so interval
        fires land at different offsets — the epoch-aligned window is shared."""
        config = {"seconds": 3600}
        a = datetime(2026, 7, 6, 12, 5, 0, tzinfo=timezone.utc)
        b = datetime(2026, 7, 6, 12, 50, 0, tzinfo=timezone.utc)
        assert (
            AutomationManager._fire_bucket("interval", config, a)
            == AutomationManager._fire_bucket("interval", config, b)
            == "interval:3600:2026-07-06T12:00:00Z"
        )

    def test_interval_next_window_gets_new_bucket(self):
        config = {"seconds": 3600}
        a = datetime(2026, 7, 6, 12, 5, 0, tzinfo=timezone.utc)
        b = datetime(2026, 7, 6, 13, 5, 0, tzinfo=timezone.utc)
        assert AutomationManager._fire_bucket("interval", config, a) != AutomationManager._fire_bucket("interval", config, b)


# =============================================================================
# Scheduled fires are gated on the claim
# =============================================================================


class TestScheduledFireClaim:
    @pytest.mark.asyncio
    async def test_concurrent_double_fire_exactly_one_executes(self, mock_db):
        """Two installs (two managers) fire the same tick: one wins the claim
        and creates the execution task, the other skips."""
        _make_automation("auto-df")
        fired = []
        m1, m2 = AutomationManager(), AutomationManager()
        for m in (m1, m2):
            _capture_tracker(m, fired)
            # Pin the bucket so the test can't straddle a real minute boundary.
            m._fire_bucket = lambda *a: "cron:2026-07-06T12:00Z"

        with patch(
            "app.core.automation_service.should_run_scheduled_here",
            return_value=(True, "elected runner"),
        ):
            await asyncio.gather(
                m1._on_schedule_trigger("auto-df", "cron", {"expression": "0 * * * *"}),
                m2._on_schedule_trigger("auto-df", "cron", {"expression": "0 * * * *"}),
            )

        assert [name for name, _ in fired] == ["auto-schedule-auto-df"]
        _close_captured(fired)

    @pytest.mark.asyncio
    async def test_different_buckets_both_fire(self, mock_db):
        _make_automation("auto-two")
        fired = []
        manager = AutomationManager()
        _capture_tracker(manager, fired)
        buckets = iter(["cron:2026-07-06T12:00Z", "cron:2026-07-06T13:00Z"])
        manager._fire_bucket = lambda *a: next(buckets)

        with patch(
            "app.core.automation_service.should_run_scheduled_here",
            return_value=(True, "elected runner"),
        ):
            await manager._on_schedule_trigger("auto-two", "cron", {"expression": "0 * * * *"})
            await manager._on_schedule_trigger("auto-two", "cron", {"expression": "0 * * * *"})

        assert len(fired) == 2
        _close_captured(fired)

    @pytest.mark.asyncio
    async def test_claim_error_fails_open(self, mock_db):
        """An unreadable claim table must not drop the fire (it is equally
        unreadable on the other installs — running twice beats never)."""
        _make_automation("auto-open")
        fired = []
        manager = AutomationManager()
        _capture_tracker(manager, fired)

        with patch(
            "app.core.automation_service.should_run_scheduled_here",
            return_value=(True, "elected runner"),
        ), patch(
            "app.db.database.claim_automation_fire",
            side_effect=RuntimeError("claim table unreachable"),
        ):
            await manager._on_schedule_trigger("auto-open", "cron", {"expression": "0 * * * *"})

        assert len(fired) == 1
        _close_captured(fired)

    @pytest.mark.asyncio
    async def test_budget_exceeded_after_claim_consumes_tick_without_running(
        self, mock_db, mock_encryption
    ):
        """The claim row stays (the tick is consumed) but nothing executes:
        budget counters live in the shared database, so an over-budget fire
        here is over budget on every install."""
        _make_automation("auto-bud", max_cost_total=0.5)
        database.update_automation("auto-bud", total_cost_used=1.0)
        fired = []
        manager = AutomationManager()
        _capture_tracker(manager, fired)
        manager._fire_bucket = lambda *a: "cron:2026-07-06T15:00Z"

        with patch(
            "app.core.automation_service.should_run_scheduled_here",
            return_value=(True, "elected runner"),
        ), patch(
            "app.core.query_engine.execute_query", new_callable=AsyncMock
        ) as eq:
            await manager._on_schedule_trigger("auto-bud", "cron", {"expression": "0 * * * *"})
            assert len(fired) == 1
            await fired[0][1]  # run the captured _execute — hits check_budget

        eq.assert_not_awaited()
        runs = database.get_automation_runs("auto-bud")
        assert [r["status"] for r in runs] == ["budget_exceeded"]
        claims = mock_db.execute(
            "SELECT fire_bucket FROM automation_fires WHERE automation_id = ?", ("auto-bud",)
        ).fetchall()
        assert [c["fire_bucket"] for c in claims] == ["cron:2026-07-06T15:00Z"]


# =============================================================================
# Manual triggers bypass the claim (only scheduled ticks are shared state)
# =============================================================================


class TestManualTriggerUnaffected:
    @pytest.mark.asyncio
    async def test_manual_trigger_requires_no_claim(self):
        fired = []
        manager = AutomationManager()
        _capture_tracker(manager, fired)
        with patch("app.core.automation_manager.database") as db:
            db.get_automation.return_value = {"id": "auto-man", "is_enabled": True}
            result = await manager.trigger_manual("auto-man")

        assert result == {"automation_id": "auto-man", "status": "triggered"}
        assert len(fired) == 1
        db.claim_automation_fire.assert_not_called()
        _close_captured(fired)


# =============================================================================
# Atomic run accounting
# =============================================================================


class TestAtomicRunAccounting:
    def test_success_updates_counters_and_health(self, mock_db):
        _make_automation("auto-inc")
        database.update_automation(
            "auto-inc", run_count=3, total_cost_used=1.5, status="running", last_error="old"
        )

        database.record_automation_run_success("auto-inc", 0.25)

        auto = database.get_automation("auto-inc")
        assert auto["run_count"] == 4
        assert auto["total_cost_used"] == pytest.approx(1.75)
        assert auto["status"] == "idle"
        assert auto["last_error"] is None
        assert auto["last_run_at"] is not None

    def test_null_counters_are_treated_as_zero(self, mock_db):
        """NULL budget columns mean unlimited; NULL usage counters must not
        poison the increment (COALESCE, not Python .get defaults)."""
        _make_automation("auto-null")
        database.update_automation("auto-null", total_cost_used=None)

        database.record_automation_run_success("auto-null", 0.1)

        auto = database.get_automation("auto-null")
        assert auto["run_count"] == 1
        assert auto["total_cost_used"] == pytest.approx(0.1)

    def test_concurrent_increments_are_not_lost(self, mock_db):
        """Both runs loaded the row at run_count=0; the old read-modify-write
        would land on 1 — the SQL-side increment must land on 2."""
        _make_automation("auto-race2")
        stale_row_a = database.get_automation("auto-race2")
        stale_row_b = database.get_automation("auto-race2")
        assert stale_row_a["run_count"] == stale_row_b["run_count"] == 0

        database.record_automation_run_success("auto-race2", 0.10)
        database.record_automation_run_success("auto-race2", 0.20)

        auto = database.get_automation("auto-race2")
        assert auto["run_count"] == 2
        assert auto["total_cost_used"] == pytest.approx(0.30)

    @pytest.mark.asyncio
    async def test_execute_run_uses_the_atomic_helper(self):
        """The manager's success path must delegate to the single-UPDATE
        helper and never push Python-computed counters into update_automation."""
        manager = AutomationManager()
        automation = {
            "id": "auto-at",
            "profile_id": "default",
            "project_id": None,
            "action_steps": [],
            "run_count": 0,
            "total_cost_used": 0,
            "max_turns": 1,
            "timeout_seconds": 60,
            "output_config": {"type": "none"},
        }
        # V2: runs execute as normal streaming sessions via turn_manager.submit;
        # the cost/tokens are derived from the durable session-event log's
        # terminal ``turn.completed`` usage (``_final_turn_outcome``), and the
        # response text from the same log (``_extract_final_assistant_text``).
        submit = MagicMock(return_value="turn-at")
        outcome = {
            "usage": {"total_cost_usd": 0.25, "tokens_in": 1, "tokens_out": 2},
            "error": None,
            "interrupted": False,
        }

        with patch("app.core.automation_manager.database") as db, patch(
            "app.core.turn_manager.submit", submit,
        ), patch(
            "app.core.turn_manager.submit_async", new=async_twin(submit),
        ), patch(
            "app.core.turn_manager.wait_session_idle",
            new=AsyncMock(return_value=True),
        ), patch("app.core.sync_engine.sync_engine", new=AsyncMock()), patch.object(
            AutomationManager, "_final_turn_outcome", return_value=outcome,
        ), patch.object(
            AutomationManager, "_extract_final_assistant_text", return_value="done",
        ):
            db.update_automation_run.return_value = {}
            db.update_automation.return_value = {}
            await manager._execute_run(automation, "run-at", "do it", {})

        db.record_automation_run_success.assert_called_once_with("auto-at", 0.25)
        for call in db.update_automation.call_args_list:
            assert "run_count" not in call.kwargs
            assert "total_cost_used" not in call.kwargs
        # Submitted exactly one server-originated automation turn.
        submit.assert_called_once()
        assert submit.call_args.args[1] == "automation"
