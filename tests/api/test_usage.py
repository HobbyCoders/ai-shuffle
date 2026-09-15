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

import pytest

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.usage import router
from app.api.auth import require_auth
from app.core.config import settings


@pytest.fixture(autouse=True)
def cached_provider_poll():
    with patch("app.core.provider_usage.provider_usage_monitor") as monitor:
        monitor.refreshing = False
        yield monitor


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

    def test_accepts_int_epoch_resets_at(self):
        """Newer Claude CLI usage payloads carry ``resets_at`` as int epoch
        seconds; cross-install rows persisted that way can't be retro-fixed, so
        the response model must coerce them instead of 500-ing (pydantic
        string_type ValidationError). Coerced to str — the FE contract declares
        ``resets_at?: string | null`` and ``parseResetMs`` parses numeric
        strings.
        """
        epoch = 1783739400
        snapshot = {
            "windows": [
                {"type": "five_hour", "utilization": 12.0, "resets_at": epoch},
            ],
            "fetched_at": 1234567890.0,
            "available": True,
            "generation": 42,
            "snapshots": [
                {
                    "instance_id": "local-1",
                    "available": True,
                    "windows": [
                        {"type": "five_hour", "utilization": 12.0,
                         "resets_at": "2026-07-09T05:00:00+00:00"},
                    ],
                },
                {
                    "instance_id": "worker-1",
                    "available": True,
                    "windows": [
                        {"type": "five_hour", "utilization": 55.0, "resets_at": epoch},
                    ],
                },
                {
                    "instance_id": "worker-2",
                    "available": True,
                    "windows": [
                        {"type": "seven_day", "utilization": 3.0, "resets_at": epoch},
                    ],
                },
            ],
            "self_instance_id": "local-1",
        }
        with patch("app.api.usage.usage_limits_monitor") as mon:
            mon.get_cluster_snapshot = AsyncMock(return_value=snapshot)
            resp = _client().get("/api/v1/usage/limits")

        assert resp.status_code == 200
        body = resp.json()
        # Int epoch coerced to its decimal-string form at every position.
        assert body["windows"][0]["resets_at"] == str(epoch)
        assert body["snapshots"][0]["windows"][0]["resets_at"] == "2026-07-09T05:00:00+00:00"
        assert body["snapshots"][1]["windows"][0]["resets_at"] == str(epoch)
        assert body["snapshots"][2]["windows"][0]["resets_at"] == str(epoch)

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
            "snapshots": [], "self_instance_id": None, "providers": [],
            "provider_refreshing": False,
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
            db.create_claude_account(
                account_id="acct-w", name="Worker Acct", credentials="enc:x",
                credential_fingerprint=None, expires_at=None, has_refresh_token=True,
                subscription_type="max",
            )
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
        # The account's subscription tier survives the response model (the
        # popover's Claude header reads it per install).
        assert worker["plan_type"] == "max"
        # Self has no account stamped on its row → null, never missing.
        assert by_id[iid]["plan_type"] is None
        assert worker["windows"][0]["utilization"] == 88.0
        # Unknown install (no registry row) excluded.
        assert "ghost-9" not in by_id

    def test_requires_auth(self):
        # No dependency override → real require_auth runs and rejects.
        resp = _client(authed=False).get("/api/v1/usage/limits")
        assert resp.status_code == 401


# A minimal Claude cluster snapshot used to isolate the provider-array behaviour
# from the (patched-away) Claude monitor.
_CLAUDE_SNAPSHOT = {
    "windows": [
        {"type": "five_hour", "utilization": 12.0, "resets_at": "2026-07-09T05:00:00+00:00"},
    ],
    "fetched_at": 1234567890.0,
    "available": True,
    "generation": 42,
    "snapshots": [],
    "self_instance_id": "local-1",
}


