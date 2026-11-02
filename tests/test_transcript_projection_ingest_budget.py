"""Concurrent-ingest ratchets on the configured isolated test DB.

Real-PG CI/probes and the isolated SQLite job use the same tests. Ingest p95 is
printed telemetry, not a shared-runner deadline. The small case asserts median
maximum heartbeat gaps during repeated real folds of a separate, stable session:
a blocking fold stalls the loop repeatedly; isolated scheduling/DB outliers do
not. The large-record case additionally proves preparation/ingest independence
by order. No source append hook calls the actor.
"""
from __future__ import annotations

import asyncio
import math
import os
import time
from statistics import median
from uuid import uuid4

import pytest

from app.core.transcript_views.bridge import ReducerBridge
from app.core.transcript_views.producer import ProjectionActor, database
from app.db import session_events as source
from app.db import transcript_projections as store
from app.db.profiles import create_profile
from app.db.sessions import create_session


def _held_prepare_frames(owner, entered, release, *args, **kwargs):
    """Hold the real CPU operation in its child until live ingestion finishes."""
    from app.core.transcript_views.producer import _prepare_frames

    assert os.getpid() != owner, "large preparation must not share the serving process/GIL"
    entered.touch()
    deadline = time.monotonic() + 30  # Broken-test guard, not a performance budget.
    while not release.exists():
        assert time.monotonic() < deadline, "ingestion never released preparation"
        time.sleep(.005)
    return _prepare_frames(*args, **kwargs)


async def _measure_loop_gap(operation):
    """Include stalls anywhere in an operation, even before its first await."""
    loop = asyncio.get_running_loop()
    last = loop.time()
    longest = 0.0

    def tick():
        nonlocal last, longest, handle
        now = loop.time()
        longest = max(longest, now - last)
        last = now
        handle = loop.call_later(.001, tick)

    handle = loop.call_soon(tick)
    try:
        result = await operation
    finally:
        longest = max(longest, loop.time() - last)
        handle.cancel()
    return result, longest


def _assert_nonblocking_folds(baseline_gaps, fold_gaps):
    before, during = median(baseline_gaps), median(fold_gaps)
    # A synchronous 10-100ms fold spoils each fold's maximum heartbeat gap.
    # Its median catches repeated blocking, not a handful of runner/DB outliers;
    # retain the original 1.35x/+3ms allowance rather than widening the budget.
    limit = max(before * 1.35, before + .003)
    print(f'projection loop gap median: baseline={before * 1000:.3f}ms '
          f'fold={during * 1000:.3f}ms limit={limit * 1000:.3f}ms '
          f'samples={len(fold_gaps)}')
    assert during <= limit


@pytest.mark.parametrize('outlier', [.00730, .007460238, .0176, .05008375, .11785])
def test_fold_budget_tolerates_isolated_runner_noise(outlier):
    _assert_nonblocking_folds([.001] * 75, [.0015] * 28 + [outlier] * 2)


@pytest.mark.parametrize('stall', [.010, .050, .100])
def test_fold_budget_rejects_repeated_blocking(stall):
    with pytest.raises(AssertionError):
        _assert_nonblocking_folds([.001] * 75, [.0015] * 2 + [stall] * 28)


