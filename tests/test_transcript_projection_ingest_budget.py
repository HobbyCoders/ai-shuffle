"""Projection/ingest independence on the isolated shim and native PostgreSQL.

Ingest p95 and timer lag are diagnostic, not shared-runner deadlines. The small
case budgets p75 maximum synchronous steps of actor.process separately for reset,
index and fold (including process overhead). It rejects blocking on every third
quantum; fewer than 25% stalled samples in a stage are not a tail-latency gate.
The large case proves preparation/ingest independence by order. Native PG also
holds a real fold commit open while requiring same-session appends to finish.
No source append hook calls the actor. No timers decide the synchronous budget.
"""
from __future__ import annotations

import asyncio
import math
import os
import threading
import time
from uuid import uuid4

# Keep module imports lightweight: the preparation child unpickles the helper
# below and must not import serving configuration (notably Windows home paths).
import pytest


def _held_prepare_frames(owner, entered, release, *args, **kwargs):
    """Hold the real CPU operation in its child until live ingestion finishes."""
    # Import the lightweight defining module, not the serving actor/config.
    # The credential-free Windows child intentionally has no USERPROFILE.
    from app.core.transcript_views.preprocess import _prepare_frames

    assert os.getpid() != owner, "large preparation must not share the serving process/GIL"
    entered.touch()
    deadline = time.monotonic() + 30  # Broken-test guard, not a performance budget.
    while not release.exists():
        assert time.monotonic() < deadline, "ingestion never released preparation"
        time.sleep(.005)
    return _prepare_frames(*args, **kwargs)


class _SynchronousSteps:
    """Time coroutine send/throw calls, not time suspended on DB/IPC/timers.

    perf_counter is high resolution on Python 3.12 Windows too. Unlike a
    heartbeat, this does not mistake Windows' coarse timer wakeups for blocking.
    Delegating the raw yields preserves Task/Future and cancellation semantics.
    """

    def __init__(self, operation):
        self.operation = operation

    def __await__(self):
        iterator = self.operation.__await__()
        advance, value, longest = iterator.send, None, 0.0
        try:
            while True:
                finished = False
                start = time.perf_counter()
                try:
                    yielded = advance(value)
                except StopIteration as complete:
                    result, finished = complete.value, True
                finally:
                    longest = max(longest, time.perf_counter() - start)
                if finished:
                    return result, longest
                try:
                    value = yield yielded
                    advance = iterator.send
                except BaseException as error:
                    advance, value = iterator.throw, error
        finally:
            iterator.close()


def _p75(values):
    return sorted(values)[math.ceil(len(values) * .75) - 1]


def _assert_nonblocking_stages(baseline_steps, stage_steps):
    before = _p75(baseline_steps)
    # This budgets synchronous loop occupancy, NOT append latency or timer lag.
    # The relative allowance plus 3ms floor leaves sub-ms plumbing headroom.
    # Per-stage p75 rejects every-third blocking while tolerating <25% outliers;
    # rarer stalls remain diagnostic, not a shared-runner tail-latency guarantee.
    limit = max(before * 1.35, before + .003)
    for stage, steps in stage_steps.items():
        during = _p75(steps)
        print(f'projection synchronous p75: stage={stage} baseline={before * 1000:.3f}ms '
              f'during={during * 1000:.3f}ms limit={limit * 1000:.3f}ms '
              f'samples={len(steps)} max={max(steps) * 1000:.3f}ms')
        assert during <= limit, f'{stage} synchronous p75 {during:.6f}s > {limit:.6f}s'


@pytest.mark.parametrize('outliers', [0, 1, 7])
def test_stage_budget_tolerates_fewer_than_quarter_synthetic_outliers(outliers):
    # Synthetic synchronous-slice distributions, not reinterpreted ingest p95s.
    _assert_nonblocking_stages([.001] * 75, {'fold': [.0015] * (30 - outliers) + [.100] * outliers})


def test_stage_budget_rejects_every_third_synthetic_stall():
    with pytest.raises(AssertionError, match='fold synchronous p75'):
        _assert_nonblocking_stages([.001] * 75, {'fold': [.0015] * 20 + [.050] * 10})


@pytest.mark.asyncio
async def test_synchronous_steps_exclude_suspension_and_include_return(monkeypatch):
    clock = [0.0]
    monkeypatch.setattr(time, 'perf_counter', lambda: clock[0])

    async def operation():
        clock[0] += .001
        asyncio.get_running_loop().call_soon(lambda: clock.__setitem__(0, clock[0] + 1))
        await asyncio.sleep(0)
        clock[0] += .002
        return 'complete'

    result, longest = await _SynchronousSteps(operation())
    assert result == 'complete'
    assert longest == pytest.approx(.002)


