"""Kimi balance and MiniMax M Plan meters: real persistence, fake upstream.

Responses copy the documented shapes (platform.kimi.ai/docs/api/balance;
the MiniMax M Plan FAQ's remaining-quota endpoint, fields as MiniMax's own CLI
reads them). Every key and figure is made up. Requests are counted at the
httpx transport, so a skipped probe is proven to send nothing.
"""
import asyncio
import logging
from unittest.mock import patch

import httpx
import pytest

from app.core import native_providers, usage_sources
from app.core.native_usage import (
    normalize_kimi_balance,
    normalize_minimax_plan,
    poll_kimi_balance,
    poll_minimax_plan,
)
from app.db import database as db

KIMI_KEY = "sk-test-kimi-not-a-real-key"
MINIMAX_KEY = "sk-cp-test-minimax-not-a-real-key"
KIMI_BODY = {"code": 0, "data": {"available_balance": 49.58894, "voucher_balance": 46.58893,
                                 "cash_balance": 3.00001}, "scode": "0x0", "status": True}
FIVE_H_START, FIVE_H_END = 1776355200000, 1776373200000
WEEK_START, WEEK_END = 1776009600000, 1776614400000


def minimax_body(**text):
    entry = {"model_name": "MiniMax-M*", "start_time": FIVE_H_START, "end_time": FIVE_H_END,
             "remains_time": 7151954, "current_interval_total_count": 1500,
             "current_interval_usage_count": 228, "current_interval_remaining_percent": 84.8,
             "current_weekly_total_count": 10500, "current_weekly_usage_count": 1000,
             "current_weekly_remaining_percent": 90.5, "weekly_start_time": WEEK_START,
             "weekly_end_time": WEEK_END, "weekly_remains_time": 248351954, **text}
    image = {"model_name": "image-01", "start_time": FIVE_H_START, "end_time": 1776441600000,
             "current_interval_total_count": 100, "current_interval_usage_count": 0,
             "current_interval_remaining_percent": 1.0}
    return {"base_resp": {"status_code": 0, "status_msg": "success"}, "model_remains": [image, entry]}


class Upstream:
    """Fake vendor host: records every request that reaches the transport."""

    def __init__(self, status=200, body=None, text=None):
        self.status, self.body, self.text, self.requests = status, body, text, []

    def handler(self, request):
        self.requests.append(request)
        if self.text is not None:
            return httpx.Response(self.status, text=self.text)
        return httpx.Response(self.status, json=self.body)


_RealAsyncClient = httpx.AsyncClient


@pytest.fixture
def upstream():
    fake = Upstream()

    def factory(*args, **kwargs):
        kwargs = dict(kwargs, transport=httpx.MockTransport(fake.handler))
        return _RealAsyncClient(*args, **kwargs)

    with patch("app.core.provider_usage.httpx.AsyncClient", side_effect=factory):
        yield fake


def make_row(provider_id, provider, mode, key, base_url=None):
    db.create_model_provider(provider_id, provider_id, provider, base_url or native_providers.endpoint_root(provider, mode),
                             auth_token=key, auth_mode=mode, api_dialect="anthropic")
    return db.get_model_provider(provider_id)


@pytest.fixture
def kimi(mock_db):
    return make_row("kimi-key", "kimi", None, KIMI_KEY)


@pytest.fixture
def minimax(mock_db):
    return make_row("minimax-plan", "minimax", "plan_key", MINIMAX_KEY)


def snapshot(provider_id):
    return next((s for s in db.get_all_provider_usage_snapshots() if s["provider_id"] == provider_id), None)


def test_registry_reaches_only_documented_kinds_and_hosts():
    def row(provider, mode=None, base_url=None):
        return {"provider_type": provider, "auth_mode": mode, "base_url": base_url}

    assert [s.id for s in usage_sources.polled_sources(row("kimi"))] == ["kimi_balance"]
    assert [s.id for s in usage_sources.polled_sources(row("minimax", "plan_key"))] == ["minimax_plan"]
    for other in (row("kimi", "plan_key"), row("minimax"), row("opencode"), row("opencode", "plan_key"),
                  row("kimi", None, "https://proxy.example/anthropic"),
                  row("kimi", None, "http://api.moonshot.ai/anthropic"),
                  row("minimax", "plan_key", "https://api.minimaxi.com/anthropic")):
        assert usage_sources.polled_sources(other) == ()
    for source_id in ("kimi_balance", "minimax_plan"):
        source = usage_sources.SOURCES[source_id]
        assert source.support == usage_sources.DOCUMENTED and source.dashboard_url.startswith("https://")
    assert native_providers.usage_meter_available("kimi", None)
    assert native_providers.usage_meter_available("minimax", "plan_key")
    for provider, mode in (("kimi", "plan_key"), ("minimax", None), ("opencode", None), ("opencode", "plan_key")):
        assert not native_providers.usage_meter_available(provider, mode)


