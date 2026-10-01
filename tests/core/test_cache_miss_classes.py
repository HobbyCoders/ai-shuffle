"""Every prompt-cache miss fluid's probe reports gets one of seven classes.

fluid put a ``cache_miss`` diagnosis on each missing call's Usage event and
raised ``CacheMissAlert`` on a streak, and nothing in the app read either.
The adapter now classifies each miss, labels the call's usage record with the
class and counts the classes in its context-usage report.
"""

import logging

import pytest

from app.core.provider import cache_miss_classes as cmc
from app.core.provider.harness_adapter import HarnessAdapter
from fluid.events import CacheMissAlert, Usage

_HASHES = {"system": "a" * 16, "tools": "b" * 16, "m1": "c" * 16}


def _usage(raw: dict, *, cache_read: int = 0) -> Usage:
    return Usage(
        session_id="engine-s", turn_id="t1",
        input_tokens=60_000, output_tokens=10,
        cache_read_tokens=cache_read, cache_creation_tokens=0,
        raw={"prefix_hashes": dict(_HASHES), **raw},
    )


def _report(verdict: str) -> dict:
    return {"cache_read_report": {"verdict": verdict}}


# One synthetic miss per class, shaped like the probe's Usage.raw fields.
SYNTHETIC_MISSES = {
    cmc.APPEND_GROWTH: {
        # Prefix reused, the fresh part is the new turn: the probe finds no
        # lost reuse and records no diagnosis, only a partial-hit verdict.
        **_report("partial_hit"),
    },
    cmc.PLANNED_REBUILD: {
        **_report("no_cache_read"),
        "cache_miss": {
            "reason": "history_rewritten", "stable_through": "m1",
            "history_rewritten": [{"tier": 2, "reason": "trigger"}],
        },
    },
    cmc.HARNESS_UPGRADE: {
        **_report("no_cache_read"),
        "cache_miss": {"reason": "fingerprint_version_changed"},
    },
    cmc.PROVIDER_FALLBACK: {
        **_report("no_cache_read"),
        "request_interval_seconds": 12.0,
        "cache_miss": {
            "reason": "prefix_grew_only", "full_coverage": True,
            "reuse_shortfall_tokens_estimated": 50_000,
        },
    },
    cmc.COMPACTION_FOLLOWON: {
        # The same grown-prefix miss, two calls after a compaction rewrite:
        # the provider is still rebuilding the rewritten tail.
        **_report("no_cache_read"),
        "request_interval_seconds": 12.0,
        "calls_since_rewrite": 2,
        "cache_miss": {
            "reason": "prefix_grew_only", "full_coverage": True,
            "reuse_shortfall_tokens_estimated": 50_000,
        },
    },
    cmc.IDLE_PAST_TTL: {
        **_report("no_cache_read"),
        "request_interval_seconds": 1_800.0,
        "cache_miss": {
            "reason": "prefix_grew_only", "full_coverage": True,
            "reuse_shortfall_tokens_estimated": 50_000,
        },
    },
    cmc.HARNESS_MUTATION: {
        **_report("no_cache_read"),
        "cache_miss": {
            "reason": "prefix_changed", "changed": "system",
            "system_diff": {"paragraph_index": 3, "section": "environment"},
        },
    },
}


def test_the_synthetic_set_covers_all_seven_classes():
    assert set(SYNTHETIC_MISSES) == set(cmc.MISS_CLASSES)
    assert len(cmc.MISS_CLASSES) == 7


@pytest.mark.parametrize("expected", cmc.MISS_CLASSES)
def test_each_miss_class_is_classified_by_the_subscriber(expected):
    adapter = HarnessAdapter()
    translated = adapter._translate(_usage(SYNTHETIC_MISSES[expected]))

    assert translated[0].usage["cache_miss_class"] == expected
    assert adapter._cache_miss_counts == {expected: 1}


