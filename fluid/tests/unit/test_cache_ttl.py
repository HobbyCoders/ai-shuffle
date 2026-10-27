"""Route policy: conservative idle evidence, never request options."""

import pytest

from fluid.context.cache_signal import CacheSignal
from fluid.context.cache_ttl import (
    delegated_cache_write_ttl, last_real_main_usage, resolve_cache_ttl,
)


@pytest.mark.parametrize("leg,route,ttl", [
    ("delegated", {}, 3600),
    ("anthropic", {}, 300),
    ("anthropic", {"base_url": "https://api.anthropic.com/v1/messages"}, 300),
    ("openai_responses", {"base_url": "https://chatgpt.com/backend-api/codex/responses"}, None),
    ("openai_responses", {"provider_type": "chatgpt"}, None),
    # Subscription credentials select Codex even if the stored row has an old URL.
    ("openai_responses", {"provider_type": "chatgpt", "base_url": "https://api.openai.com/v1"}, None),
    ("openai_responses", {"provider_type": "custom", "auth_mode": "chatgpt"}, None),
    ("openai_responses", {"provider_type": "openai", "model": "gpt-5.6"}, 1800),
    ("openai_responses", {"base_url": "https://api.openai.com/v1/responses", "model": "gpt-4.1"}, 1800),
    # API host wins over a misleading model name.
    ("openai_responses", {"base_url": "https://api.openai.com/v1/responses", "model": "gpt-5.3-codex"}, 1800),
    ("openai_responses", {"base_url": "https://api.x.ai/v1/responses"}, 300),
    ("openai_responses", {"provider_type": "xai"}, 300),
    ("openai_responses", {"provider_type": "grok"}, 300),
    ("openai_responses", {"provider_type": "custom", "auth_mode": "grok"}, 300),
    ("openrouter", {"model": "anthropic/claude-sonnet-5"}, 300),
    ("openrouter", {"model": "openai/gpt-5.6"}, 600),
    ("openrouter", {"model": "x-ai/grok-4"}, 300),
    ("openrouter", {"model": "unknown"}, 300),
    ("local", {}, 300),
    ("unknown", {}, 300),
    (None, {}, 300),
    ("openai_responses", {}, 1800),
    ("openai_responses", {"model": "gpt-5.6"}, 1800),
    ("openai_responses", {"base_url": "https://proxy.example/v1/responses"}, 300),
    # A lookalike hostname must not inherit Codex's non-expiry policy.
    ("openai_responses", {"base_url": "https://chatgpt.com.example/backend-api/codex"}, 300),
])
def test_route_policy(leg, route, ttl):
    assert resolve_cache_ttl(leg, **route) == ttl


def _writes(five=0, hour=0, **usage):
    return {"cache_creation_input_tokens": five + hour, "cache_creation": {
        "ephemeral_5m_input_tokens": five, "ephemeral_1h_input_tokens": hour,
    }, **usage}


@pytest.mark.parametrize("usage,ttl", [
    (_writes(five=100), 300),
    (_writes(hour=100), 3600),
    (_writes(five=100, hour=100), 3600),
    ({"input_tokens": 100}, 3600),  # missing evidence is not evidence of 5m
    ({"cache_read_input_tokens": 100}, 3600),
    (_writes(five=100, auxiliary_source="checkpoint_summary"), 3600),
    (_writes(five=100, usage_estimated=True), 3600),
    (_writes(five=100, input_tokens_estimated=True), 3600),
    (_writes(five=100, output_tokens_estimated=True), 3600),
    (_writes(five=100, cache_creation_input_tokens=0), 3600),  # zero-prompt snapshot
])
def test_delegated_write_evidence(usage, ttl):
    assert resolve_cache_ttl("delegated", last_usage=usage) == ttl
    assert delegated_cache_write_ttl(usage) == ("5m" if ttl == 300 else "1h")


def test_scan_to_last_real_main_call_not_latest_snapshot_or_auxiliary():
    real = _writes(five=100)
    calls = [{}, _writes(hour=100, cache_creation_input_tokens=0),
             _writes(hour=100, auxiliary_source="checkpoint_summary"), real, _writes(hour=100)]
    assert last_real_main_usage(calls) is real
    assert resolve_cache_ttl("delegated", last_usage=last_real_main_usage(calls)) == 300
    assert last_real_main_usage(calls[:3]) is None


@pytest.mark.parametrize("ttl,gap,cold", [(300, 301, True), (1800, 1200, False),
                                          (1800, 2400, True), (None, 36_000, False),
                                          (None, None, False)])
def test_signal_obeys_route_but_static_changes_still_count(ttl, gap, cold):
    signal = CacheSignal(ttl_seconds=ttl, gap_s=gap, resumed=True, p_cold=1.0)
    assert signal.cold_now is cold
    assert signal.to_dict()["ttl_seconds"] == ttl
    assert signal.p_cold == 1.0  # measured misses still affect scheduling
    assert CacheSignal(ttl_seconds=ttl, gap_s=gap, static_changed=True).cold_reason == "static_changed"
