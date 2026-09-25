"""NativeLoop's per-call cache-probe fields and auxiliary (tool-side) usage.

Both are instrumentation: whatever a probe helper throws must cost a field,
never a call, and the fingerprint rotation must stay intact."""

from __future__ import annotations

import pytest

import fluid.engine.native as native_mod
from fluid.engine.native import NativeLoop
from fluid.events import Usage
from fluid.providers.base import ProviderCaps
from fluid.store.base import MemoryStore


class _Provider:
    name = "anthropic"
    caps = ProviderCaps(explicit_cache_breakpoints=True, max_cache_breakpoints=4)

    async def stream(self, request):  # pragma: no cover - never streamed here
        if False:
            yield None


def _loop() -> NativeLoop:
    return NativeLoop(provider=_Provider(), store=MemoryStore(), model="m", session_id="s")


def _fp(stamp: str | None, **segments: str) -> dict[str, str]:
    fp = {"system": "sys-1", "tools": "tools-1", "m1": "a", **segments}
    if stamp is not None:
        fp["_requested_at"] = stamp
    return fp


def _boom(*_a, **_k):
    raise RuntimeError("probe helper exploded")


# ─── _cache_probe_fields ─────────────────────────────────────────────────────


def test_no_fingerprint_reports_only_the_decision_and_consumes_rewrites() -> None:
    loop = _loop()
    loop._history_rewrites = [{"rung": "trim"}]

    out = loop._cache_probe_fields({"input_tokens": 10, "cache_read_input_tokens": 5})

    assert set(out) == {"compaction_decision", "cache_read_report"}
    assert "prefix_hashes" not in out
    assert loop._history_rewrites == []
    assert loop._prev_prefix_fingerprint is None


def test_fingerprint_rotates_and_interval_is_measured() -> None:
    loop = _loop()
    loop._prev_prefix_fingerprint = _fp("100.0")
    current = _fp("102.5")
    loop._prefix_fingerprint = current
    loop._pending_system_text = "system text"

    out = loop._cache_probe_fields({"input_tokens": 10})

    assert out["prefix_hashes"] is current
    assert out["request_interval_seconds"] == pytest.approx(2.5)
    assert loop._prefix_fingerprint is None
    assert loop._prev_prefix_fingerprint is current
    assert (loop._prev_system_text, loop._pending_system_text) == ("system text", None)


@pytest.mark.parametrize("previous, current", [
    ("not-a-time", "5"),
    ("5", None),
    (None, "5"),
])
def test_malformed_request_times_drop_only_the_interval(previous, current) -> None:
    loop = _loop()
    loop._prev_prefix_fingerprint = _fp(previous)
    loop._prefix_fingerprint = _fp(current)

    out = loop._cache_probe_fields({})

    assert "request_interval_seconds" not in out
    assert "prefix_hashes" in out


def test_clock_skew_never_reports_a_negative_interval() -> None:
    loop = _loop()
    loop._prev_prefix_fingerprint = _fp("200")
    loop._prefix_fingerprint = _fp("150")

    assert loop._cache_probe_fields({})["request_interval_seconds"] == 0.0


@pytest.mark.parametrize("helper", ["cache_read_report", "system_change_detail", "diagnose_cache_miss"])
def test_a_failing_probe_helper_costs_its_field_not_the_call(monkeypatch, helper) -> None:
    loop = _loop()
    loop._prev_prefix_fingerprint = _fp("1")
    current = _fp("2", m1="changed")
    loop._prefix_fingerprint = current
    loop._unexplained_misses = 3
    monkeypatch.setattr(native_mod, helper, _boom)

    out = loop._cache_probe_fields({"input_tokens": 90_000, "cache_read_input_tokens": 0})

    assert out["prefix_hashes"] is current
    assert loop._prev_prefix_fingerprint is current  # rotation still happened
    if helper == "cache_read_report":
        assert "cache_read_report" not in out
    if helper == "diagnose_cache_miss":
        assert "cache_miss" not in out
        assert loop._unexplained_misses == 0


def test_system_detail_reaches_the_diagnosis(monkeypatch) -> None:
    loop = _loop()
    loop._prev_prefix_fingerprint = _fp("1")
    loop._prefix_fingerprint = _fp("2", system="sys-2")
    seen: dict = {}
    monkeypatch.setattr(native_mod, "system_change_detail", lambda *a, **k: {"diff": "x"})

    def diagnose(**kwargs):
        seen.update(kwargs)
        return None

    monkeypatch.setattr(native_mod, "diagnose_cache_miss", diagnose)
    loop._cache_probe_fields({
        "input_tokens": 100, "cache_creation_input_tokens": 50, "cache_read_input_tokens": 7,
    })

    assert seen["system_detail"] == {"diff": "x"}
    # Fresh prompt tokens are uncached input plus cache writes.
    assert seen["input_tokens"] == 150 and seen["cache_read_tokens"] == 7


