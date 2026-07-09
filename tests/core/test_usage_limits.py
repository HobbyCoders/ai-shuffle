"""
Tests for the Claude usage-limits monitor (app/core/usage_limits.py).

Covers:
  * parse_usage_response — the real endpoint shape, the scoped-Fable window that
    only appears in the ``limits`` array, overage from ``extra_usage``, and
    malformed/missing fields.
  * UsageLimitsMonitor.fetch — happy path, missing credential (graceful empty),
    HTTP error and network error (keep last snapshot, warn once).
  * apply_live_event — update an existing window vs append a new one.
  * get_snapshot — on-demand fetch when stale.
"""

import logging
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

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

        assert by_type["five_hour"]["utilization"] == 12.0
        assert by_type["five_hour"]["resets_at"] == "2026-07-09T05:00:00+00:00"
        assert by_type["seven_day"]["utilization"] == 17.0
        # Fable weekly limit only exists in the limits[] array (weekly_scoped)
        # and must be surfaced as seven_day_overage_included.
        assert by_type["seven_day_overage_included"]["utilization"] == 24.0

    def test_top_level_wins_over_limits_array(self):
        data = {
            "five_hour": {"utilization": 40.0, "resets_at": "x"},
            "limits": [{"kind": "session", "percent": 5, "resets_at": "y", "scope": None}],
        }
        windows = parse_usage_response(data)
        by_type = {w["type"]: w for w in windows}
        assert by_type["five_hour"]["utilization"] == 40.0  # not 5

    def test_overage_from_extra_usage_when_enabled(self):
        data = {"extra_usage": {"is_enabled": True, "utilization": 8.0}}
        windows = parse_usage_response(data)
        by_type = {w["type"]: w for w in windows}
        assert by_type["overage"]["utilization"] == 8.0

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


class TestFetch:
    @pytest.mark.asyncio
    async def test_happy_path_parses_and_caches(self, monitor):
        with patch("app.core.usage_limits.direct_oauth") as oauth, \
             patch("app.core.usage_limits.httpx.AsyncClient", _fake_httpx(200, REAL_RESPONSE)):
            oauth.ensure_fresh_credentials = AsyncMock(return_value=False)
            oauth.get_access_token.return_value = "secret-token"

            snap = await monitor.fetch()

        assert snap["available"] is True
        assert snap["fetched_at"] is not None
        types = {w["type"] for w in snap["windows"]}
        assert "five_hour" in types and "seven_day_overage_included" in types

    @pytest.mark.asyncio
    async def test_sends_bearer_and_beta_header_but_never_logs_token(self, monitor, caplog):
        captured = {}

        def _client_factory(*args, **kwargs):
            captured["headers"] = kwargs.get("headers", {})
            return _fake_httpx(200, REAL_RESPONSE)()

        with patch("app.core.usage_limits.direct_oauth") as oauth, \
             patch("app.core.usage_limits.httpx.AsyncClient", side_effect=None) as ac:
            oauth.ensure_fresh_credentials = AsyncMock(return_value=False)
            oauth.get_access_token.return_value = "secret-token"

            # The get() call carries the headers, not the constructor — inspect it.
            resp = MagicMock(); resp.status_code = 200; resp.json.return_value = REAL_RESPONSE
            client = MagicMock()
            client.get = AsyncMock(return_value=resp)
            ctx = MagicMock()
            ctx.__aenter__ = AsyncMock(return_value=client)
            ctx.__aexit__ = AsyncMock(return_value=False)
            ac.return_value = ctx

            with caplog.at_level(logging.DEBUG):
                await monitor.fetch()

            _, kwargs = client.get.call_args
            headers = kwargs["headers"]

        assert headers["Authorization"] == "Bearer secret-token"
        assert headers["anthropic-beta"] == "oauth-2025-04-20"
        # The token must never appear in any log record.
        assert "secret-token" not in caplog.text

    @pytest.mark.asyncio
    async def test_no_credential_returns_empty_gracefully(self, monitor, caplog):
        with patch("app.core.usage_limits.direct_oauth") as oauth:
            oauth.ensure_fresh_credentials = AsyncMock(return_value=False)
            oauth.get_access_token.return_value = None

            with caplog.at_level(logging.WARNING):
                snap = await monitor.fetch()

        assert snap == {
            "windows": [], "fetched_at": None, "available": False,
            "generation": monitor.current_generation(),
        }
        # Missing credential is an expected steady state — no warning spam.
        assert caplog.text == ""

    @pytest.mark.asyncio
    async def test_http_error_keeps_last_snapshot_and_warns_once(self, monitor, caplog):
        with patch("app.core.usage_limits.direct_oauth") as oauth:
            oauth.ensure_fresh_credentials = AsyncMock(return_value=False)
            oauth.get_access_token.return_value = "tok"

            # First: a good fetch to populate a last-known snapshot.
            with patch("app.core.usage_limits.httpx.AsyncClient", _fake_httpx(200, REAL_RESPONSE)):
                await monitor.fetch()

            # Then: two failing fetches — snapshot preserved, only one WARNING.
            with patch("app.core.usage_limits.httpx.AsyncClient", _fake_httpx(500, {})), \
                 caplog.at_level(logging.WARNING):
                snap1 = await monitor.fetch()
                snap2 = await monitor.fetch()

        assert snap1["available"] is True  # kept last good snapshot
        assert snap1["windows"] == snap2["windows"]
        assert caplog.text.count("Usage-limits fetch failed") == 1

    @pytest.mark.asyncio
    async def test_network_error_keeps_last_snapshot(self, monitor):
        with patch("app.core.usage_limits.direct_oauth") as oauth:
            oauth.ensure_fresh_credentials = AsyncMock(return_value=False)
            oauth.get_access_token.return_value = "tok"

            with patch("app.core.usage_limits.httpx.AsyncClient", _fake_httpx(200, REAL_RESPONSE)):
                await monitor.fetch()

            with patch(
                "app.core.usage_limits.httpx.AsyncClient",
                _fake_httpx(raise_exc=RuntimeError("boom")),
            ):
                snap = await monitor.fetch()

        assert snap["available"] is True
        assert snap["windows"]  # unchanged