@pytest.mark.asyncio
async def test_background_projection_preserves_ingest_p95(monkeypatch):
    monkeypatch.setenv('AI_SHUFFLE_TRANSCRIPT_PRODUCER', '1')
    store.initialize()
    create_profile('ingest-fixture', 'Fixture', None, {}, if_absent=True)
    sid = f'projection-ingest-fixture-{uuid4()}'
    ingest_sid = f'concurrent-ingest-fixture-{uuid4()}'
    for session_id in (sid, ingest_sid):
        create_session(session_id, 'ingest-fixture', derive_provider_from_profile=False)
    seed = []
    for index in range(64):
        seed += [{'type': 'message.user', 'payload': {'content': f'Request {index}'}},
                 {'type': 'tool.use', 'payload': {'tool_use_id': f'tool-{index}', 'name': 'Read', 'input': {'path': 'fixture'}}},
                 {'type': 'tool.result', 'payload': {'tool_use_id': f'tool-{index}', 'content': 'fixture result ' * 160}},
                 {'type': 'turn.completed', 'payload': {}}]
    source.append_events(sid, seed)
    bridge = ReducerBridge()
    actor = ProjectionActor(bridge)
    task = None
    fold_gaps = []
    measuring = False
    fold = actor._fold

    async def measured_fold(*args, **kwargs):
        if not measuring:
            return await fold(*args, **kwargs)
        result, gap = await _measure_loop_gap(fold(*args, **kwargs))
        fold_gaps.append(gap)
        return result

    monkeypatch.setattr(actor, '_fold', measured_fold)

    async def ingest():
        values, gaps = [], []
        for _ in range(75):
            start = time.perf_counter()
            _, gap = await _measure_loop_gap(database(source.append_events, ingest_sid, [
                {'type': 'assistant.text_delta', 'payload': {'text': 'small delta'}}]))
            values.append(time.perf_counter() - start)
            gaps.append(gap)
        return values, gaps

    async def background():
        more, completed = False, 0
        for _ in range(75):
            # Re-fold a stable session: appending to the actor's own source can
            # keep it chasing watermarks without ever reaching an actual fold.
            more = await actor.process(sid, recheck=not more)
            completed += not more
            await asyncio.sleep(0)
        assert completed >= 5, 'must exercise repeated complete folds'
        print(f'projection ingest quanta=75 completed={completed}')

    try:
        for _ in range(32):
            if not await actor.process(sid):
                break
        assert store.load(sid).ready
        baseline, baseline_gaps = await ingest()
        measuring = True
        task = asyncio.create_task(background())
        concurrent, _ = await ingest()
        await task
        assert len(fold_gaps) >= 5
        p95 = lambda values: sorted(values)[math.ceil(len(values) * .95) - 1]
        print(f'projection ingest p95: baseline={p95(baseline) * 1000:.3f}ms '
              f'concurrent={p95(concurrent) * 1000:.3f}ms')
        _assert_nonblocking_folds(baseline_gaps, fold_gaps)
    finally:
        try:
            if task is not None:
                await task
        finally:
            await bridge.close()


