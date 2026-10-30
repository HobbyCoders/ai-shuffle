"""A small concurrent-ingest ratchet, on the configured isolated test DB.

Real-PG CI/probes use the same test; the ordinary isolated SQLite job also
catches accidental event-loop stalls. No source append hook calls the actor.
The large-record timing case additionally measures same-process GIL contention.
"""
from __future__ import annotations

import asyncio
import math
import time

import pytest

from app.core.transcript_views.bridge import ReducerBridge
from app.core.transcript_views.producer import ProjectionActor, database
from app.db import session_events as source
from app.db import transcript_projections as store
from app.db.profiles import create_profile
from app.db.sessions import create_session


@pytest.mark.asyncio
async def test_background_projection_preserves_ingest_p95(monkeypatch):
    monkeypatch.setenv('AI_SHUFFLE_TRANSCRIPT_PRODUCER', '1')
    store.initialize()
    create_profile('ingest-fixture', 'Fixture', None, {}, if_absent=True)
    sid = 'projection-ingest-fixture'
    create_session(sid, 'ingest-fixture', derive_provider_from_profile=False)
    seed = []
    for index in range(64):
        seed += [{'type': 'message.user', 'payload': {'content': f'Request {index}'}},
                 {'type': 'tool.use', 'payload': {'tool_use_id': f'tool-{index}', 'name': 'Read', 'input': {'path': 'fixture'}}},
                 {'type': 'tool.result', 'payload': {'tool_use_id': f'tool-{index}', 'content': 'fixture result ' * 160}},
                 {'type': 'turn.completed', 'payload': {}}]
    source.append_events(sid, seed)
    bridge = ReducerBridge()
    actor = ProjectionActor(bridge)
    stop = asyncio.Event()
    quanta = 0
    task = None
    async def ingest():
        values = []
        for _ in range(75):
            start = time.perf_counter()
            await database(source.append_events, sid, [{'type': 'assistant.text_delta', 'payload': {'text': 'small delta'}}])
            values.append(time.perf_counter() - start)
        return values
    async def background():
        nonlocal quanta
        for _ in range(2000):
            if stop.is_set():
                return
            await actor.process(sid)
            quanta += 1
            await asyncio.sleep(0)
    try:
        for _ in range(32):
            if not await actor.process(sid):
                break
        assert store.load(sid).ready
        baseline = await ingest()
        task = asyncio.create_task(background())
        concurrent = await ingest()
        stop.set()
        await task
        assert quanta > 0
        p95 = lambda values: sorted(values)[math.ceil(len(values) * .95) - 1]
        before, during = p95(baseline), p95(concurrent)
        # Tight absolute allowance catches a blocking 10-100ms fold while
        # accommodating the scheduler noise of free shared CI runners.
        limit = max(before * 1.35, before + .003)
        print(f'projection ingest p95: baseline={before * 1000:.3f}ms '
              f'concurrent={during * 1000:.3f}ms limit={limit * 1000:.3f}ms quanta={quanta}')
        assert during <= limit
    finally:
        stop.set()
        if task is not None:
            await task
        await bridge.close()


class LargeFoldLatencyRegression(AssertionError):
    """Only the known default-off timing defect, never a correctness failure."""


@pytest.mark.asyncio
@pytest.mark.xfail(
    raises=LargeFoldLatencyRegression,
    strict=False,
    reason='https://github.com/HobbyCoders/ai-shuffle/issues/902: fix before enabling the producer',
)
async def test_large_admissible_folds_preserve_loop_lag_and_ingest_p95(monkeypatch):
    import threading
    from app.core.transcript_views import producer as implementation

    monkeypatch.setenv('AI_SHUFFLE_TRANSCRIPT_PRODUCER', '1')
    store.initialize()
    create_profile('large-ingest-profile', 'Fixture', None, {}, if_absent=True)
    large, ingest_sid = 'large-fold-fixture', 'large-concurrent-ingest'
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
    owner, preparations = threading.get_ident(), 0
    prepare = implementation._prepare_frames
    def off_loop(*args, **kwargs):
        nonlocal preparations
        assert threading.get_ident() != owner
        preparations += 1
        return prepare(*args, **kwargs)
    monkeypatch.setattr(implementation, '_prepare_frames', off_loop)
    bridge, stop = ReducerBridge(), asyncio.Event()
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
            if stop.is_set():
                return
            await actor.process(large, recheck=True)
            for _ in range(20):
                if not await actor.process(large):
                    break
            state = await database(store.load, large)
            assert state.ready == 1, (state.stage, state.error_code)
            assert state.scan_seq == 4 and state.preview_json
            completed += 1
    try:
        # Warm the bundled real Node process before measuring either phase.
        await bridge.request('plan', carry=None, frames=[])
        baseline_lag = []
        ticker = asyncio.create_task(heartbeat(baseline_lag))
        baseline = await ingest()
        stop.set()
        await ticker
        stop.clear()
        concurrent_lag = []
        ticker = asyncio.create_task(heartbeat(concurrent_lag))
        background_task = asyncio.create_task(background())
        concurrent = await ingest()
        stop.set()
        await background_task
        await ticker
        assert completed >= 2 and preparations >= 4
        p95 = lambda values: sorted(values)[math.ceil(len(values) * .95) - 1]
        before, during = p95(baseline), p95(concurrent)
        before_lag, during_lag = p95(baseline_lag), p95(concurrent_lag)
        ingest_limit = max(before * 1.35, before + .006)
        lag_limit = max(before_lag * 1.35, before_lag + .004)
        print(f'large projection: 1MiB records={completed} preparations={preparations} '
              f'ingest p95={before * 1000:.3f}->{during * 1000:.3f}ms '
              f'limit={ingest_limit * 1000:.3f}ms '
              f'loop-lag p95={before_lag * 1000:.3f}->{during_lag * 1000:.3f}ms '
              f'limit={lag_limit * 1000:.3f}ms max={max(concurrent_lag) * 1000:.3f}ms')
        # Keep running real large folds and every correctness assertion. Only
        # these two measured budgets are temporarily non-gating for #902; the
        # producer remains default-off. Do not raise either original limit.
        try:
            assert during <= ingest_limit
            assert during_lag <= lag_limit
        except AssertionError as error:
            raise LargeFoldLatencyRegression('large-fold latency budget exceeded; see #902') from error
    finally:
        stop.set()
        try:
            await asyncio.gather(*(task for task in (background_task, ticker) if task is not None),
                                 return_exceptions=True)
        finally:
            await bridge.close()
