"""Speed follow-ups for bounded pending recovery and bulk integration."""
import asyncio
from types import SimpleNamespace

import pytest

from app.network import engine, transport
from tests.core.network.harness import join
from tests.core.network.test_pending_apply_budget import pending_semantics_clock  # noqa: F401


def test_empty_probes_do_not_spend_request_budget(monkeypatch, pending_semantics_clock):
    streams = [(f"origin{i}", 1) for i in range(6)]
    calls, wakes = [], []

    def one(data, ctx, result):
        result._pending_drains.update(streams)

    def chunk(origin, incarnation, ctx, result):
        calls.append(origin)
        if origin == "origin5":
            result.applied += 1
            return True
        return False

    monkeypatch.setattr(engine, "_apply_one", one)
    monkeypatch.setattr(engine, "_apply_pending_chunk", chunk)
    monkeypatch.setattr(engine, "_pending_kick", lambda: wakes.append(True))
    ctx = SimpleNamespace(device_id="me", identity=SimpleNamespace(incarnation=1))
    result = engine.apply_batch([{"collection": "x", "op": "upsert"}], ctx=ctx)
    assert calls == [origin for origin, _ in streams]
    assert result.applied == 1
    assert not result._pending_more and not wakes


async def test_catchup_exchanges_throttled_and_final_empty_pass_flushes(monkeypatch):
    # A final empty probe follows a full last chunk; its prior progress still
    # needs an exchange even though the final pass itself applies nothing.
    ticks = iter([0.0, 0.1, 0.9, 1.0, 1.1, 1.2])
    results = iter([engine.ApplyResult(applied=64, _pending_more=True) for _ in range(5)]
                   + [engine.ApplyResult()])
    kicks, delays = [], []
    clock = [0.0]

    async def to_thread(fn):
        clock[0] = next(ticks)
        return next(results)

    async def sleep(delay):
        delays.append(delay)
        if len(delays) == 6:
            raise asyncio.CancelledError

    monkeypatch.setattr(transport, "_pending_wake", None)
    monkeypatch.setattr(transport, "time", SimpleNamespace(monotonic=lambda: clock[0]))
    monkeypatch.setattr(transport, "asyncio", SimpleNamespace(
        to_thread=to_thread, sleep=sleep, CancelledError=asyncio.CancelledError))
    monkeypatch.setattr(engine, "kick", lambda: kicks.append(clock[0]))
    with pytest.raises(asyncio.CancelledError):
        await transport.pending_loop()
    assert kicks == [0.0, 1.0, 1.2]
    assert delays == [0.0] * 5 + [30.0]


async def test_catchup_error_flushes_throttled_dirty_progress(monkeypatch):
    clock = [0.0]
    kicks, delays = [], []
    outcomes = iter([engine.ApplyResult(applied=64, _pending_more=True),
                     engine.ApplyResult(applied=64, _pending_more=True), RuntimeError("db offline")])

    async def to_thread(fn):
        outcome = next(outcomes)
        clock[0] += 0.1
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    async def sleep(delay):
        delays.append(delay)
        if len(delays) == 3:
            # Check before another resume can succeed and hide the delay.
            assert len(kicks) == 2
            raise asyncio.CancelledError

    monkeypatch.setattr(transport, "_pending_wake", None)
    monkeypatch.setattr(transport, "time", SimpleNamespace(monotonic=lambda: clock[0]))
    monkeypatch.setattr(transport, "asyncio", SimpleNamespace(
        to_thread=to_thread, sleep=sleep, CancelledError=asyncio.CancelledError))
    monkeypatch.setattr(engine, "kick", lambda: kicks.append(clock[0]))
    with pytest.raises(asyncio.CancelledError):
        await transport.pending_loop()
    assert len(kicks) == 2 and kicks[1] < 1.0
    assert delays[:2] == [0.0, 0.0]


@pytest.mark.parametrize("reason", ["Referenced record has not arrived yet",
                                    "Credential storage is locked; cannot store credentials"])
def test_recoverable_dependency_backoff_capped_and_resumes(workers, monkeypatch, reason):
    from app.db import network as store

    worker = workers("dependency")
    now = [0.0]
    monkeypatch.setattr(engine, "time", SimpleNamespace(monotonic=lambda: now[0]))
    blocked = [True]
    attempts = []

    def apply(cursor, env, *, ctx, result):
        attempts.append(now[0])
        if blocked[0]:
            raise engine.TransientApplyError(reason)
        store.set_watermark(cursor, "origin", 1, 2)
        return "applied"

    monkeypatch.setattr(engine, "apply_envelope", apply)
    with worker.active():
        engine.create_network("Dependency")
        with store.get_db() as conn:
            cursor = conn.cursor()
            store.set_watermark(cursor, "origin", 1, 1)
            store.stash_pending(cursor, {"origin_device_id": "origin", "origin_incarnation": 1,
                                         "origin_seq": 2, "collection": "probe"})
        ctx = engine.require_context()
        for delay in [30, 60, 60, 60, 60]:
            assert engine.resume_pending().retry == 1
            retry = ctx._pending_retries[("origin", 1)]
            assert retry.delay == delay
            now[0] = retry.until
        blocked[0] = False
        assert engine.resume_pending().applied == 1
        assert not ctx._pending_retries and len(attempts) == 6


@pytest.mark.parametrize("change", ["keyring", "member", "identity"])
def test_context_invalidation_wakes_pending_after_publish(workers, monkeypatch, change):
    a, b = workers("sender"), workers("receiver")
    with a.active():
        engine.create_network("Wake")
    join(a, b)
    wakes = []
    with b.active():
        previous = engine.require_context()
        previous._pending_retries[(a.device_id, 1)] = engine._PendingRetry(1, 600, 999999999)
        if change == "keyring":
            previous.keyring = {}
        elif change == "member":
            previous.members = {}
        else:
            previous.network_id = "previous-network"
        monkeypatch.setattr(engine, "_pending_kick", lambda: wakes.append(engine.cached_context()))
        # Snapshot reads must not invalidate published retries or emit wakes.
        engine.load_context(publish=False)
        assert not wakes and previous._pending_retries
        refreshed = engine.load_context()
        assert wakes == [refreshed] and not refreshed._pending_retries
        engine.load_context()
        assert len(wakes) == 1