def _assert_claude_fields_unchanged(body):
    """Every legacy Claude top-level field is byte-identical to the source."""
    assert body["available"] is True
    assert body["fetched_at"] == 1234567890.0
    assert body["generation"] == 42
    assert body["snapshots"] == []
    assert body["self_instance_id"] == "local-1"
    assert body["windows"][0]["type"] == "five_hour"
    assert body["windows"][0]["utilization"] == 12.0
    assert body["windows"][0]["resets_at"] == "2026-07-09T05:00:00+00:00"


class TestUsageLimitsProviders:
    @pytest.mark.asyncio
    async def test_real_monitor_returns_progressive_cached_results(self):
        import asyncio
        from app.api import usage
        from app.core import provider_usage
        from app.db import database as db
        from app.db.async_database import run_sync

        db.create_model_provider(
            provider_id="progressive-grok", name="grok", provider_type="grok",
            base_url="https://example.test",
        )

        monitor = provider_usage.ProviderUsageMonitor()
        saved, release = asyncio.Event(), asyncio.Event()

        async def upstream_work():
            await run_sync(
                db.upsert_provider_usage_snapshot, "progressive-grok", "grok", "SuperGrok",
                [{"type": "seven_day", "utilization": 34}], None, {}, int(time.time() * 1000)
            )
            saved.set()
            await release.wait()

        with patch.object(provider_usage, "provider_usage_monitor", monitor), patch.object(
            monitor, "_poll_providers", new=AsyncMock(side_effect=upstream_work)
        ) as work, patch.object(usage, "usage_limits_monitor") as claude:
            claude.get_cluster_snapshot = AsyncMock(return_value=dict(_CLAUDE_SNAPSHOT))
            first = await asyncio.wait_for(usage.get_usage_limits(False, "admin"), 1)
            assert first.provider_refreshing
            try:
                await asyncio.wait_for(saved.wait(), 1)
                progressive = await asyncio.wait_for(usage.get_usage_limits(False, "admin"), 1)
                assert progressive.provider_refreshing
                grok = next(p for p in progressive.providers if p.provider_id == "progressive-grok")
                assert grok.windows[0].utilization == 34
                assert grok.status == "fresh"
            finally:
                release.set()
                await monitor._poll_task
            final = await usage.get_usage_limits(False, "admin")
            assert not final.provider_refreshing
            work.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_poll_completion_during_snapshot_read_keeps_followup_enabled(self):
        from app.api import usage
        from app.core.provider_usage import provider_usage_monitor

        async def loaded():
            provider_usage_monitor.refreshing = False
            return []

        provider_usage_monitor.refreshing = True
        with patch.object(usage, "usage_limits_monitor") as mon, patch.object(
            usage, "_load_provider_usage", new=AsyncMock(side_effect=loaded)
        ):
            mon.get_cluster_snapshot = AsyncMock(return_value=dict(_CLAUDE_SNAPSHOT))
            response = await usage.get_usage_limits(False, "admin")
        assert response.provider_refreshing is True

    @pytest.mark.parametrize("balance", [None, 0.0, 12.5])
    def test_billing_failure_preserves_balance_and_reports_safe_status(self, balance):
        from app.db import database as db
        from app.db.provider_usage_snapshots import record_provider_usage_failure

        db.create_model_provider(
            provider_id="prov-openai", name="openai", provider_type="openai",
            base_url="https://example.test",
        )

        fetched = int(time.time() * 1000)
        if balance is not None:
            db.upsert_provider_usage_snapshot(
                "prov-openai", "openai", None, [], {"spend_month": balance}, {"source": "openai_admin"}, fetched
            )
        record_provider_usage_failure("prov-openai", "openai", "billing_access_denied")
        db.upsert_provider_usage_snapshot(
            "prov-openai", "openai", None, [], None, {"x-ratelimit-limit-requests": "500"}, fetched + 9999
        )
        with patch("app.api.usage.usage_limits_monitor") as mon:
            mon.get_cluster_snapshot = AsyncMock(return_value=dict(_CLAUDE_SNAPSHOT))
            body = _client().get("/api/v1/usage/limits").json()
        entry = body["providers"][0]
        assert entry["status"] == ("stale" if balance is not None else "unavailable")
        assert entry["last_error_kind"] == "billing_access_denied"
        assert entry["fetched_at"] == (fetched if balance is not None else None)
        assert entry["credits"] == ({"spend_month": balance} if balance is not None else None)
        assert "raw" not in entry

    def test_refresh_returns_cached_data_while_provider_work_is_pending(self, cached_provider_poll):
        cached_provider_poll.refreshing = True
        with patch("app.api.usage.usage_limits_monitor") as mon, patch("app.api.usage._request_claude_refresh"):
            mon.get_cluster_snapshot = AsyncMock(return_value=dict(_CLAUDE_SNAPSHOT))
            response = _client().get("/api/v1/usage/limits?refresh=true")
        assert response.status_code == 200
        assert response.json()["provider_refreshing"] is True
        cached_provider_poll.request_refresh.assert_called_once_with(force=True)
        cached_provider_poll.poll_once.assert_not_called()

    @pytest.mark.parametrize("balance", [None, 0.0, 12.5])
    @pytest.mark.parametrize("configured", [False, True])
    def test_retired_oauth_snapshot_hidden_without_merging_real_providers(self, balance, configured):
        from app.db import database as db
        from app.db.provider_usage_snapshots import record_provider_usage_failure

        db.create_model_provider(
            provider_id="prov-chatgpt", name="chatgpt", provider_type="chatgpt",
            base_url="https://example.test",
        )
        db.create_model_provider(
            provider_id="prov-openai", name="openai", provider_type="openai",
            base_url="https://example.test",
        )
        db.create_model_provider(
            provider_id="prov-openai-2", name="openai", provider_type="openai",
            base_url="https://example.test",
        )

        fetched = int(time.time() * 1000)
        db.upsert_provider_usage_snapshot(
            "openai-api", "openai", None, [],
            {"balance_remaining": balance} if balance is not None else None, {}, fetched,
        )
        record_provider_usage_failure("openai-api", "openai", "billing_access_denied")
        db.upsert_provider_usage_snapshot(
            "prov-chatgpt", "chatgpt", "pro", [{"type": "five_hour", "utilization": 12}],
            {"balance": 99}, {}, fetched,
        )
        if configured:
            record_provider_usage_failure("prov-openai", "openai", "billing_access_denied")
            db.upsert_provider_usage_snapshot(
                "prov-openai-2", "openai", None, [], {"spend_month": 0}, {"source": "openai_admin"}, fetched,
            )
        with patch("app.api.usage.usage_limits_monitor") as mon:
            mon.get_cluster_snapshot = AsyncMock(return_value=dict(_CLAUDE_SNAPSHOT))
            response = _client().get("/api/v1/usage/limits")
        assert response.status_code == 200
        body = response.json()
        _assert_claude_fields_unchanged(body)
        entries = {entry["provider_id"]: entry for entry in body["providers"]}
        expected = {"prov-chatgpt", "prov-openai", "prov-openai-2"} if configured else {"prov-chatgpt"}
        assert set(entries) == expected
        assert entries["prov-chatgpt"]["credits"] is None
        assert entries["prov-chatgpt"]["windows"][0]["utilization"] == 12
        if configured:
            assert entries["prov-openai"]["last_error_kind"] == "billing_access_denied"
            assert entries["prov-openai-2"]["credits"] == {"spend_month": 0}
        assert any(row["provider_id"] == "openai-api" for row in db.get_all_provider_usage_snapshots())

    def test_seeded_rows_returned_with_joined_name(self):
        from app.db import database as db

        db.create_model_provider(
            provider_id="prov-chatgpt", name="My ChatGPT", provider_type="chatgpt",
            base_url="https://chatgpt.com",
        )
        db.upsert_provider_usage_snapshot(
            provider_id="prov-chatgpt", provider_type="chatgpt", plan_type="plus",
            windows=[
                {"type": "five_hour", "utilization": 42.0, "resets_at": 1783739400},
                {"type": "seven_day", "utilization": 7.0, "resets_at": None},
            ],
            credits={"has_credits": True, "balance": 5.0},
            raw={"x-codex-plan-type": "plus"},
            fetched_at=1783739400000,
        )

        with patch("app.api.usage.usage_limits_monitor") as mon:
            mon.get_cluster_snapshot = AsyncMock(return_value=dict(_CLAUDE_SNAPSHOT))
            resp = _client().get("/api/v1/usage/limits")

        assert resp.status_code == 200
        body = resp.json()
        _assert_claude_fields_unchanged(body)

        providers = body["providers"]
        assert len(providers) == 1
        entry = providers[0]
        assert entry["provider_id"] == "prov-chatgpt"
        assert entry["provider_name"] == "My ChatGPT"
        assert entry["provider_type"] == "chatgpt"
        assert entry["plan_type"] == "plus"
        # Codex extra-credit headers stay off the ChatGPT quota row.
        assert entry["credits"] is None
        assert entry["fetched_at"] == 1783739400000
        # Window int-epoch resets_at coerced to decimal string; None preserved.
        assert entry["windows"][0]["type"] == "five_hour"
        assert entry["windows"][0]["utilization"] == 42.0
        assert entry["windows"][0]["resets_at"] == str(1783739400)
        assert entry["windows"][1]["resets_at"] is None

    def test_empty_table_yields_empty_providers(self):
        with patch("app.api.usage.usage_limits_monitor") as mon:
            mon.get_cluster_snapshot = AsyncMock(return_value=dict(_CLAUDE_SNAPSHOT))
            resp = _client().get("/api/v1/usage/limits")

        assert resp.status_code == 200
        body = resp.json()
        _assert_claude_fields_unchanged(body)
        assert body["providers"] == []

    @pytest.mark.parametrize("provider_type", ["grok", "chatgpt", "openrouter", "openai"])
    def test_account_deletion_removes_usage_and_hides_late_poll(self, provider_type):
        from app.api.model_providers import router as providers_router
        from app.api.auth import require_admin
        from app.db import database as db

        db.create_model_provider(
            provider_id="removed-account", name="Removed account",
            provider_type=provider_type, base_url="https://example.test",
        )

        def save_snapshot():
            db.upsert_provider_usage_snapshot(
                "removed-account", provider_type, "SuperGrok",
                [{"type": "seven_day", "utilization": 0}],
                {"balance_remaining": 20}, {}, int(time.time() * 1000),
            )

        save_snapshot()
        client = _client()
        client.app.include_router(providers_router)
        client.app.dependency_overrides[require_admin] = lambda: "admin"
        with patch("app.api.usage.usage_limits_monitor") as mon:
            mon.get_cluster_snapshot = AsyncMock(return_value=dict(_CLAUDE_SNAPSHOT))
            assert len(client.get("/api/v1/usage/limits").json()["providers"]) == 1
            assert client.delete("/api/v1/model-providers/removed-account").status_code == 200
            assert db.get_all_provider_usage_snapshots() == []
            assert client.get("/api/v1/usage/limits").json()["providers"] == []
            save_snapshot()  # An already-running upstream request completes late.
            assert client.get("/api/v1/usage/limits").json()["providers"] == []

    def test_deleted_provider_is_not_returned(self):
        """Old snapshots and late poll results must not resurrect an account."""
        from app.db import database as db

        db.upsert_provider_usage_snapshot(
            provider_id="prov-orphan", provider_type="openrouter", plan_type=None,
            windows=[], credits=None, raw=None, fetched_at=1,
        )
        # No matching model_providers row exists for prov-orphan.

        with patch("app.api.usage.usage_limits_monitor") as mon:
            mon.get_cluster_snapshot = AsyncMock(return_value=dict(_CLAUDE_SNAPSHOT))
            resp = _client().get("/api/v1/usage/limits")

        assert resp.status_code == 200
        providers = resp.json()["providers"]
        assert providers == []

    def test_raw_never_in_response(self):
        """The debug ``raw`` blob must never appear in any providers[] entry."""
        from app.db import database as db

        db.create_model_provider(
            provider_id="prov-raw", name="chatgpt", provider_type="chatgpt",
            base_url="https://example.test",
        )

        db.upsert_provider_usage_snapshot(
            provider_id="prov-raw", provider_type="chatgpt", plan_type="pro",
            windows=[{"type": "five_hour", "utilization": 1.0, "resets_at": None}],
            credits={"balance": 1.0},
            raw={"x-codex-plan-type": "pro", "x-codex-secret-lookalike": "should-not-leak"},
            fetched_at=9,
        )

        with patch("app.api.usage.usage_limits_monitor") as mon:
            mon.get_cluster_snapshot = AsyncMock(return_value=dict(_CLAUDE_SNAPSHOT))
            resp = _client().get("/api/v1/usage/limits")

        assert resp.status_code == 200
        body = resp.json()
        for entry in body["providers"]:
            assert "raw" not in entry
        # The raw blob's contents must not appear anywhere in the response body.
        assert "should-not-leak" not in resp.text

    def test_openai_and_xai_snapshots_excluded_from_response(self):
        """xAI API-key snapshots never surface (per-minute headroom only).
        OpenAI snapshots without a displayable credits blob stay hidden too;
        SuperGrok subscription snapshots (provider_type='grok') are included."""
        from app.db import database as db

        db.create_model_provider(
            provider_id="prov-openai", name="openai", provider_type="openai",
            base_url="https://example.test",
        )
        db.create_model_provider(
            provider_id="prov-xai", name="xai", provider_type="xai",
            base_url="https://example.test",
        )
        db.create_model_provider(
            provider_id="prov-or", name="openrouter", provider_type="openrouter",
            base_url="https://example.test",
        )
        db.create_model_provider(
            provider_id="prov-grok", name="grok", provider_type="grok",
            base_url="https://example.test",
        )

        db.upsert_provider_usage_snapshot(
            provider_id="prov-openai", provider_type="openai", plan_type=None,
            windows=[{"type": "requests", "utilization": 0.2, "resets_at": None}],
            credits=None, raw=None, fetched_at=5,
        )
        db.upsert_provider_usage_snapshot(
            provider_id="prov-xai", provider_type="xai", plan_type=None,
            windows=[{"type": "requests", "utilization": 1.0, "resets_at": None}],
            credits=None, raw=None, fetched_at=5,
        )
        db.upsert_provider_usage_snapshot(
            provider_id="prov-or", provider_type="openrouter", plan_type=None,
            windows=[], credits={"balance_remaining": 18.11}, raw=None, fetched_at=6,
        )
        db.upsert_provider_usage_snapshot(
            provider_id="prov-grok", provider_type="grok", plan_type="SuperGrok",
            windows=[{"type": "seven_day", "utilization": 34.0, "resets_at": None}],
            credits={"credit_usage_percent": 34.0}, raw=None, fetched_at=7,
        )

        with patch("app.api.usage.usage_limits_monitor") as mon:
            mon.get_cluster_snapshot = AsyncMock(return_value=dict(_CLAUDE_SNAPSHOT))
            resp = _client().get("/api/v1/usage/limits")

        assert resp.status_code == 200
        providers = resp.json()["providers"]
        assert [p["provider_id"] for p in providers] == ["prov-openai", "prov-or", "prov-grok"]
        assert providers[0]["credits"] is None
        assert providers[0]["last_error_kind"] == "missing_credentials"
        grok = next(p for p in providers if p["provider_id"] == "prov-grok")
        assert grok["provider_type"] == "grok"
        assert grok["plan_type"] == "SuperGrok"

    def test_openai_credits_surface_without_rate_limit_windows(self):
        """Official organization spend is not a prepaid balance or RPM window."""
        from app.db import database as db

        db.create_model_provider(
            provider_id="prov-openai", name="openai", provider_type="openai",
            base_url="https://example.test",
        )

        db.upsert_provider_usage_snapshot(
            provider_id="prov-openai", provider_type="openai", plan_type=None,
            windows=[{"type": "requests", "utilization": 0.2, "resets_at": None}],
            credits={"spend_month": 4.25},
            raw={"source": "openai_admin"}, fetched_at=8,
        )

        with patch("app.api.usage.usage_limits_monitor") as mon:
            mon.get_cluster_snapshot = AsyncMock(return_value=dict(_CLAUDE_SNAPSHOT))
            resp = _client().get("/api/v1/usage/limits")

        assert resp.status_code == 200
        providers = resp.json()["providers"]
        assert len(providers) == 1
        entry = providers[0]
        assert entry["provider_id"] == "prov-openai"
        assert entry["provider_type"] == "openai"
        assert entry["windows"] == []
        assert entry["credits"]["spend_month"] == 4.25
        assert "balance_remaining" not in entry["credits"]

    def test_fal_credits_surface_with_brand_name(self):
        """fal.ai is an integration key, not a model-provider row; the API
        still names it so the popover can render without a registry join."""
        from app.db import database as db

        db.set_system_setting("fal_billing_api_key", "dummy-fal-key")
        db.upsert_provider_usage_snapshot(
            provider_id="fal-ai", provider_type="fal", plan_type=None,
            windows=[], credits={"balance_remaining": 24.5, "currency": "USD"},
            raw=None, fetched_at=9,
            expected_credential_generation=db.get_system_setting_revision("fal_billing_api_key"),
        )

        with patch("app.api.usage.usage_limits_monitor") as mon:
            mon.get_cluster_snapshot = AsyncMock(return_value=dict(_CLAUDE_SNAPSHOT))
            resp = _client().get("/api/v1/usage/limits")

        assert resp.status_code == 200
        providers = resp.json()["providers"]
        assert len(providers) == 1
        entry = providers[0]
        assert entry["provider_id"] == "fal-ai"
        assert entry["provider_name"] == "fal.ai"
        assert entry["provider_type"] == "fal"
        assert entry["credits"]["balance_remaining"] == 24.5

    def test_retired_oauth_balance_is_not_a_platform_account(self):
        """Old successful OAuth snapshots must not outlive the retired fallback."""
        from app.db import database as db

        db.upsert_provider_usage_snapshot(
            provider_id="openai-api", provider_type="openai", plan_type=None,
            windows=[], credits={"balance_remaining": 12.5, "total_usage": 2.0},
            raw=None, fetched_at=10,
        )

        with patch("app.api.usage.usage_limits_monitor") as mon:
            mon.get_cluster_snapshot = AsyncMock(return_value=dict(_CLAUDE_SNAPSHOT))
            resp = _client().get("/api/v1/usage/limits")

        assert resp.status_code == 200
        providers = resp.json()["providers"]
        assert providers == []

    def test_provider_load_exception_yields_empty_providers_with_200(self):
        """A DB hiccup inside _load_provider_usage degrades to providers: [] and
        must NOT turn the endpoint into an error or drop Claude fields."""
        with patch("app.api.usage.usage_limits_monitor") as mon, patch(
            "app.db.database.get_all_provider_usage_snapshots",
            side_effect=RuntimeError("db down"),
        ):
            mon.get_cluster_snapshot = AsyncMock(return_value=dict(_CLAUDE_SNAPSHOT))
            resp = _client().get("/api/v1/usage/limits")

        assert resp.status_code == 200
        body = resp.json()
        _assert_claude_fields_unchanged(body)
        assert body["providers"] == []
