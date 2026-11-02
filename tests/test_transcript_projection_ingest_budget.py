"""Projection/ingest independence on the isolated shim and native PostgreSQL.

The small case budgets p75 of the longest event-loop callback per reset, index
and fold quantum, including child Tasks and call_soon work. It rejects blocking
on every third quantum but tolerates <25% noisy quanta per stage. The 6ms allowance
covers CPU-contention plateaus near one scheduler time slice while the permanent
10ms fold control must still fail. Steady 3–6ms synchronous stalls per quantum
no longer fail. Ingest p95 and timer lag are diagnostic. This is NOT a
GIL/executor-starvation or rare-tail gate.
The large case proves child-preparation/ingest independence by order. Native PG
checks same-session appends across reset, index and fold commit calls, holding
transactions after their bodies and using a server-side lock-wait bound.
No source append hook calls the actor; perf_counter also works on Windows.
"""
from __future__ import annotations

import asyncio
from contextlib import contextmanager
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


def _measure_handles(monkeypatch):
    """Measure *all* loop callbacks during an await, including child Tasks.

    The final sleep(0) lets Handle._run record the operation's returning step.
    perf_counter is high resolution on Windows; suspended DB/IPC time is excluded.
    Only one measurement is active at a time (baseline, then actor quanta).
    """
    samples = None
    run = asyncio.events.Handle._run

    def timed_run(handle):
        start = time.perf_counter()
        try:
            return run(handle)
        finally:
            if samples is not None:
                samples.append(time.perf_counter() - start)

    monkeypatch.setattr(asyncio.events.Handle, '_run', timed_run)

    async def measure(operation):
        nonlocal samples
        assert samples is None, 'measurements must not overlap'
        samples = []
        try:
            result = await operation
            await asyncio.sleep(0)
            return result, max(samples)
        finally:
            samples = None

    return measure


def _p75(values):
    return sorted(values)[math.ceil(len(values) * .75) - 1]


def _assert_nonblocking_stages(baseline_steps, stage_steps):
    before = _p75(baseline_steps)
    # Per-stage p75 of the longest callback, not append p95 or timer lag.
    # Reject every-third blocking but tolerate <25% noisy quanta in each stage.
    # 6ms covers CPU-contention plateaus near one scheduler time slice while
    # keeping the permanent 10ms fold control a real rejection.
    limit = max(before * 1.35, before + .006)
    for stage, steps in stage_steps.items():
        during = _p75(steps)
        print(f'projection handle p75: stage={stage} baseline={before * 1000:.3f}ms '
              f'during={during * 1000:.3f}ms limit={limit * 1000:.3f}ms '
              f'samples={len(steps)} max={max(steps) * 1000:.3f}ms')
        assert during <= limit, f'{stage} handle p75 {during:.6f}s > {limit:.6f}s'


@pytest.mark.parametrize('outliers', [0, 1, 7])
def test_stage_budget_tolerates_fewer_than_quarter_synthetic_outliers(outliers):
    _assert_nonblocking_stages([.001] * 75, {'fold': [.0015] * (30 - outliers) + [.100] * outliers})


def test_stage_budget_rejects_every_third_synthetic_stall():
    with pytest.raises(AssertionError, match='fold handle p75'):
        _assert_nonblocking_stages([.001] * 75, {'fold': [.0015] * 20 + [.050] * 10})


@pytest.mark.parametrize('callback', [0, .050])
@pytest.mark.asyncio
async def test_handle_budget_includes_callbacks_and_final_return(monkeypatch, callback):
    clock = [0.0]
    monkeypatch.setattr(time, 'perf_counter', lambda: clock[0])
    measure = _measure_handles(monkeypatch)

    async def operation():
        clock[0] += .001
        asyncio.get_running_loop().call_soon(lambda: clock.__setitem__(0, clock[0] + callback))
        await asyncio.sleep(.001)  # Suspension is not callback execution time.
        clock[0] += .002
        return 'complete'

    result, longest = await measure(operation())
    assert result == 'complete'
    assert longest == pytest.approx(max(.002, callback))


