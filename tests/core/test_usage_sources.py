"""Usage source registry: capability definitions, registry-driven polling and
negative-capability caching.

Upstream requests are counted at the HTTP transport; no provider is contacted.
"""

from urllib.parse import urlsplit

import httpx
import pytest

from app.core import encryption, provider_usage, usage_sources
from app.core.config import settings
from app.core.openrouter_usage import SETTING_PREFIX, SNAPSHOT_PREFIX
from app.core.provider_usage import ProviderUsageMonitor
from app.db import database as db
from app.network import usage_budget

_RealAsyncClient = httpx.AsyncClient


@pytest.fixture
def upstream(monkeypatch):
    """Record every upstream request; answer each host with a configurable status."""
    state = {"requests": [], "status": {}, "claims": 0}

    def handler(request):
        state["requests"].append(request)
        status = state["status"].get(request.url.host, 200)
        path = request.url.path
        if status != 200:
            body = {}
        elif path.endswith("/credits"):
            body = {"data": {"total_credits": 10, "total_usage": 4}}
        elif path.endswith("/key"):
            body = {"data": {"usage": 1.5, "limit": 10}}
        elif path.endswith("/billing/user_balance"):
            body = 7
        elif path.endswith("/billing"):
            body = {"config": {"creditUsagePercent": 12}}
        elif path.endswith("/settings"):
            body = {"subscription_tier_display": "SuperGrok"}
        else:
            body = {"data": [], "has_more": False}
        return httpx.Response(status, json=body)

    def factory(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return _RealAsyncClient(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", factory)
    original = usage_budget.acquire

    async def counting_acquire(*args, **kwargs):
        state["claims"] += 1
        return await original(*args, **kwargs)

    monkeypatch.setattr(usage_budget, "acquire", counting_acquire)
    return state


def hosts(state):
    return [r.url.host for r in state["requests"]]


# ---------------------------------------------------------------------------
# Registry definitions
# ---------------------------------------------------------------------------

def test_every_source_has_a_complete_capability_definition():
    for source in usage_sources.SOURCES.values():
        public = source.public()
        assert public["dashboard_url"].startswith("https://"), source.id
        assert source.scope in {"api_key", "project", "organization", "account", "subscription"}
        assert source.measure in usage_sources.MEASURE_LABELS
        assert source.support in usage_sources.SUPPORT_LABELS
        assert source.collection in {usage_sources.PASSIVE, usage_sources.POLLED}
        assert source.credential and source.failure_policy and source.scope_label
        if source.collection == usage_sources.POLLED:
            assert source.endpoints and all(e.min_interval >= 300 for e in source.endpoints)
        else:
            assert not source.endpoints and source.poller is None
        if source.poller:
            assert callable(getattr(ProviderUsageMonitor, source.poller))


def test_subscription_endpoints_are_labeled_unofficial():
    for source_id in ("grok_subscription", "codex_headers", "codex_usage"):
        source = usage_sources.SOURCES[source_id]
        assert source.subscription_specific
        assert source.public()["support_label"] == "Unofficial, subscription-specific endpoint"
    for source_id in ("openai_admin", "openrouter_key", "openrouter_credits"):
        assert usage_sources.SOURCES[source_id].support == usage_sources.DOCUMENTED


def test_fal_balance_uses_the_ordinary_key_endpoint_and_says_it_is_undocumented():
    source = usage_sources.SOURCES["fal_billing"]
    assert [e.url for e in source.endpoints] == ["https://rest.alpha.fal.ai/billing/user_balance"]
    assert source.support == usage_sources.UNDOCUMENTED
    assert source.public()["support_label"] == "Undocumented endpoint"
    assert "Admin" not in source.credential


def test_measures_distinguish_spend_credit_allowance_and_rate_limits():
    measure = {s.id: s.measure for s in usage_sources.SOURCES.values()}
    assert measure["openai_admin"] == usage_sources.REPORTED_SPEND
    assert measure["openrouter_key"] == usage_sources.REPORTED_SPEND
    assert measure["openrouter_credits"] == usage_sources.MONETARY_CREDIT
    assert measure["fal_billing"] == usage_sources.MONETARY_CREDIT
    assert measure["claude_rate_limit_events"] == usage_sources.SUBSCRIPTION_ALLOWANCE
    assert measure["grok_subscription"] == usage_sources.SUBSCRIPTION_ALLOWANCE
    assert measure["codex_usage"] == usage_sources.SUBSCRIPTION_ALLOWANCE
    assert measure["openai_headers"] == usage_sources.RATE_LIMIT
    assert "prepaid credit balance" in usage_sources.SOURCES["openai_admin"].unavailable


@pytest.mark.parametrize("row, expected", [
    ({"provider_type": "openrouter", "base_url": "https://openrouter.ai/api/v1"},
     {"openrouter_key", "openrouter_credits"}),
    ({"provider_type": "openrouter", "base_url": "https://proxy.example/api/v1"}, {"openrouter_key"}),
    ({"provider_type": "openai", "base_url": "https://api.openai.com/v1"}, {"openai_admin"}),
    ({"provider_type": "openai", "base_url": "https://proxy.example/v1"}, set()),
    ({"provider_type": "grok"}, {"grok_subscription"}),
    ({"provider_type": "custom", "auth_mode": "grok"}, {"grok_subscription"}),
    ({"provider_type": "xai"}, set()),
    ({"provider_type": "chatgpt"}, {"codex_usage"}),
    ({"provider_type": "custom", "auth_mode": "chatgpt"}, {"codex_usage"}),
    ({"provider_type": "anthropic"}, set()),
])
def test_polled_sources_follow_the_registry(row, expected):
    assert {s.id for s in usage_sources.polled_sources(row)} == expected
    assert [s.id for s in usage_sources.global_sources()] == ["fal_billing"]


@pytest.mark.parametrize("url", [
    "https://api.openai.com/v1/dashboard/billing/credit_grants",
    "https://api.fal.ai/v1/account/billing?expand=credits",
    "https://api.fal.ai/v1/account/billing",
])
def test_unregistered_endpoints_are_refused(url):
    source = "fal_billing" if "fal" in url else "openai_admin"
    with pytest.raises(ValueError):
        usage_sources.endpoint_for(source, url)


def test_template_endpoint_matches_the_configured_host_only():
    assert usage_sources.endpoint_for("openrouter_key", "https://proxy.example/api/v1/key").min_interval == 300
    with pytest.raises(ValueError):
        usage_sources.endpoint_for("openrouter_key", "https://proxy.example/api/v1/credits")


@pytest.mark.asyncio
async def test_unregistered_url_never_reaches_the_network(upstream):
    with pytest.raises(ValueError):
        await provider_usage._registered_get(
            "openai_admin", "https://api.openai.com/v1/dashboard/billing/credit_grants",
            headers={"Authorization": "Bearer k"},
        )
    assert not upstream["requests"] and upstream["claims"] == 0


# ---------------------------------------------------------------------------
# Registry-driven poll
# ---------------------------------------------------------------------------

def _openrouter(provider_id="router"):
    row = db.create_model_provider(provider_id, "Router", "openrouter", "https://openrouter.ai/api/v1",
                                   auth_token="model-key")
    db.set_system_setting(SETTING_PREFIX + row["id"], "management-key")
    return row


@pytest.mark.asyncio
async def test_full_poll_only_requests_registered_endpoints(upstream, monkeypatch):
    _openrouter()
    db.create_model_provider("xai-row", "xAI", "xai", "https://api.x.ai/v1", auth_token="xai-key")
    db.create_model_provider("custom-openai", "Proxy", "openai", "https://proxy.example/v1", auth_token="k")
    db.set_system_setting("fal_api_key", "fal-media-key")
    await ProviderUsageMonitor().poll_once()

    registered = []
    for request in upstream["requests"]:
        url = str(request.url).split("?")[0]
        matches = [s.id for s in usage_sources.SOURCES.values()
                   for e in s.endpoints
                   if e.url.split("?")[0].replace("{origin}", f"{request.url.scheme}://{request.url.host}") == url]
        assert matches, f"unregistered upstream request {url}"
        registered.extend(matches)
    assert sorted(registered) == ["fal_billing", "openrouter_credits", "openrouter_key"]
    assert "api.x.ai" not in hosts(upstream) and "proxy.example" not in hosts(upstream)


# ---------------------------------------------------------------------------
# Negative capability cache
# ---------------------------------------------------------------------------

def _raw(snapshot_id):
    return db.get_provider_usage_raw(snapshot_id) or {}


@pytest.mark.asyncio
async def test_denied_fal_key_is_not_reprobed_until_resaved(upstream):
    db.set_system_setting("fal_api_key", "fal-media-key")
    upstream["status"]["rest.alpha.fal.ai"] = 403
    monitor = ProviderUsageMonitor()
    await monitor._poll_fal()
    assert hosts(upstream) == ["rest.alpha.fal.ai"]
    assert _raw("fal-ai")["usage_error"] == "billing_access_denied"
    assert "fal-media-key" not in str(_raw("fal-ai"))

    for _ in range(3):
        await monitor._poll_fal()
    # Known-denied: no budget claim and no upstream request.
    assert len(upstream["requests"]) == 1 and upstream["claims"] == 1

    # Re-saving the same key bumps the credential revision and probes again.
    db.set_system_setting("fal_api_key", "fal-media-key")
    await usage_budget.resume_authentication(provider_usage.FAL_BALANCE_URL, {"Authorization": "Key fal-media-key"})
    upstream["status"]["rest.alpha.fal.ai"] = 200
    await monitor._poll_fal()
    assert len(upstream["requests"]) == 2
    snapshot = next(s for s in db.get_all_provider_usage_snapshots() if s["provider_id"] == "fal-ai")
    assert snapshot["credits"]["balance_remaining"] == 7
    assert "denied_fingerprint" not in snapshot["raw"]


@pytest.mark.asyncio
@pytest.mark.parametrize("status, kind", [(401, "billing_access_denied"), (410, "unsupported")])
async def test_refused_openrouter_management_key_is_negative_cached(upstream, status, kind):
    from app.core import openrouter_usage

    row = _openrouter()
    upstream["status"]["openrouter.ai"] = status
    await openrouter_usage.poll(row)
    await openrouter_usage.poll(row)
    assert len(upstream["requests"]) == 1 and upstream["claims"] == 1
    assert _raw(SNAPSHOT_PREFIX + row["id"])["usage_error"] == kind

    db.set_system_setting(SETTING_PREFIX + row["id"], "replacement-key")
    upstream["status"]["openrouter.ai"] = 200
    await openrouter_usage.poll(row)
    assert len(upstream["requests"]) == 2
    assert upstream["requests"][-1].headers["authorization"] == "Bearer replacement-key"


@pytest.mark.asyncio
async def test_refused_openrouter_model_key_is_negative_cached_per_key(upstream):
    row = db.get_model_provider(_openrouter()["id"], include_token=True)
    upstream["status"]["openrouter.ai"] = 403
    monitor = ProviderUsageMonitor()
    await monitor._poll_provider(row)
    await monitor._poll_provider(row)
    assert [r.url.path for r in upstream["requests"]] == ["/api/v1/key"]
    assert _raw(row["id"])["usage_error"] == "billing_access_denied"

    rotated = {**row, "auth_token": "rotated-model-key"}
    upstream["status"]["openrouter.ai"] = 200
    await monitor._poll_provider(rotated)
    assert len(upstream["requests"]) == 2


@pytest.mark.asyncio
async def test_refused_grok_token_is_negative_cached_until_token_changes(upstream, monkeypatch):
    tokens = {"codex_access_token": "grok-token"}
    monkeypatch.setattr(db, "get_codex_tokens", lambda _pid: dict(tokens))
    upstream["status"]["cli-chat-proxy.grok.com"] = 403
    row = {"id": "grok-row", "provider_type": "grok"}
    monitor = ProviderUsageMonitor()
    await monitor._poll_grok_provider(row)
    await monitor._poll_grok_provider(row)
    assert len(upstream["requests"]) == 1 and upstream["claims"] == 1
    assert _raw("grok-row")["usage_error"] == "billing_access_denied"

    tokens["codex_access_token"] = "renewed-token"
    upstream["status"]["cli-chat-proxy.grok.com"] = 200
    await monitor._poll_grok_provider(row)
    assert [r.url.path for r in upstream["requests"][1:]] == ["/v1/billing", "/v1/settings"]


@pytest.mark.asyncio
async def test_refused_openai_admin_key_is_negative_cached_until_resaved(authenticated_client, upstream):
    row = db.create_model_provider(provider_id="test-openai", name="OpenAI", provider_type="openai",
                                   base_url="https://api.openai.com", auth_token="inference-key")
    db.set_openai_billing_binding(row["id"], "org-test", settings.effective_instance_id,
                                  encryption.encrypt_value("admin-key"))
    upstream["status"]["api.openai.com"] = 403
    monitor = ProviderUsageMonitor()
    assert not await monitor._poll_openai_provider(row)
    first = len(upstream["requests"])
    assert first == 2  # costs + completions, once each
    assert not await monitor._poll_openai_provider(row)
    assert len(upstream["requests"]) == first and upstream["claims"] == 2

    # Re-saving re-encrypts the key: a new revision is probed again.
    db.set_openai_billing_binding(row["id"], "org-test", settings.effective_instance_id,
                                  encryption.encrypt_value("admin-key"))
    for endpoint in ("costs", "usage/completions"):
        await usage_budget.resume_authentication(
            f"{usage_sources.OPENAI_ADMIN_BASE}/{endpoint}",
            {"Authorization": "Bearer admin-key", "OpenAI-Organization": "org-test"},
        )
    upstream["status"]["api.openai.com"] = 200
    assert await monitor._poll_openai_provider(row)
    assert len(upstream["requests"]) == first + 2


# ---------------------------------------------------------------------------
# Passive capture classification
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
@pytest.mark.parametrize("provider_type, base_url, expected", [
    ("openai", "https://api.openai.com/v1", "openai"),
    ("xai", "https://api.x.ai/v1", "xai"),
    ("openai", "https://proxy.example/v1", "openai_compatible"),
    ("grok", "", None),
])
async def test_passive_headers_are_labeled_by_the_row_source(provider_type, base_url, expected):
    db.create_model_provider("row", "Row", provider_type, base_url)
    if expected is None:
        db.upsert_provider_usage_snapshot("row", "grok", "SuperGrok", [{"type": "seven_day", "utilization": 5}],
                                          None, {}, 1)
    await provider_usage._persist_openai_capture(
        "row", {"x-ratelimit-limit-requests": "60", "x-ratelimit-remaining-requests": "30"})
    snapshot = next(s for s in db.get_all_provider_usage_snapshots() if s["provider_id"] == "row")
    if expected is None:
        # The subscription allowance is not overwritten by per-minute headroom.
        assert snapshot["provider_type"] == "grok"
        assert snapshot["windows"] == [{"type": "seven_day", "utilization": 5}]
    else:
        assert snapshot["provider_type"] == expected


def test_passive_snapshot_type_defaults():
    assert usage_sources.passive_snapshot_type(None, codex_backend=True) == "chatgpt"
    assert usage_sources.passive_snapshot_type(None, codex_backend=False) == "openai"


def test_claude_usage_comes_only_from_the_cli_stream():
    claude = [s for s in usage_sources.SOURCES.values() if s.snapshot_type == "claude"]
    assert [s.id for s in claude] == ["claude_rate_limit_events"]
    source = claude[0]
    assert source.collection == usage_sources.PASSIVE and not source.endpoints and source.poller is None
    assert source.dashboard_url == "https://claude.ai/settings/usage"
    assert not hasattr(usage_sources, "CLAUDE_OAUTH_USAGE_URL")
    for s in usage_sources.SOURCES.values():
        for endpoint in s.endpoints:
            assert "anthropic.com" not in (urlsplit(endpoint.url).hostname or "")


def test_matrix_is_json_safe():
    import json

    rows = usage_sources.matrix()
    assert json.loads(json.dumps(rows)) == rows
    assert {r["id"] for r in rows} == set(usage_sources.SOURCES)


def test_docs_matrix_lists_every_registry_source():
    from pathlib import Path

    doc = (Path(__file__).resolve().parents[2] / "docs" / "provider-usage-sources.md").read_text(encoding="utf-8")
    for source in usage_sources.SOURCES.values():
        assert f"`{source.id}`" in doc, source.id
        assert source.dashboard_url in doc, source.id
