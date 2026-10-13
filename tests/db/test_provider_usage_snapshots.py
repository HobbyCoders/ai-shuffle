"""Tests for the per-provider usage-snapshot DB helpers
(app/db/provider_usage_snapshots.py).

Covers the upsert (insert then replace on conflict), the ``windows``/``credits``/
``raw`` JSON round-trip through the pg_shim (stored as JSON blobs, read back as
list/dict), None credits/raw round-trip, and delete. Also verifies the
idempotent ``_run_column_migrations`` block re-creates the table on a fresh DB
that is missing it. Runs on the in-memory SQLite shim via the autouse
``_auto_db_shim`` fixture (documented here by depending on ``mock_db``).
"""

from app.db import database as db


class TestUpsertProviderUsageSnapshot:
    def test_insert_then_replace_on_conflict(self, mock_db):
        db.upsert_provider_usage_snapshot(
            provider_id="prov-A",
            provider_type="chatgpt",
            plan_type="plus",
            windows=[{"type": "five_hour", "utilization": 12.0, "resets_at": "x"}],
            credits={"has_credits": True, "balance": 5.0, "unlimited": False},
            raw={"x-codex-plan-type": "plus"},
            fetched_at=1234567890000,
        )

        rows = db.get_all_provider_usage_snapshots()
        assert len(rows) == 1
        row = rows[0]
        assert row["provider_id"] == "prov-A"
        assert row["provider_type"] == "chatgpt"
        assert row["plan_type"] == "plus"
        assert row["fetched_at"] == 1234567890000
        assert row["windows"][0]["type"] == "five_hour"
        assert row["credits"] == {"has_credits": True, "balance": 5.0, "unlimited": False}
        assert row["raw"] == {"x-codex-plan-type": "plus"}

        # A second upsert for the same provider_id REPLACES the row (no dup).
        db.upsert_provider_usage_snapshot(
            provider_id="prov-A",
            provider_type="openrouter",
            plan_type=None,
            windows=[{"type": "seven_day", "utilization": 55.0, "resets_at": "y"}],
            credits={"limit": 10.0, "usage": 2.5},
            raw={"limit": 10.0},
            fetched_at=999,
        )
        rows = db.get_all_provider_usage_snapshots()
        assert len(rows) == 1
        row = rows[0]
        assert row["provider_type"] == "openrouter"
        assert row["plan_type"] is None
        assert row["fetched_at"] == 999
        assert row["windows"][0]["type"] == "seven_day"
        assert row["credits"] == {"limit": 10.0, "usage": 2.5}
        assert row["raw"] == {"limit": 10.0}

    def test_windows_credits_raw_round_trip_through_shim(self, mock_db):
        windows = [
            {"type": "five_hour", "utilization": 12.5, "resets_at": "2026-07-09T05:00:00+00:00"},
            {"type": "seven_day", "utilization": 17.0, "resets_at": None},
        ]
        credits = {"has_credits": True, "balance": 3.14, "unlimited": False}
        raw = {"x-codex-plan-type": "pro", "x-codex-credits-balance": "3.14"}
        db.upsert_provider_usage_snapshot(
            provider_id="prov-B",
            provider_type="chatgpt",
            plan_type="pro",
            windows=windows,
            credits=credits,
            raw=raw,
            fetched_at=1,
        )
        row = next(
            r for r in db.get_all_provider_usage_snapshots() if r["provider_id"] == "prov-B"
        )
        # Parsed back to real list/dicts, not JSON strings.
        assert isinstance(row["windows"], list)
        assert row["windows"] == windows
        assert isinstance(row["credits"], dict)
        assert row["credits"] == credits
        assert isinstance(row["raw"], dict)
        assert row["raw"] == raw

    def test_null_credits_on_conflict_preserves_existing(self, mock_db):
        """OpenAI header capture writes credits=None; that must not wipe a
        credit-grants balance the poller already stored."""
        db.upsert_provider_usage_snapshot(
            provider_id="prov-openai",
            provider_type="openai",
            plan_type=None,
            windows=[],
            credits={"balance_remaining": 20.75},
            raw={"total_available": 20.75},
            fetched_at=1,
        )
        db.upsert_provider_usage_snapshot(
            provider_id="prov-openai",
            provider_type="openai",
            plan_type=None,
            windows=[{"type": "requests", "utilization": 1.0, "resets_at": None}],
            credits=None,
            raw={"x-ratelimit-limit-requests": "60"},
            fetched_at=2,
        )
        row = next(
            r for r in db.get_all_provider_usage_snapshots()
            if r["provider_id"] == "prov-openai"
        )
        assert row["credits"] == {"balance_remaining": 20.75}
        assert row["windows"][0]["type"] == "requests"
        assert row["fetched_at"] == 2
        assert row["raw"]["billing_fetched_at"] == 1

    def test_billing_failure_and_capture_have_independent_freshness(self, mock_db):
        from app.db.provider_usage_snapshots import record_provider_usage_failure

        db.upsert_provider_usage_snapshot("api", "openai", None, [], {"balance_remaining": 5}, {}, 1)
        record_provider_usage_failure("api", "openai", "billing_access_denied")
        db.upsert_provider_usage_snapshot("api", "openai", None, [], None, {}, 2)
        row = next(r for r in db.get_all_provider_usage_snapshots() if r["provider_id"] == "api")
        assert row["fetched_at"] == 2
        assert row["raw"]["billing_fetched_at"] == 1
        assert row["raw"]["usage_error"] == "billing_access_denied"
        db.upsert_provider_usage_snapshot("api", "openai", None, [], {"balance_remaining": 4}, {}, 3)
        row = next(r for r in db.get_all_provider_usage_snapshots() if r["provider_id"] == "api")
        assert row["raw"]["billing_fetched_at"] == 3
        assert "usage_error" not in row["raw"]

    def test_capture_then_billing_failure_has_no_successful_billing_timestamp(self, mock_db):
        from app.db.provider_usage_snapshots import record_provider_usage_failure

        db.upsert_provider_usage_snapshot("api", "openai", None, [], None, {}, 2)
        record_provider_usage_failure("api", "openai", "private upstream text")
        row = next(r for r in db.get_all_provider_usage_snapshots() if r["provider_id"] == "api")
        assert row["fetched_at"] == 2
        assert row["raw"]["billing_fetched_at"] is None
        assert row["raw"]["usage_error"] == "unavailable"

    def test_none_credits_and_raw_round_trip_as_none(self, mock_db):
        db.upsert_provider_usage_snapshot(
            provider_id="prov-C",
            provider_type="chatgpt",
            plan_type=None,
            windows=[],
            credits=None,
            raw=None,
            fetched_at=None,
        )
        row = next(
            r for r in db.get_all_provider_usage_snapshots() if r["provider_id"] == "prov-C"
        )
        assert row["windows"] == []
        assert row["credits"] is None
        assert row["raw"] is None
        assert row["plan_type"] is None
        assert row["fetched_at"] is None

    def test_multiple_providers_listed(self, mock_db):
        db.upsert_provider_usage_snapshot(
            provider_id="prov-1", provider_type="chatgpt", plan_type=None,
            windows=[], credits=None, raw=None, fetched_at=1,
        )
        db.upsert_provider_usage_snapshot(
            provider_id="prov-2", provider_type="openrouter", plan_type=None,
            windows=[], credits=None, raw=None, fetched_at=1,
        )
        ids = {r["provider_id"] for r in db.get_all_provider_usage_snapshots()}
        assert ids == {"prov-1", "prov-2"}