async def _exercise_projection_stages(monkeypatch, *, quanta=75):
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

    measure = _measure_handles(monkeypatch)
    append_windows = []

    async def ingest(*, continuous=False):
        nonlocal appends
        values, steps = [], []
        while len(values) < 75 or (continuous and not stop.is_set()):
            start = time.perf_counter()
            operation = database(source.append_events, ingest_sid, [
                {'type': 'assistant.text_delta', 'payload': {'text': 'small delta'}}])
            if continuous:
                await operation
            else:
                _, longest = await measure(operation)
                steps.append(longest)
            end = time.perf_counter()
            values.append(end - start)
            append_windows.append((start, end))
            appends += 1
        return values, steps

    async def background():
        more, completed = False, 0
        try:
            for _ in range(quanta):
                # Re-fold a stable session; same-session appends otherwise keep
                # it chasing watermarks. Commit contention is tested by order below.
                previous = await database(store.load, sid)
                stage = 'reset' if not more else previous.stage
                start = time.perf_counter()
                more, longest = await measure(actor.process(sid, recheck=not more))
                stage_steps[stage].append(longest)
                if stage == 'fold':
                    fold_windows.append((start, time.perf_counter()))
                if not more:
                    state = await database(store.load, sid)
                    assert state.ready, f'cycle failed: {state.stage}/{state.error_code}'
                    completed += 1
                await asyncio.sleep(0)
            assert completed >= 5, 'must exercise repeated ready projections'
            print(f'projection ingest quanta={quanta} ready={completed} concurrent_appends={appends - 75}')
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
        concurrent, _ = await ingest_task
        for stage, steps in stage_steps.items():
            assert len(steps) >= 5, f'must sample the real {stage} stage'
        # An in-flight append overlaps even if one slow DB call spans a whole
        # fold. Requiring a completion per fold turned that single outlier red.
        overlaps = sum(any(a < end and b > start for a, b in append_windows)
                       for start, end in fold_windows)
        print(f'projection ingest overlap: folds={overlaps}/{len(fold_windows)}')
        assert len(fold_windows) >= 5 and overlaps == len(fold_windows), 'ingest must overlap every fold'
        p95 = lambda values: sorted(values)[math.ceil(len(values) * .95) - 1]
        print(f'projection ingest p95 (diagnostic): baseline={p95(baseline) * 1000:.3f}ms '
              f'concurrent={p95(concurrent) * 1000:.3f}ms')
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
    ('_index', .050, 1), ('_reset', .050, 1), ('_fold', .050, 3), ('child', .050, 1),
], ids=['fold-10ms', 'fold-50ms', 'process-50ms', 'index-50ms', 'reset-50ms', 'every-third-fold-50ms', 'child-50ms'])
@pytest.mark.asyncio
async def test_stage_budget_detects_real_blocking(monkeypatch, method, seconds, every):
    from app.core.transcript_views.producer import ProjectionActor

    target = 'process' if method == 'child' else method
    real = getattr(ProjectionActor, target)
    calls = 0

    async def stall():
        time.sleep(seconds)

    async def blocking(self, *args, **kwargs):
        nonlocal calls
        calls += 1
        if calls % every == 0:
            if method == 'child':
                await asyncio.create_task(stall())
            else:
                time.sleep(seconds)
        return await real(self, *args, **kwargs)

    monkeypatch.setattr(ProjectionActor, target, blocking)
    stage = 'reset' if target == 'process' else method[1:]
    print(f'projection negative control: method={method} sleep={seconds * 1000:.0f}ms every={every}')
    with pytest.raises(AssertionError, match=f'{stage} handle p75'):
        await _exercise_projection_stages(monkeypatch, quanta=25)