def test_classification_leaves_the_fluid_event_untouched():
    event = _usage(SYNTHETIC_MISSES[cmc.HARNESS_MUTATION])
    HarnessAdapter()._translate(event)
    assert "cache_miss_class" not in event.raw
    assert "class" not in event.raw["cache_miss"]


@pytest.mark.asyncio
async def test_context_usage_reports_misses_by_class():
    class _Engine:
        async def get_context_usage(self):
            return {"totalTokens": 1_000, "categories": []}

    adapter = HarnessAdapter()
    adapter._engine = _Engine()
    assert "cacheMisses" not in await adapter.get_context_usage()

    for expected in cmc.MISS_CLASSES:
        adapter._translate(_usage(SYNTHETIC_MISSES[expected]))
    adapter._translate(_usage(SYNTHETIC_MISSES[cmc.HARNESS_MUTATION]))
    # A healthy hit is not a miss and is not counted.
    adapter._translate(_usage(_report("full_hit"), cache_read=59_000))

    report = (await adapter.get_context_usage())["cacheMisses"]
    assert report["byClass"] == {
        cmc.APPEND_GROWTH: 1, cmc.PLANNED_REBUILD: 1, cmc.HARNESS_UPGRADE: 1,
        cmc.PROVIDER_FALLBACK: 1, cmc.COMPACTION_FOLLOWON: 1,
        cmc.IDLE_PAST_TTL: 1, cmc.HARNESS_MUTATION: 2,
    }
    assert report["total"] == 8
    assert report["unexpected"] == 2


def test_the_alert_after_a_miss_is_logged_with_its_class(caplog):
    adapter = HarnessAdapter()
    adapter._translate(_usage(SYNTHETIC_MISSES[cmc.PROVIDER_FALLBACK]))
    alert = CacheMissAlert(
        session_id="engine-s", turn_id="t1", consecutive=2,
        reason="prefix_grew_only", input_tokens=60_000, cache_read_tokens=0,
        provider="openai_responses", model="gpt-6-astra", cache_key="conv-1",
        note="every shared checkpoint matched",
    )
    with caplog.at_level(logging.WARNING, logger="app.core.provider.harness_adapter"):
        assert adapter._translate(alert) == []
    assert "class=provider_fallback" in caplog.text
    assert "consecutive=2" in caplog.text


# ─── The pure classifier ────────────────────────────────────────────────────


def test_a_routing_header_change_is_a_mutation_but_a_settings_switch_is_planned():
    headers = {"reason": "request_settings_changed", "routing_headers_changed": True}
    settings = {"reason": "request_settings_changed", "routing_headers_changed": False}
    assert cmc.classify_cache_miss(headers) == cmc.HARNESS_MUTATION
    assert cmc.classify_cache_miss(settings) == cmc.PLANNED_REBUILD


def test_headers_that_moved_with_a_provider_switch_are_a_planned_rebuild():
    # Codex backend to the public API: the endpoint changed and the Codex
    # headers went away in the same request. The user switched; nothing
    # changed behind their back.
    switch = {"reason": "request_settings_changed",
              "routing_headers_changed": True, "other_settings_changed": True}
    assert cmc.classify_cache_miss(switch) == cmc.PLANNED_REBUILD
    alone = {"reason": "request_settings_changed",
             "routing_headers_changed": True, "other_settings_changed": False}
    assert cmc.classify_cache_miss(alone) == cmc.HARNESS_MUTATION


def test_a_system_change_is_a_mutation_even_after_a_compaction():
    diagnosis = {"reason": "prefix_changed", "changed": "tools",
                 "history_rewritten": [{"tier": 1}]}
    assert cmc.classify_cache_miss(diagnosis) == cmc.HARNESS_MUTATION
    history = {"reason": "prefix_changed", "changed": "m8",
               "history_rewritten": [{"tier": 1}]}
    assert cmc.classify_cache_miss(history) == cmc.PLANNED_REBUILD
    unannounced = {"reason": "prefix_changed", "changed": "m8"}
    assert cmc.classify_cache_miss(unannounced) == cmc.HARNESS_MUTATION