# ---------------------------------------------------------------------------
# generation guard (stale in-flight poll ordering)
# ---------------------------------------------------------------------------

class TestGenerationGuard:
    def test_snapshot_includes_generation(self, monitor):
        assert monitor._snapshot()["generation"] == monitor.current_generation()

    def test_bump_generation_increments(self, monitor):
        g0 = monitor.current_generation()
        assert monitor.bump_generation() == g0 + 1
        assert monitor.current_generation() == g0 + 1
        assert monitor._snapshot()["generation"] == g0 + 1

    @pytest.mark.asyncio
    async def test_stale_fetch_discarded_when_generation_bumped_midflight(self, monitor):
        """A poll that STARTED before an account switch must not overwrite the
        new account's data if it completes afterwards.

        Simulated by bumping the generation from inside the network read (as a
        concurrent ``switch_account`` would) and returning DIFFERENT data. The
        stale result is discarded and the primed windows survive.
        """
        # Prime the monitor with account A's windows.
        with patch("app.core.usage_limits.direct_oauth") as oauth, \
             patch("app.core.usage_limits.httpx.AsyncClient", _fake_httpx(200, REAL_RESPONSE)):
            oauth.ensure_fresh_credentials = AsyncMock(return_value=False)
            oauth.get_access_token.return_value = "tokA"
            await monitor.fetch()
        primed = monitor._snapshot()["windows"]
        assert primed  # sanity: account A produced windows

        async def _get_then_switch(*args, **kwargs):
            # A switch lands while this fetch is awaiting the network.
            monitor.bump_generation()
            resp = MagicMock()
            resp.status_code = 200
            resp.json.return_value = {"five_hour": {"utilization": 99.0, "resets_at": None}}
            return resp

        client = MagicMock()
        client.get = AsyncMock(side_effect=_get_then_switch)
        ctx = MagicMock()
        ctx.__aenter__ = AsyncMock(return_value=client)
        ctx.__aexit__ = AsyncMock(return_value=False)
        factory = MagicMock(return_value=ctx)

        with patch("app.core.usage_limits.direct_oauth") as oauth, \
             patch("app.core.usage_limits.httpx.AsyncClient", factory):
            oauth.ensure_fresh_credentials = AsyncMock(return_value=False)
            oauth.get_access_token.return_value = "tokA"
            snap = await monitor.fetch()

        # Discarded: primed windows unchanged, the 99% straggler never applied.
        assert snap["windows"] == primed
        assert all(w["utilization"] != 99.0 for w in snap["windows"])

    @pytest.mark.asyncio
    async def test_fresh_fetch_applies_when_generation_stable(self, monitor):
        with patch("app.core.usage_limits.direct_oauth") as oauth, \
             patch(
                 "app.core.usage_limits.httpx.AsyncClient",
                 _fake_httpx(200, {"five_hour": {"utilization": 55.0, "resets_at": None}}),
             ):
            oauth.ensure_fresh_credentials = AsyncMock(return_value=False)
            oauth.get_access_token.return_value = "tok"
            snap = await monitor.fetch()
        assert any(w["utilization"] == 55.0 for w in snap["windows"])


