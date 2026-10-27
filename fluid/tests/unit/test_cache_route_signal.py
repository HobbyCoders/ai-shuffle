"""Native routing feeds scheduling, probe labels and reuse-floor learning."""

from types import SimpleNamespace

import pytest

from fluid.engine.native import NativeLoop
from fluid.messages import Message, Text
from fluid.providers.base import ProviderCaps, ProviderRequest
from fluid.store.base import MemoryStore


@pytest.mark.parametrize("name,url,model,ttl,gap,reason,miss_class", [
    ("openai_responses", "https://chatgpt.com/backend-api/codex/responses", "gpt-5.6",
     None, 36_000, "", "provider_fallback"),
    ("openai_responses", "https://api.openai.com/v1/responses", "gpt-5.6",
     1800, 1200, "", "provider_fallback"),
    ("openai_responses", "https://api.openai.com/v1/responses", "gpt-4.1",
     1800, 2400, "gap", "idle_past_ttl"),
    ("openai_responses", "https://api.x.ai/v1/responses", "grok-4",
     300, 360, "gap", "idle_past_ttl"),
    ("anthropic", "https://api.anthropic.com/v1/messages", "claude-sonnet-5",
     300, 360, "gap", "idle_past_ttl"),
    ("openrouter", "https://openrouter.ai/api/v1/chat/completions", "openai/gpt-5.6",
     600, 540, "", "provider_fallback"),
])
async def test_native_route_policy_reaches_all_cache_consumers(
    monkeypatch, name, url, model, ttl, gap, reason, miss_class,
):
    provider = SimpleNamespace(name=name, _base_url=url, caps=ProviderCaps())
    loop = NativeLoop(provider=provider, store=MemoryStore(), model=model, system="stable")
    now = 100_000.0
    monkeypatch.setattr("fluid.engine.native.time.time", lambda: now)
    try:
        first = loop._cache_signal("route", overhead_tokens=0, turn_start=None)
        assert first.ttl_seconds == ttl
        request = ProviderRequest(model=model, system="system " * 40_000,
                                  messages=[Message("user", [Text("hello")])])
        usage = {"input_tokens": 60_000, "cache_read_input_tokens": 0}
        # Drive fingerprint/usage production, rather than seeding the TTL field
        # the classifier reads. Identical requests make any miss a shortfall.
        loop._fingerprint_request(request)
        loop._cache_probe_fields(usage)
        now += gap
        loop._fingerprint_request(request)
        raw = loop._cache_probe_fields(usage)
        assert raw["cache_ttl_seconds"] == ttl
        assert raw["request_interval_seconds"] == gap
        assert raw["cache_miss_class"] == miss_class
        signal = loop._cache_signal("route", overhead_tokens=0, turn_start=None)
        assert signal.gap_s == gap and signal.cold_reason == reason
        loop._fold_reuse_floor(raw, usage, 5000, 60_000)
        assert ("reuse_floor_tokens" in raw) is (reason != "gap")
        loop.system = "changed"
        assert loop._cache_signal("route", overhead_tokens=0, turn_start=None).cold_reason == "static_changed"
    finally:
        await loop.aclose()
