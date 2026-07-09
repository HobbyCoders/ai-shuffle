"""
Tests for GET /api/v1/usage/limits.

The endpoint serves the cached usage snapshot (fetching on demand when stale)
and must require auth. Uses a bare app mounting only the usage router with the
require_auth dependency overridden, matching tests/api/test_health_ready.py.
"""

from unittest.mock import AsyncMock, patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.usage import router
from app.api.auth import require_auth


def _client(authed=True):
    app = FastAPI()
    app.include_router(router)
    if authed:
        app.dependency_overrides[require_auth] = lambda: "admin"
    return TestClient(app)


class TestUsageLimitsEndpoint:
    def test_returns_snapshot(self):
        snapshot = {
            "windows": [
                {"type": "five_hour", "utilization": 12.0, "resets_at": "2026-07-09T05:00:00+00:00"},
                {"type": "seven_day", "utilization": 17.0, "resets_at": None},
            ],
            "fetched_at": 1234567890.0,
            "available": True,
        }
        with patch("app.api.usage.usage_limits_monitor") as mon:
            mon.get_snapshot = AsyncMock(return_value=snapshot)
            resp = _client().get("/api/v1/usage/limits")

        assert resp.status_code == 200
        body = resp.json()
        assert body["available"] is True
        assert body["fetched_at"] == 1234567890.0
        assert body["windows"][0]["type"] == "five_hour"
        assert body["windows"][0]["utilization"] == 12.0

    def test_returns_empty_when_unavailable(self):
        with patch("app.api.usage.usage_limits_monitor") as mon:
            mon.get_snapshot = AsyncMock(
                return_value={"windows": [], "fetched_at": None, "available": False}
            )
            resp = _client().get("/api/v1/usage/limits")

        assert resp.status_code == 200
        body = resp.json()
        assert body == {"windows": [], "fetched_at": None, "available": False}

    def test_requires_auth(self):
        # No dependency override → real require_auth runs and rejects.
        resp = _client(authed=False).get("/api/v1/usage/limits")
        assert resp.status_code == 401