@pytest.mark.asyncio
async def test_stage_budget_tolerates_one_slow_append(monkeypatch):
    from app.db import session_events as source
    from random import Random

    append, calls = source.append_events, 0
    slow_at = Random(965).randint(76, 150)  # After the 75-append baseline.

    def slow(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == slow_at:
            time.sleep(.118)  # Off loop: observed #965 outlier, not loop blocking.
        return append(*args, **kwargs)

    monkeypatch.setattr(source, 'append_events', slow)
    await _exercise_projection_stages(monkeypatch)
    assert calls >= slow_at, 'must actually inject the single slow append'


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
@pytest.mark.parametrize('stage', ['reset', 'index', 'fold'])
@pytest.mark.parametrize('lock_source', [False, True, 'end'], ids=['independent', 'detect-source-row-lock', 'detect-tx-tail-lock'])
@pytest.mark.asyncio
async def test_projection_commit_does_not_block_same_session_append(monkeypatch, stage, lock_source):
    from app.db import transcript_projections as store

    print(f'projection commit control: stage={stage} injected_source_lock={lock_source}')
    if lock_source:
        cas, get_db, local = store._cas, store.get_db, threading.local()

        def locking_cas(cursor, state, previous):
            local.sid = state.session_id
            if lock_source != 'end':
                cursor.execute('SELECT next_seq FROM session_event_counters WHERE session_id=%s FOR UPDATE',
                               (state.session_id,))
            cas(cursor, state, previous)

        @contextmanager
        def locking_tail():
            with get_db() as connection:
                local.sid = None
                yield connection
                if local.sid:
                    connection.cursor().execute(
                        'SELECT next_seq FROM session_event_counters WHERE session_id=%s FOR UPDATE', (local.sid,))
                    connection.cursor().execute('SELECT pg_sleep(.05)')

        monkeypatch.setattr(store, '_cas', locking_cas)
        if lock_source == 'end':
            monkeypatch.setattr(store, 'get_db', locking_tail)
        with pytest.raises(AssertionError, match='same-session append blocked on projection source lock'):
            await _exercise_commit_order(monkeypatch, stage)
    else:
        await _exercise_commit_order(monkeypatch, stage)


async def _exercise_commit_order(monkeypatch, stage):
    """Append throughout the selected real commit CALL, including its tx tail.

    Hold after the transaction body, not inside its first statement. A 10ms
    PostgreSQL lock_timeout budgets only actual lock waits, not CPU scheduling.
    Keep appending before/after the hold too, covering separate transactions.
    """
    from psycopg2.errors import LockNotAvailable
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
    started, entered, release, finished = (threading.Event() for _ in range(4))
    local = threading.local()
    commit, get_db = store.commit, store.get_db
    armed = False

    def observed_commit(state, previous, **kwargs):
        kind = 'reset' if previous is None or state.generation != previous.generation else previous.stage
        selected = armed and state.session_id == sid and kind == stage and not started.is_set()
        local.hold = selected
        if selected:
            started.set()
        try:
            return commit(state, previous, **kwargs)
        finally:
            local.hold = False
            if selected:
                finished.set()

    @contextmanager
    def held_transaction():
        with get_db() as connection:
            yield connection
            if getattr(local, 'hold', False):
                entered.set()
                assert release.wait(10), 'test never released the commit transaction'

    def append():
        with source.get_event_db() as connection:
            connection.cursor().execute("SET LOCAL lock_timeout = '10ms'")
            return source.append_events(sid, [
                {'type': 'assistant.text_delta', 'payload': {'text': 'during real commit'}}], _conn=connection)

    monkeypatch.setattr(store, 'commit', observed_commit)
    monkeypatch.setattr(store, 'get_db', held_transaction)
    bridge = ReducerBridge()
    actor = ProjectionActor(bridge)
    append_task, appends, held_appends = None, 0, 0

    async def project():
        nonlocal armed
        for _ in range(20):
            if not await actor.process(sid):
                break
        assert (await database(store.load, sid)).ready
        armed = True
        for quantum in range(20):
            if not await actor.process(sid, recheck=quantum == 0):
                return
        pytest.fail('projection never finished')

    task = asyncio.create_task(project())
    try:
        assert await asyncio.to_thread(started.wait, 10), f'{stage} commit never entered'
        deadline = time.perf_counter() + 10  # Broken-test guard, not a latency budget.
        while not finished.is_set():
            assert time.perf_counter() < deadline, 'commit call never finished'
            start = time.perf_counter()
            append_task = asyncio.create_task(database(append))
            done, _ = await asyncio.wait({append_task}, timeout=10)
            assert done, 'same-session append never finished'
            try:
                await append_task
            except LockNotAvailable as error:
                print(f'projection source lock rejected: stage={stage} '
                      f'append_ms={(time.perf_counter()-start)*1000:.3f} lock_timeout_ms=10')
                raise AssertionError('same-session append blocked on projection source lock') from error
            appends += 1
            if entered.is_set() and not release.is_set():
                held_appends += 1
                print(f'projection commit order: stage={stage} append_ms={(time.perf_counter()-start)*1000:.3f} '
                      'completed_while_held=True lock_timeout_ms=10')
                release.set()
        assert held_appends == 1, 'append must finish before transaction release'
    finally:
        release.set()
        try:
            if append_task is not None:
                await asyncio.gather(append_task, return_exceptions=True)
            await task
        finally:
            await bridge.close()
    assert (await database(store.load, sid)).ready
    assert await database(source.get_max_seq, sid) == 2 + appends


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