@pytest.mark.asyncio
async def test_synchronous_steps_forward_future_errors_and_cancellation():
    async def broken():
        await asyncio.sleep(0)
        raise ValueError('future failed')

    with pytest.raises(ValueError, match='future failed'):
        await _SynchronousSteps(asyncio.create_task(broken()))

    entered, exited = asyncio.Event(), asyncio.Event()

    async def waiting():
        try:
            entered.set()
            await asyncio.Future()
        finally:
            exited.set()

    task = asyncio.ensure_future(_SynchronousSteps(waiting()))
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert exited.is_set()


async def _exercise_projection_stages(monkeypatch):
    from app.core.transcript_views.bridge import ReducerBridge
    from app.core.transcript_views.producer import ProjectionActor, database
    from app.db import session_events as source
    from app.db import transcript_projections as store
    from app.db.profiles import create_profile
    from app.db.sessions import create_session

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
    task, ingest_task = None, None
    stop = asyncio.Event()
    stage_steps = {'reset': [], 'index': [], 'fold': []}
    appends, fold_windows = 0, []

    async def ingest(*, continuous=False):
        nonlocal appends
        values, steps = [], []
        while len(values) < 75 or (continuous and not stop.is_set()):
            start = time.perf_counter()
            _, longest = await _SynchronousSteps(database(source.append_events, ingest_sid, [
                {'type': 'assistant.text_delta', 'payload': {'text': 'small delta'}}]))
            values.append(time.perf_counter() - start)
            steps.append(longest)
            appends += 1
        return values, steps

    async def background():
        more, completed = False, 0
        try:
            for _ in range(75):
                # Re-fold a stable session; same-session appends otherwise keep
                # it chasing watermarks. Commit contention is tested by order below.
                previous = await database(store.load, sid)
                stage = 'reset' if not more else previous.stage
                before_appends = appends
                more, longest = await _SynchronousSteps(actor.process(sid, recheck=not more))
                stage_steps[stage].append(longest)
                if stage == 'fold':
                    fold_windows.append(appends - before_appends)
                if not more:
                    state = await database(store.load, sid)
                    assert state.ready, f'cycle failed: {state.stage}/{state.error_code}'
                    completed += 1
                await asyncio.sleep(0)
            assert completed >= 5, 'must exercise repeated ready projections'
            print(f'projection ingest quanta=75 ready={completed} '
                  f'overlapped_folds={sum(n > 0 for n in fold_windows)}/{len(fold_windows)} '
                  f'concurrent_appends={appends - 75}')
        finally:
            stop.set()

    try:
        for _ in range(32):
            if not await actor.process(sid):
                break
        assert (await database(store.load, sid)).ready
        baseline, baseline_steps = await ingest()
        # Ingestion remains alive until the actor finishes, not a fixed window
        # that ends before most folds. Schedule it first so overlap starts here.
        ingest_task = asyncio.create_task(ingest(continuous=True))
        task = asyncio.create_task(background())
        await task
        concurrent, concurrent_steps = await ingest_task
        for stage, steps in stage_steps.items():
            assert len(steps) >= 5, f'must sample the real {stage} stage'
        assert len(fold_windows) >= 5 and all(n > 0 for n in fold_windows), 'ingest must overlap every fold'
        p95 = lambda values: sorted(values)[math.ceil(len(values) * .95) - 1]
        print(f'projection ingest p95 (diagnostic): baseline={p95(baseline) * 1000:.3f}ms '
              f'concurrent={p95(concurrent) * 1000:.3f}ms '
              f'ingest_sync_p75={_p75(concurrent_steps) * 1000:.3f}ms')
        _assert_nonblocking_stages(baseline_steps, stage_steps)
    finally:
        stop.set()
        try:
            await asyncio.gather(*(t for t in (task, ingest_task) if t is not None), return_exceptions=True)
        finally:
            await bridge.close()


@pytest.mark.asyncio
async def test_background_projection_keeps_each_stage_nonblocking(monkeypatch):
    await _exercise_projection_stages(monkeypatch)


@pytest.mark.parametrize(('method', 'seconds', 'every'), [
    ('_fold', .010, 1), ('_fold', .050, 1), ('process', .050, 1),
    ('_index', .050, 1), ('_reset', .050, 1), ('_fold', .050, 3),
], ids=['fold-10ms', 'fold-50ms', 'process-50ms', 'index-50ms', 'reset-50ms', 'every-third-fold-50ms'])
@pytest.mark.asyncio
async def test_stage_budget_detects_real_blocking(monkeypatch, method, seconds, every):
    from app.core.transcript_views.producer import ProjectionActor

    real = getattr(ProjectionActor, method)
    calls = 0

    async def blocking(self, *args, **kwargs):
        nonlocal calls
        calls += 1
        if calls % every == 0:
            time.sleep(seconds)
        return await real(self, *args, **kwargs)

    monkeypatch.setattr(ProjectionActor, method, blocking)
    stage = 'reset' if method == 'process' else method[1:]
    print(f'projection negative control: method={method} sleep={seconds * 1000:.0f}ms every={every}')
    with pytest.raises(AssertionError, match=f'{stage} synchronous p75'):
        await _exercise_projection_stages(monkeypatch)