class TestDeleteProviderUsageSnapshot:
    def test_delete_removes_row(self, mock_db):
        db.upsert_provider_usage_snapshot(
            provider_id="prov-X", provider_type="chatgpt", plan_type=None,
            windows=[], credits=None, raw=None, fetched_at=1,
        )
        assert db.delete_provider_usage_snapshot("prov-X") is True
        assert db.get_all_provider_usage_snapshots() == []

    def test_delete_missing_returns_false(self, mock_db):
        assert db.delete_provider_usage_snapshot("nope") is False


class TestProviderUsageSnapshotsMigration:
    def test_migration_block_recreates_table(self, mock_db, test_db):
        """Dropping the table and running _run_column_migrations restores it."""
        from app.db.base import table_exists
        from tests.pg_shim import ShimCursor

        cursor = ShimCursor(test_db.cursor())
        cursor.execute("DROP TABLE IF EXISTS provider_usage_snapshots")
        assert not table_exists(cursor, "provider_usage_snapshots")

        db._run_column_migrations(cursor)

        assert table_exists(cursor, "provider_usage_snapshots")
        # Table is usable after the idempotent migration re-creates it.
        db.upsert_provider_usage_snapshot(
            provider_id="prov-mig", provider_type="chatgpt", plan_type=None,
            windows=[], credits=None, raw=None, fetched_at=1,
        )
        ids = {r["provider_id"] for r in db.get_all_provider_usage_snapshots()}
        assert "prov-mig" in ids