def test_kimi_balance_normalizer_rejects_malformed_bodies():
    assert normalize_kimi_balance(KIMI_BODY) == {"balance_remaining": 49.58894, "currency": "USD"}
    assert normalize_kimi_balance({**KIMI_BODY, "data": {"available_balance": -2.5}})["balance_remaining"] == -2.5
    for body in (None, [], "x", {}, {"code": 1, "data": KIMI_BODY["data"]}, {**KIMI_BODY, "status": False},
                 {"code": 0, "data": {"available_balance": "49"}}, {"code": 0, "data": {"available_balance": True}},
                 {"code": 0, "data": {"available_balance": float("nan")}}, {"code": 0, "data": []}):
        assert normalize_kimi_balance(body) is None


def test_minimax_plan_normalizer_reads_explicit_percentages_only():
    assert normalize_minimax_plan(minimax_body()) == [
        {"type": "five_hour", "utilization": 15.2, "resets_at": str(FIVE_H_END)},
        {"type": "seven_day", "utilization": 9.5, "resets_at": str(WEEK_END)},
    ]
    # Without a percentage the counts are ambiguous (remaining in older
    # responses, used in newer ones), so that window is not reported.
    no_pct = normalize_minimax_plan(minimax_body(current_weekly_remaining_percent=None))
    assert [w["type"] for w in no_pct] == ["five_hour"]
    exhausted = normalize_minimax_plan(minimax_body(current_interval_status=2, current_interval_remaining_percent=None))
    assert exhausted[0] == {"type": "five_hour", "utilization": 100.0, "resets_at": str(FIVE_H_END)}
    unlimited = normalize_minimax_plan(minimax_body(current_interval_status=3, current_interval_total_count=0,
                                                    current_weekly_status=3, current_weekly_total_count=0))
    assert unlimited is None
    for body in (None, [], {}, {"model_remains": "x"}, {"base_resp": {"status_code": 1004}, "model_remains": []},
                 {**minimax_body(), "base_resp": {"status_code": 2049, "status_msg": "invalid api key"}},
                 {"model_remains": [{"model_name": "image-01", "current_interval_total_count": 5,
                                     "current_interval_remaining_percent": 50}]},
                 minimax_body(current_interval_remaining_percent=140, current_weekly_remaining_percent="90")):
        assert normalize_minimax_plan(body) is None


async def test_kimi_balance_poll_reads_the_documented_endpoint_once(kimi, upstream, caplog):
    upstream.body = KIMI_BODY
    await poll_kimi_balance(kimi)
    assert [str(r.url) for r in upstream.requests] == [usage_sources.KIMI_BALANCE_URL]
    assert upstream.requests[0].headers["authorization"] == f"Bearer {KIMI_KEY}"
    result = snapshot("kimi-key")
    assert result["provider_type"] == "kimi_balance"
    assert result["credits"] == {"balance_remaining": 49.58894, "currency": "USD"}
    assert KIMI_KEY not in str(result) and "voucher" not in str(result)
    from app.api.usage import _load_provider_usage

    entry = {e.provider_id: e for e in await _load_provider_usage()}["kimi-key"]
    assert entry.credits["balance_remaining"] == 49.58894 and entry.status == "fresh"
    assert entry.source == "Kimi balance API"
    assert entry.source_info.dashboard_url == "https://platform.kimi.ai/console/account"
    assert KIMI_KEY not in caplog.text


async def test_minimax_plan_poll_stores_windows(minimax, upstream):
    upstream.body = minimax_body()
    await poll_minimax_plan(minimax)
    assert [str(r.url) for r in upstream.requests] == [usage_sources.MINIMAX_PLAN_URL]
    assert upstream.requests[0].headers["authorization"] == f"Bearer {MINIMAX_KEY}"
    result = snapshot("minimax-plan")
    assert result["provider_type"] == "minimax_plan"
    assert [(w["type"], w["utilization"]) for w in result["windows"]] == [("five_hour", 15.2), ("seven_day", 9.5)]
    from app.api.usage import _load_provider_usage

    entry = {e.provider_id: e for e in await _load_provider_usage()}["minimax-plan"]
    assert entry.source_info.dashboard_url == "https://platform.minimax.io/console/usage"
    assert [w.resets_at for w in entry.windows] == [str(FIVE_H_END), str(WEEK_END)]