@pytest.mark.asyncio
async def test_stage_budget_rejects_failed_cycles(monkeypatch):
    from app.core.transcript_views.preprocess import TailUnavailable
    from app.core.transcript_views.producer import ProjectionActor

    fold, calls = ProjectionActor._fold, 0

    async def failing(self, *args, **kwargs):
        nonlocal calls
        calls += 1
        # Warm-up has two folds. Fail every second measured fold afterwards.
        if calls > 2 and calls % 2 == 0:
            raise TailUnavailable('negative_control')
        return await fold(self, *args, **kwargs)

    monkeypatch.setattr(ProjectionActor, '_fold', failing)
    with pytest.raises(AssertionError, match='cycle failed: unsupported/projection_limit'):
        await _exercise_projection_stages(monkeypatch)


@pytest.mark.skipif(os.environ.get('ALLOW_REAL_PG') != '1',
                    reason='SQLite shim serializes all writers; requires native PostgreSQL row locks')
@pytest.mark.parametrize('lock_source', [False, True], ids=['independent', 'detect-source-row-lock'])
@pytest.mark.asyncio
async def test_projection_commit_does_not_block_same_session_append(monkeypatch, lock_source):
    from app.db import transcript_projections as store

    print(f'projection commit control: injected_source_lock={lock_source}')
    if lock_source:
        cas = store._cas

        def locking_cas(cursor, state, previous):
            if previous is not None and state.scan_seq > previous.scan_seq:
                cursor.execute('SELECT next_seq FROM session_event_counters WHERE session_id=%s FOR UPDATE',
                               (state.session_id,))
            cas(cursor, state, previous)

        monkeypatch.setattr(store, '_cas', locking_cas)
        with pytest.raises(AssertionError, match='same-session append waited for projection commit release'):
            await _exercise_commit_order(monkeypatch)
    else:
        await _exercise_commit_order(monkeypatch)


async def _exercise_commit_order(monkeypatch):
    """An append must finish BEFORE the real fold's transaction is released."""
    from app.core.transcript_views.bridge import ReducerBridge
    from app.core.transcript_views.producer import ProjectionActor, database
    from app.db import session_events as source
    from app.db import transcript_projections as store
    from app.db.profiles import create_profile
    from app.db.sessions import create_session

    monkeypatch.setenv('AI_SHUFFLE_TRANSCRIPT_PRODUCER', '1')
    store.initialize()
    create_profile('commit-ingest', 'Fixture', None, {}, if_absent=True)
    sid = f'commit-ingest-{uuid4()}'
    create_session(sid, 'commit-ingest', derive_provider_from_profile=False)
    source.append_events(sid, [
        {'type': 'message.user', 'payload': {'content': 'real fold'}},
        {'type': 'turn.completed', 'payload': {}}])
    entered, release = threading.Event(), threading.Event()
    cas = store._cas

    def held_cas(cursor, state, previous):
        cas(cursor, state, previous)
        if state.session_id == sid and previous is not None and state.scan_seq > previous.scan_seq:
            entered.set()
            assert release.wait(30), 'test never released the fold commit'

    monkeypatch.setattr(store, '_cas', held_cas)
    bridge = ReducerBridge()
    actor = ProjectionActor(bridge)
    append_task = None

    async def project():
        for _ in range(20):
            if not await actor.process(sid):
                return
        pytest.fail('projection never finished')

    task = asyncio.create_task(project())
    try:
        assert await asyncio.to_thread(entered.wait, 30), 'fold commit never entered'
        await asyncio.sleep(0)
        if task.done():
            await task
            pytest.fail('projection finished before the held commit')
        start = time.perf_counter()
        append_task = asyncio.create_task(database(source.append_events, sid, [
            {'type': 'assistant.text_delta', 'payload': {'text': 'while commit held'}}]))
        # Do not cancel a DB worker on timeout: release and join it in finally.
        done, _ = await asyncio.wait({append_task}, timeout=10)
        elapsed = time.perf_counter() - start
        print(f'projection commit order: append={elapsed * 1000:.3f}ms '
              f'completed_while_held={bool(done)} deadlock_bound=10000ms')
        assert done, 'same-session append waited for projection commit release'
        await append_task
        assert not release.is_set() and not task.done()
    finally:
        release.set()
        try:
            await asyncio.gather(*(t for t in (task, append_task) if t is not None))
        finally:
            await bridge.close()
    assert (await database(store.load, sid)).ready
    assert await database(source.get_max_seq, sid) == 3


@pytest.mark.asyncio
async def test_large_preparation_allows_ingest_before_release(monkeypatch, tmp_path):
    from app.core.transcript_views import producer as implementation
    from app.core.transcript_views.bridge import ReducerBridge
    from app.core.transcript_views.producer import ProjectionActor, database
    from app.db import session_events as source
    from app.db import transcript_projections as store
    from app.db.profiles import create_profile
    from app.db.sessions import create_session

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
