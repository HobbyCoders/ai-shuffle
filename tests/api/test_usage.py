"""
Tests for GET /api/v1/usage/limits.

The endpoint serves the enriched CLUSTER usage snapshot (the local install's
legacy top-level fields plus every install's per-account snapshot, joined to the
installs registry), fetching on demand when stale, and must require auth. Uses a
bare app mounting only the usage router with the require_auth dependency
overridden, matching tests/api/test_health_ready.py.
"""

import time
from unittest.mock import AsyncMock, patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.usage import router
from app.api.auth import require_auth
from app.core.config import settings


def _client(authed=True):
    app = FastAPI()
    app.include_router(router)
    if authed:
        app.dependency_overrides[require_auth] = lambda: "admin"
    return TestClient(app)


class TestUsageLimitsEndpoint:
    def test_returns_snapshot_with_legacy_fields(self):
        snapshot = {
            "windows": [
                {"type": "five_hour", "utilization": 12.0, "resets_at": "2026-07-09T05:00:00+00:00"},
                {"type": "seven_day", "utilization": 17.0, "resets_at": None},
            ],
            "fetched_at": 1234567890.0,
            "available": True,
            "generation": 42,
            "snapshots": [],
            "self_instance_id": "local-1",
        }
        with patch("app.api.usage.usage_limits_monitor") as mon:
            mon.get_cluster_snapshot = AsyncMock(return_value=snapshot)
            resp = _client().get("/api/v1/usage/limits")

        assert resp.status_code == 200
        body = resp.json()
        # Legacy top-level fields intact for old clients.
        assert body["available"] is True
        assert body["fetched_at"] == 1234567890.0
        assert body["windows"][0]["type"] == "five_hour"
        assert body["windows"][0]["utilization"] == 12.0
        # New cluster fields present.
        assert body["snapshots"] == []
        assert body["self_instance_id"] == "local-1"

    def test_returns_empty_when_unavailable(self):
        with patch("app.api.usage.usage_limits_monitor") as mon:
            mon.get_cluster_snapshot = AsyncMock(
                return_value={"windows": [], "fetched_at": None, "available": False}
            )
            resp = _client().get("/api/v1/usage/limits")

        assert resp.status_code == 200
        body = resp.json()
        assert body == {
            "windows": [], "fetched_at": None, "available": False, "generation": None,
            "snapshots": [], "self_instance_id": None,
        }

    def test_cluster_join_and_unknown_install_excluded(self):
        """The real get_cluster_snapshot joins the installs registry, includes
        self even without a registry row, and drops rows for installs that have
        left the registry.
        """
        from app.db import database as db
        from app.core.usage_limits import usage_limits_monitor

        iid = settings.effective_instance_id

        # Prime the singleton so get_cluster_snapshot skips the network fetch.
        prev_fetched = usage_limits_monitor._fetched_at
        prev_windows = usage_limits_monitor._windows
        prev_avail = usage_limits_monitor._available
        prev_gen = usage_limits_monitor._generation
        try:
            usage_limits_monitor._fetched_at = time.time()
            usage_limits_monitor._windows = [
                {"type": "five_hour", "utilization": 10.0, "resets_at": None}
            ]
            usage_limits_monitor._available = True
            usage_limits_monitor._generation = 12345
            # Drop any short-TTL cluster cache carried over from a prior test so
            # the rows seeded below are read fresh.
            usage_limits_monitor._cluster_cache = None

            # This install's own persisted snapshot (self — always included).
            db.upsert_usage_snapshot(
                iid,
                windows=[{"type": "five_hour", "utilization": 10.0, "resets_at": None}],
                available=True, generation=12345, fetched_at=111,
            )
            # A second install: registry entry + snapshot → joins device_name/online.
            db.register_install("worker-1", "Worker", "http://w:1455", "docker", "1.0", {})
            db.upsert_usage_snapshot(
                "worker-1",
                windows=[{"type": "five_hour", "utilization": 88.0, "resets_at": "r"}],
                available=True, generation=2, fetched_at=222,
                account_id="acct-w", account_email="w@example.com", account_name="Worker Acct",
            )
            # An install with NO registry row → must be excluded.
            db.upsert_usage_snapshot(
                "ghost-9", windows=[], available=False, generation=3, fetched_at=333,
            )

            resp = _client().get("/api/v1/usage/limits")
        finally:
            usage_limits_monitor._fetched_at = prev_fetched
            usage_limits_monitor._windows = prev_windows
            usage_limits_monitor._available = prev_avail
            usage_limits_monitor._generation = prev_gen
            usage_limits_monitor._cluster_cache = None

        assert resp.status_code == 200
        body = resp.json()

        # Legacy top-level fields describe the local install.
        assert body["available"] is True
        assert body["windows"][0]["type"] == "five_hour"
        assert body["self_instance_id"] == iid

        by_id = {s["instance_id"]: s for s in body["snapshots"]}
        # Self is always present.
        assert iid in by_id
        # Second install joined its registry row.
        assert "worker-1" in by_id
        worker = by_id["worker-1"]
        assert worker["device_name"] == "Worker"
        assert worker["online"] is True
        assert worker["account_email"] == "w@example.com"
        assert worker["account_name"] == "Worker Acct"
        assert worker["windows"][0]["utilization"] == 88.0
        # Unknown install (no registry row) excluded.
        assert "ghost-9" not in by_id

    def test_requires_auth(self):
        # No dependency override → real require_auth runs and rejects.
        resp = _client(authed=False).get("/api/v1/usage/limits")
        assert resp.status_code == 401