@pytest.mark.asyncio
async def test_large_admissible_folds_preserve_loop_lag_and_ingest_p95(monkeypatch, tmp_path):
    from app.core.transcript_views import producer as implementation

    monkeypatch.setenv('AI_SHUFFLE_TRANSCRIPT_PRODUCER', '1')
    store.initialize()
    create_profile('large-ingest-profile', 'Fixture', None, {}, if_absent=True)
    large, ingest_sid = f'large-fold-fixture-{uuid4()}', f'large-concurrent-ingest-{uuid4()}'
    for sid in (large, ingest_sid):
        create_session(sid, 'large-ingest-profile', derive_provider_from_profile=False)
    source.append_events(large, [
        {'type': 'message.user', 'payload': {'content': 'Large admissible result'}},
        {'type': 'tool.use', 'payload': {'tool_use_id': 'large-tool', 'name': 'Read', 'input': {'path': 'fixture'}}},
        {'type': 'tool.result', 'payload': {'tool_use_id': 'large-tool', 'content': 'x' * (1024 * 1024)}},
        {'type': 'turn.completed', 'payload': {}}
    ])
    # Deterministically cover the cause, in addition to timing: the actual
    # restore/re-evaluate/append/frame construction may never run on this loop.
    owner, preparations = os.getpid(), 0
    bridge, stop = ReducerBridge(), asyncio.Event()
    entered, release = tmp_path / "preparation-entered", tmp_path / "preparation-release"
    hold_preparation, held = False, False
    operations = set()
    prepare = bridge.prepare
    async def off_process(call, *args, **kwargs):
        nonlocal preparations, held
        sequence = bridge._preparation._sequence
        if call is implementation._prepare_frames and hold_preparation and not held:
            held = True
            result = await prepare(_held_prepare_frames, owner, entered, release, *args, **kwargs)
        else:
            result = await prepare(call, *args, **kwargs)
        # Cover every expensive boundary, including replay restore, input lookup,
        # checkpointing and IPC JSON encoding/decoding, not only frame building.
        assert bridge._preparation._sequence == sequence + 1
        assert bridge._preparation._process.pid != owner
        operations.add(call.__name__)
        if call is implementation._prepare_frames:
            preparations += 1
        return result
    monkeypatch.setattr(bridge, 'prepare', off_process)
    actor = ProjectionActor(bridge)
    ticker, background_task = None, None
    async def heartbeat(values):
        loop = asyncio.get_running_loop()
        while not stop.is_set():
            start = loop.time()
            await asyncio.sleep(.001)
            values.append(max(0.0, loop.time() - start - .001))
    async def ingest():
        values = []
        for _ in range(150):
            start = time.perf_counter()
            await database(source.append_events, ingest_sid, [
                {'type': 'assistant.text_delta', 'payload': {'text': 'small delta'}}])
            values.append(time.perf_counter() - start)
            await asyncio.sleep(.001)
        return values
    completed = 0
    async def background():
        nonlocal completed
        for _ in range(8):
            await actor.process(large, recheck=True)
            for _ in range(20):
                if not await actor.process(large):
                    break
            state = await database(store.load, large)
            assert state.ready == 1, (state.stage, state.error_code)
            assert state.scan_seq == 4 and state.preview_json
            completed += 1
    try:
        # Warm both private runtimes through a real fold before either phase;
        # spawn/import startup is not the steady-state contention measurement.
        await bridge.request('plan', carry=None, frames=[])
        for _ in range(20):
            if not await actor.process(large):
                break
        assert (await database(store.load, large)).ready
        preparations = 0
        baseline_lag = []
        ticker = asyncio.create_task(heartbeat(baseline_lag))
        baseline = await ingest()
        stop.set()
        await ticker
        stop.clear()
        hold_preparation = True
        operations.clear()
        background_task = asyncio.create_task(background())

        async def until_preparation_entered():
            while not entered.exists():
                if background_task.done():
                    await background_task  # Surface a failed fold, not a timeout.
                    pytest.fail("fold never reached off-process preparation")
                await asyncio.sleep(.001)

        await asyncio.wait_for(until_preparation_entered(), 30)
        concurrent_lag = []
        ticker = asyncio.create_task(heartbeat(concurrent_lag))
        # A read/append that waits on preparation cannot finish: only this test
        # releases the child, after all 150 real appends and heartbeats complete.
        # The 30s bound detects that deadlock; it is not a latency allowance.
        concurrent = await asyncio.wait_for(ingest(), 30)
        assert len(concurrent) == 150 and concurrent_lag
        assert await database(source.get_max_seq, ingest_sid) == 300
        assert held and not release.exists() and not background_task.done()
        assert completed == 0 and preparations == 0
        stop.set()
        await ticker
        release.touch()
        await background_task
        assert completed == 8 and preparations >= 8
        assert {'restore', 'input_lookup_keys', '_prepare_frames', '_checkpoint_tail',
                '_encode_packet', 'loads'} <= operations
        # Keep same-run measurements for diagnosis, but do not turn unrelated
        # runner scheduling/GIL noise into a product failure (#794).
        p95 = lambda values: sorted(values)[math.ceil(len(values) * .95) - 1]
        before, during = p95(baseline), p95(concurrent)
        before_lag, during_lag = p95(baseline_lag), p95(concurrent_lag)
        print(f'large projection: 1MiB records={completed} preparations={preparations} '
              f'ingest p95={before * 1000:.3f}->{during * 1000:.3f}ms '
              f'loop-lag p95={before_lag * 1000:.3f}->{during_lag * 1000:.3f}ms '
              f'max={max(concurrent_lag) * 1000:.3f}ms; 150 appends while child held')
    finally:
        release.touch()
        stop.set()
        try:
            await asyncio.gather(*(task for task in (background_task, ticker) if task is not None),
                                 return_exceptions=True)
        finally:
            await bridge.close()