def test_first_request_and_unknown_reasons():
    assert cmc.classify_cache_miss({"reason": "no_previous_request"}) == cmc.PLANNED_REBUILD
    # An unknown reason counts as unexpected so someone looks at it.
    assert cmc.classify_cache_miss({"reason": "something_new"}) == cmc.HARNESS_MUTATION


def test_the_ttl_boundary_and_a_missing_interval():
    miss = {"reason": "prefix_unchanged"}
    assert cmc.classify_cache_miss(miss, request_interval_seconds=300) == cmc.PROVIDER_FALLBACK
    assert cmc.classify_cache_miss(miss, request_interval_seconds=301) == cmc.IDLE_PAST_TTL
    assert cmc.classify_cache_miss(miss) == cmc.PROVIDER_FALLBACK
    assert cmc.classify_cache_miss(
        miss, request_interval_seconds=120, ttl_seconds=60
    ) == cmc.IDLE_PAST_TTL


def test_calls_that_are_not_misses_have_no_class():
    assert cmc.usage_miss_class({"prefix_hashes": _HASHES, **_report("full_hit")}) is None
    assert cmc.usage_miss_class({"prefix_hashes": _HASHES, **_report("small_prompt")}) is None
    # The probe did not fingerprint this call (probe off, delegated leg).
    assert cmc.usage_miss_class(_report("no_cache_read")) is None
    assert cmc.usage_miss_class({}) is None


def test_a_dropped_sticky_key_is_a_provider_fallback():
    # OpenRouter's permanent key drop changes the request; the probe marks
    # the diagnosis so the miss is not filed as a planned settings switch.
    dropped = {"reason": "request_settings_changed", "routing_headers_changed": False,
               "other_settings_changed": True, "sticky_key_dropped": True}
    assert cmc.classify_cache_miss(dropped) == cmc.PROVIDER_FALLBACK


def test_the_alert_is_logged_with_the_class_the_engine_stamped(caplog):
    adapter = HarnessAdapter()
    adapter._translate(_usage(SYNTHETIC_MISSES[cmc.HARNESS_MUTATION]))
    alert = CacheMissAlert(
        session_id="engine-s", turn_id="t1", consecutive=1,
        reason="sticky_key_dropped", provider="openrouter", model="glm",
        miss_class=cmc.PROVIDER_FALLBACK,
    )
    with caplog.at_level(logging.WARNING, logger="app.core.provider.harness_adapter"):
        adapter._translate(alert)
    assert "class=provider_fallback" in caplog.text


@pytest.mark.asyncio
async def test_cache_miss_counts_survive_an_adapter_reconnect(monkeypatch):
    # A reconnect builds a new adapter with empty counts. Its first capture
    # used to overwrite the persisted counts with the lower ones.
    from app.db import database

    persisted = {"captured_at": "x", "data": {"cacheMisses": {
        "byClass": {cmc.PROVIDER_FALLBACK: 4, cmc.HARNESS_MUTATION: 1},
        "total": 5, "unexpected": 1,
    }}}
    reads: list[str] = []

    def _snapshot(session_id):
        reads.append(session_id)
        return persisted

    monkeypatch.setattr(database, "get_session_context_usage_snapshot", _snapshot)

    class _Engine:
        async def get_context_usage(self):
            return {"totalTokens": 1_000, "categories": []}

    adapter = HarnessAdapter()
    adapter._engine = _Engine()
    monkeypatch.setattr(adapter, "_resolve_app_session_id", lambda: "app-s")
    adapter._translate(_usage(SYNTHETIC_MISSES[cmc.HARNESS_MUTATION]))

    report = (await adapter.get_context_usage())["cacheMisses"]
    assert report["byClass"] == {cmc.PROVIDER_FALLBACK: 4, cmc.HARNESS_MUTATION: 2}
    assert report["total"] == 6
    assert report["unexpected"] == 2
    # Read once per adapter, not on every capture.
    await adapter.get_context_usage()
    assert reads == ["app-s"]