@pytest.mark.parametrize("status", [401, 403])
async def test_denied_key_is_not_reprobed_until_the_key_is_replaced(kimi, upstream, status, caplog):
    upstream.status, upstream.body = status, {"error": {"message": f"bad key {KIMI_KEY}"}}
    await poll_kimi_balance(kimi)
    await poll_kimi_balance(kimi)
    assert len(upstream.requests) == 1
    assert snapshot("kimi-key")["raw"]["usage_error"] == "billing_access_denied"
    assert KIMI_KEY not in caplog.text and KIMI_KEY not in str(snapshot("kimi-key"))
    db.update_model_provider("kimi-key", auth_token="sk-test-kimi-replacement")
    assert snapshot("kimi-key") is None
    upstream.status, upstream.body = 200, KIMI_BODY
    await poll_kimi_balance(kimi)
    assert len(upstream.requests) == 2
    assert upstream.requests[1].headers["authorization"] == "Bearer sk-test-kimi-replacement"
    assert snapshot("kimi-key")["credits"]["balance_remaining"] == 49.58894


async def test_minimax_business_auth_error_counts_as_denied(minimax, upstream):
    upstream.body = {"base_resp": {"status_code": 1004, "status_msg": "not authorized"}}
    await poll_minimax_plan(minimax)
    await poll_minimax_plan(minimax)
    assert len(upstream.requests) == 1
    assert snapshot("minimax-plan")["raw"]["usage_error"] == "billing_access_denied"


@pytest.fixture
def budget_clock(monkeypatch):
    """Advance only the request budget's clock, to step past min_interval."""
    import types

    from app.core import usage_polling
    from app.network import usage_budget

    now = {"t": 1_900_000_000.0}
    monkeypatch.setattr(usage_budget, "time", types.SimpleNamespace(time=lambda: now["t"]))
    monkeypatch.setattr(usage_polling.random, "uniform", lambda *_: 0)
    return now


async def test_second_read_inside_the_interval_sends_nothing(kimi, upstream, budget_clock):
    upstream.body = KIMI_BODY
    await poll_kimi_balance(kimi)
    await poll_kimi_balance(kimi)
    assert len(upstream.requests) == 1
    budget_clock["t"] += usage_sources.interval_for("kimi_balance", usage_sources.KIMI_BALANCE_URL) + 1
    await poll_kimi_balance(kimi)
    assert len(upstream.requests) == 2


@pytest.mark.parametrize("response", [{"text": "<html>not json</html>"}, {"body": {"code": 0, "data": None}},
                                      {"body": [1, 2]}, {"status": 500, "body": {}}])
async def test_malformed_or_failed_reading_keeps_the_last_balance(kimi, upstream, budget_clock, response):
    upstream.body = KIMI_BODY
    await poll_kimi_balance(kimi)
    fetched_at = snapshot("kimi-key")["fetched_at"]
    upstream.status = response.get("status", 200)
    upstream.body, upstream.text = response.get("body"), response.get("text")
    budget_clock["t"] += 3600
    await poll_kimi_balance(kimi)
    assert len(upstream.requests) == 2
    result = snapshot("kimi-key")
    assert result["credits"]["balance_remaining"] == 49.58894
    assert result["fetched_at"] == fetched_at
    assert result["raw"]["usage_error"] == "unavailable"


async def test_malformed_first_reading_is_unavailable_not_zero(minimax, upstream):
    upstream.body = {"base_resp": {"status_code": 0}, "model_remains": [{"model_name": "MiniMax-M*"}]}
    await poll_minimax_plan(minimax)
    result = snapshot("minimax-plan")
    assert result["windows"] == [] and result["credits"] is None
    assert result["raw"]["usage_error"] == "unavailable"


async def test_unlisted_url_is_refused_before_any_request(upstream):
    from app.core.provider_usage import _registered_get

    with pytest.raises(ValueError):
        await _registered_get("kimi_balance", "https://platform.kimi.ai/api/user/balance",
                              headers={"Authorization": f"Bearer {KIMI_KEY}"})
    assert upstream.requests == []


@pytest.mark.parametrize("mutation", ["replace", "disable", "delete"])
@pytest.mark.parametrize("status", [200, 403])
async def test_late_reading_cannot_outlive_the_key(kimi, mutation, status):
    entered, release = asyncio.Event(), asyncio.Event()

    async def slow(request):
        entered.set()
        await release.wait()
        return httpx.Response(status, json=KIMI_BODY)

    def factory(*args, **kwargs):
        return _RealAsyncClient(*args, **dict(kwargs, transport=httpx.MockTransport(slow)))

    with patch("app.core.provider_usage.httpx.AsyncClient", side_effect=factory):
        task = asyncio.create_task(poll_kimi_balance(kimi))
        await asyncio.wait_for(entered.wait(), 5)
        if mutation == "replace":
            db.update_model_provider("kimi-key", auth_token="sk-test-kimi-new")
        elif mutation == "disable":
            db.set_model_provider_enabled("kimi-key", False)
        else:
            db.delete_model_provider("kimi-key")
        release.set()
        await task
    assert snapshot("kimi-key") is None


