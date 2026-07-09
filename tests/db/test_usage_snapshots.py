"""Tests for the per-install usage-snapshot DB helpers (app/db/usage_snapshots.py).

Covers the upsert (insert then replace on conflict), the ``windows`` JSON
round-trip through the pg_shim (stored as a JSON blob, read back as a list), and
delete. Runs on the in-memory SQLite shim via the autouse ``_auto_db_shim``
fixture (documented here by depending on ``mock_db``).
"""

from app.db import database as db


class TestUpsertUsageSnapshot:
    def test_insert_then_replace_on_conflict(self, mock_db):
        db.upsert_usage_snapshot(
            "inst-A",
            windows=[{"type": "five_hour", "utilization": 12.0, "resets_at": "x"}],
            available=True,
            generation=1000,
            fetched_at=1234567890000,
            account_id="acct-1",
            account_email="a@example.com",
            account_name="Account One",
        )

        rows = db.get_all_usage_snapshots()
        assert len(rows) == 1
        row = rows[0]
        assert row["instance_id"] == "inst-A"
        assert row["available"] is True
        assert row["generation"] == 1000
        assert row["fetched_at"] == 1234567890000
        assert row["account_id"] == "acct-1"
        assert row["account_email"] == "a@example.com"
        assert row["account_name"] == "Account One"

        # A second upsert for the same instance_id REPLACES the row (no dup).
        db.upsert_usage_snapshot(
            "inst-A",
            windows=[{"type": "seven_day", "utilization": 55.0, "resets_at": "y"}],
            available=False,
            generation=2000,
            fetched_at=999,
            account_id="acct-2",
            account_email=None,
            account_name="Account Two",
        )
        rows = db.get_all_usage_snapshots()
        assert len(rows) == 1
        row = rows[0]
        assert row["generation"] == 2000
        assert row["available"] is False
        assert row["account_id"] == "acct-2"
        assert row["account_email"] is None
        assert row["windows"][0]["type"] == "seven_day"

    def test_windows_json_round_trips_through_shim(self, mock_db):
        windows = [
            {"type": "five_hour", "utilization": 12.5, "resets_at": "2026-07-09T05:00:00+00:00"},
            {"type": "seven_day", "utilization": 17.0, "resets_at": None},
        ]
        db.upsert_usage_snapshot(
            "inst-B", windows=windows, available=True, generation=1, fetched_at=1,
        )
        row = next(r for r in db.get_all_usage_snapshots() if r["instance_id"] == "inst-B")
        # Parsed back to a real list of dicts, not a JSON string.
        assert isinstance(row["windows"], list)
        assert row["windows"] == windows

    def test_empty_windows_and_optional_account_meta(self, mock_db):
        db.upsert_usage_snapshot(
            "inst-C", windows=[], available=False, generation=None, fetched_at=None,
        )
        row = next(r for r in db.get_all_usage_snapshots() if r["instance_id"] == "inst-C")
        assert row["windows"] == []
        assert row["available"] is False
        assert row["account_id"] is None
        assert row["account_email"] is None
        assert row["account_name"] is None

    def test_multiple_installs_listed(self, mock_db):
        db.upsert_usage_snapshot("inst-1", windows=[], available=True, generation=1, fetched_at=1)
        db.upsert_usage_snapshot("inst-2", windows=[], available=True, generation=1, fetched_at=1)
        ids = {r["instance_id"] for r in db.get_all_usage_snapshots()}
        assert ids == {"inst-1", "inst-2"}


class TestDeleteUsageSnapshot:
    def test_delete_removes_row(self, mock_db):
        db.upsert_usage_snapshot("inst-X", windows=[], available=True, generation=1, fetched_at=1)
        assert db.delete_usage_snapshot("inst-X") is True
        assert db.get_all_usage_snapshots() == []

    def test_delete_missing_returns_false(self, mock_db):
        assert db.delete_usage_snapshot("nope") is False