def test_sticky_key_drop_is_attached_to_a_settings_change_once(monkeypatch) -> None:
    loop = _loop()
    monkeypatch.setattr(
        native_mod, "diagnose_cache_miss",
        lambda **_k: {"reason": "request_settings_changed", "changed": "request"},
    )
    loop._sticky_drop_unreported = True

    loop._prefix_fingerprint = _fp("1")
    first = loop._cache_probe_fields({})
    loop._prefix_fingerprint = _fp("2")
    second = loop._cache_probe_fields({})

    assert first["cache_miss"]["sticky_key_dropped"] is True
    assert "sticky_key_dropped" not in second["cache_miss"]
    assert loop._sticky_drop_unreported is False


def test_unexplained_misses_count_up_and_explained_ones_reset(monkeypatch) -> None:
    loop = _loop()
    verdicts = iter([True, True, False])
    monkeypatch.setattr(native_mod, "diagnose_cache_miss", lambda **_k: {"reason": "x"})
    monkeypatch.setattr(native_mod, "is_unexplained_miss", lambda _d: next(verdicts))

    streak = []
    for stamp in ("1", "2", "3"):
        loop._prefix_fingerprint = _fp(stamp)
        out = loop._cache_probe_fields({})
        streak.append(out["cache_miss"].get("consecutive_unexplained"))

    assert streak == [1, 2, None]
    assert loop._unexplained_misses == 0


# ─── record_auxiliary_usage ──────────────────────────────────────────────────


async def _live_loop() -> NativeLoop:
    """Tools report auxiliary usage mid-turn, when the session exists."""
    loop = _loop()
    await loop._ensure_session()
    return loop


async def test_auxiliary_usage_is_recorded_and_marked_cost_incomplete() -> None:
    loop = await _live_loop()
    usage = {
        "input_tokens": 120, "output_tokens": 30,
        "cache_read_input_tokens": 7, "cache_creation_input_tokens": 3,
        "provider_response": {"id": "img-1"}, "streamed": True,
    }

    await loop.record_auxiliary_usage(usage, model="gpt-image-1", turn_id="t1", source="ImageGen")

    event = [e for e in loop.bus.log("s") if isinstance(e, Usage)][-1]
    assert (event.input_tokens, event.output_tokens) == (120, 30)
    assert (event.cache_read_tokens, event.cache_creation_tokens) == (7, 3)
    assert event.turn_id == "t1"
    assert event.raw["model"] == "gpt-image-1"
    assert event.raw["auxiliary_source"] == "ImageGen"
    assert event.raw["cost_incomplete"] is True
    assert event.raw["provider_response"] == {"id": "img-1"}
    assert "model" not in usage  # the caller's dict is not mutated
    # Only numeric counters join the turn totals; bools and snapshots do not.
    assert loop._turn_usage == {
        "input_tokens": 120, "output_tokens": 30,
        "cache_read_input_tokens": 7, "cache_creation_input_tokens": 3,
    }


async def test_auxiliary_usage_sums_across_calls() -> None:
    loop = await _live_loop()
    loop._turn_usage = {"input_tokens": 1000, "cost_usd": 0.5}

    await loop.record_auxiliary_usage({"input_tokens": 10, "cost_usd": 0.25},
                                      model="m", turn_id=None, source="a")
    await loop.record_auxiliary_usage({"input_tokens": 5}, model="m", turn_id=None, source="b")

    assert loop._turn_usage == {"input_tokens": 1015, "cost_usd": 0.75}
    events = [e for e in loop.bus.log("s") if isinstance(e, Usage)]
    assert [e.raw["auxiliary_source"] for e in events] == ["a", "b"]


async def test_empty_auxiliary_usage_records_nothing() -> None:
    loop = _loop()
    await loop.record_auxiliary_usage({}, model="m", turn_id="t", source="x")
    assert not [e for e in loop.bus.log("s") if isinstance(e, Usage)]
    assert loop._turn_usage == {}


async def test_missing_counts_default_to_zero() -> None:
    loop = await _live_loop()
    await loop.record_auxiliary_usage({"images": 2}, model="m", turn_id=None, source="x")
    event = [e for e in loop.bus.log("s") if isinstance(e, Usage)][-1]
    assert (event.input_tokens, event.output_tokens,
            event.cache_read_tokens, event.cache_creation_tokens) == (0, 0, 0, 0)
    assert event.raw["images"] == 2