async def test_rows_without_a_source_send_nothing(mock_db, upstream):
    from app.core.provider_usage import ProviderUsageMonitor

    for provider, mode in (("kimi", "plan_key"), ("minimax", None), ("opencode", None), ("opencode", "plan_key")):
        make_row(f"{provider}-{mode or 'api'}", provider, mode, "sk-test-unused")
    # Row-scoped sources only: the account-wide fal.ai reader is not a row's.
    with patch("app.network.engine.cached_context", return_value=None), \
            patch.object(usage_sources, "global_sources", return_value=()):
        await ProviderUsageMonitor()._poll_providers()
    assert upstream.requests == []
    assert db.get_all_provider_usage_snapshots() == []


LINK_ONLY = {
    ("kimi", "plan_key"): ("kimi_code", "https://www.kimi.com/membership/subscription?tab=quota"),
    ("minimax", None): ("minimax_api", "https://platform.minimax.io/user-center/payment/balance"),
    ("opencode", "plan_key"): ("opencode_go", "https://opencode.ai/console"),
    ("opencode", None): ("opencode_console", "https://opencode.ai/console"),
}


def test_link_only_kinds_point_at_the_vendor_page_without_a_meter():
    from pathlib import Path

    def row(provider, mode=None, base_url=None):
        return {"provider_type": provider, "auth_mode": mode, "base_url": base_url}

    doc = (Path(__file__).resolve().parents[2] / "docs" / "provider-usage-sources.md").read_text(encoding="utf-8")
    for (provider, mode), (kind, url) in LINK_ONLY.items():
        link = usage_sources.usage_link_for(row(provider, mode))
        assert (link.id, link.dashboard_url) == (kind, url)
        assert url in doc
        # Subscriptions have a quota and no dollar figure; pay-as-you-go keys
        # lack only a balance (OpenCode Console cost is still recorded per turn).
        assert link.note == ("No quota meter for this key" if mode == "plan_key" else "No balance meter for this key")
        assert link.subscription == (mode == "plan_key")
        assert usage_sources.polled_sources(row(provider, mode)) == ()
        assert not native_providers.usage_meter_available(provider, mode)
    for other in (row("kimi"), row("minimax", "plan_key"), row("custom"),
                  row("kimi", "plan_key", "https://gateway.example.test/coding"),
                  row("minimax", None, "https://api.minimaxi.com/anthropic"),
                  row("opencode", None, "http://opencode.ai/inference/anthropic")):
        assert usage_sources.usage_link_for(other) is None


async def test_link_only_rows_are_listed_but_send_and_store_nothing(mock_db, upstream):
    from types import SimpleNamespace

    from app.api.usage import _load_usage_links
    from app.core.provider_usage import ProviderUsageMonitor

    for provider, mode in LINK_ONLY:
        make_row(f"{provider}-{mode or 'api'}", provider, mode, "sk-test-unused")
    with patch("app.network.engine.cached_context", return_value=None), \
            patch.object(usage_sources, "global_sources", return_value=()):
        await ProviderUsageMonitor()._poll_providers()
        links = await _load_usage_links([])
        # A row that already has a reading keeps that section instead.
        without_go = await _load_usage_links([SimpleNamespace(provider_id="opencode-plan_key")])
    assert upstream.requests == []
    assert db.get_all_provider_usage_snapshots() == []
    assert {(link.provider_id, link.kind, link.dashboard_url) for link in links} == {
        (f"{provider}-{mode or 'api'}", kind, url) for (provider, mode), (kind, url) in LINK_ONLY.items()
    }
    assert "opencode-plan_key" not in {link.provider_id for link in without_go}


async def test_poll_failure_logs_never_carry_the_key(kimi, upstream, caplog):
    def boom(request):
        raise httpx.ConnectError(f"refused with {request.headers['authorization']}")

    upstream.handler = boom
    with caplog.at_level(logging.DEBUG):
        await poll_kimi_balance(kimi)
    assert len(upstream.requests) == 0  # the handler raised before recording
    assert KIMI_KEY not in caplog.text
    assert KIMI_KEY not in str(snapshot("kimi-key"))