# ---------------------------------------------------------------------------
# apply_live_event / get_snapshot
# ---------------------------------------------------------------------------

class TestLiveEvents:
    def test_updates_existing_window(self, monitor):
        monitor._windows = [{"type": "five_hour", "utilization": 10.0, "resets_at": "a"}]
        snap = monitor.apply_live_event("five_hour", 55.0, "b")
        assert snap is not None
        w = next(w for w in snap["windows"] if w["type"] == "five_hour")
        assert w["utilization"] == 55.0 and w["resets_at"] == "b"
        assert len(snap["windows"]) == 1  # replaced, not appended

    def test_appends_new_window(self, monitor):
        snap = monitor.apply_live_event("seven_day_opus", 30.0, "c")
        assert [w["type"] for w in snap["windows"]] == ["seven_day_opus"]
        assert snap["available"] is True

    def test_normalizes_fraction_utilization_to_percent(self, monitor):
        # SDK sends 0.0-1.0; snapshot must be 0-100.
        snap = monitor.apply_live_event("five_hour", 0.42, "a")
        assert snap["windows"][0]["utilization"] == 42.0
        # A value already in percent (>1) is left alone.
        snap2 = monitor.apply_live_event("seven_day", 17.0, "b")
        w = next(w for w in snap2["windows"] if w["type"] == "seven_day")
        assert w["utilization"] == 17.0

    def test_ignores_incomplete_event(self, monitor):
        assert monitor.apply_live_event(None, 5.0, "x") is None
        assert monitor.apply_live_event("five_hour", None, "x") is None
        assert monitor.apply_live_event("five_hour", "nan", "x") is None

    @pytest.mark.asyncio
    async def test_get_snapshot_fetches_when_never_fetched(self, monitor):
        with patch("app.core.usage_limits.direct_oauth") as oauth, \
             patch("app.core.usage_limits.httpx.AsyncClient", _fake_httpx(200, REAL_RESPONSE)):
            oauth.ensure_fresh_credentials = AsyncMock(return_value=False)
            oauth.get_access_token.return_value = "tok"
            snap = await monitor.get_snapshot()
        assert snap["available"] is True and snap["windows"]

    @pytest.mark.asyncio
    async def test_get_snapshot_uses_cache_when_fresh(self, monitor):
        import time as _t
        monitor._windows = [{"type": "five_hour", "utilization": 1.0, "resets_at": None}]
        monitor._fetched_at = _t.time()
        monitor._available = True
        # If it tried to fetch, the missing httpx patch would still be fine, but
        # patch direct_oauth to assert get_access_token is NOT called.
        with patch("app.core.usage_limits.direct_oauth") as oauth:
            snap = await monitor.get_snapshot(max_age=300)
            oauth.get_access_token.assert_not_called()
        assert snap["windows"][0]["utilization"] == 1.0
