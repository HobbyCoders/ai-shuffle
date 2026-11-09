"""A lost terminal must recover after restart, never replay an executed prompt."""
import asyncio
import json
import os
import sys
from pathlib import Path

import pytest

from app.db import base, runs, session_events, stop_notices, turns
from tests.db.postgres_harness import requires_real_pg
from tests.db.test_event_write_lock_timeout_postgres import (  # noqa: F401
    cluster, connect, session_id,
)

pytestmark = requires_real_pg

async def test_stop_capture_and_latch_need_no_run_or_counter_lock(session_id, connect, monkeypatch):
    from psycopg2.errors import LockNotAvailable

    owner = runs.claim_run(session_id, 'original-owner', 60)
    turn = turns.enqueue_turn(session_id, 'user', prompt='already executed')
    assert turns.start_turn(turn['id'], owner['id'], 'original-owner')
    with monkeypatch.context() as blocked:
        blocked.setattr(session_events, 'LOCK_RETRY_DELAYS_SECONDS', ())
        blocked.setattr(stop_notices, 'deliver', lambda _: (_ for _ in ()).throw(LockNotAvailable()))
        assert turns.settle_turn(turn['id'], 'aborted', 'recording_failed', run_id=owner['id'])
    holder = connect()
    rival = connect()
    try:
        cursor = holder.cursor()
        # Model the prefix a transcript writer holds while C remains contended.
        cursor.execute('SELECT id FROM runs WHERE session_id=%s FOR UPDATE', (session_id,))
        cursor.execute('INSERT INTO session_event_counters (session_id,next_seq) VALUES (%s,0) '
                       'ON CONFLICT (session_id) DO UPDATE SET next_seq=session_event_counters.next_seq',
                       (session_id,))
        receipt = await asyncio.wait_for(asyncio.to_thread(
            turns.capture_stop_receipt, session_id, turn['id'], None), 1)
        assert receipt is not None
        def latch_under_admission():
            # The synchronous latch is protected against enqueue/start, not a
            # plain check followed by an awaited side effect.
            with pytest.raises(LockNotAvailable):
                rival.cursor().execute('SELECT id FROM sessions WHERE id=%s FOR UPDATE NOWAIT', (session_id,))
            rival.rollback()
            return 'latched'
        assert await asyncio.wait_for(asyncio.to_thread(
            turns.apply_stop_receipt, session_id, receipt, latch_under_admission), 1) == 'latched'
        assert turns.get_turn(turn['id'])['state'] == 'draining'
        assert stop_notices.pending() == ['recording-failed:' + turn['id']]
    finally:
        holder.rollback()
        rival.rollback()
    # Admission/latching released S; normal terminal delivery still owns one close.
    assert stop_notices.deliver('recording-failed:' + turn['id'])
    assert len(session_events.get_events(session_id)) == 1


_SAVE_AND_EXIT = r"""
import json, sys
from app.core.config import settings
from app.db import base, runs, session_events, stop_notices, turns
object.__setattr__(settings, 'get_database_connection', lambda: json.loads(sys.argv[1]))
base.init_pool(min_conn=1, max_conn=4)
try:
    sid = sys.argv[2]
    run = runs.claim_run(sid, 'old-owner', 60)
    turn = turns.enqueue_turn(sid, sys.argv[3], prompt='already executed')
    assert turns.start_turn(turn['id'], run['id'], 'old-owner')
    session_events.LOCK_RETRY_DELAYS_SECONDS = (0, 0)
    result = turns.settle_turn(turn['id'], 'aborted', 'recording_failed', run_id=run['id'])
    assert result['state'] == 'draining'
    assert stop_notices.pending() == ['recording-failed:' + turn['id']]
    # The detecting/Stop process dies after guarded admission. No lock or
    # transition may strand terminal recovery in the restarted process.
    receipt = turns.capture_stop_receipt(sid, turn['id'], None)
    assert receipt is not None and receipt['cleanup_guard']['turn']['id'] == turn['id']
    assert turns.get_turn(turn['id'])['stop_reason'] == 'recording_failed'
    runs.release_run(run['id'], run['fence_token'])
    print(json.dumps({'turn_id': turn['id'], 'run_id': run['id']}), flush=True)
finally:
    base.close_pool()
"""

_RESTART_AND_RECOVER = r"""
import asyncio, json, sys
from app.core.config import settings
from app.db import base, turns
from app.core.scoped_remote_stop import retry_incomplete_notes
from app.core import query_session_lifecycle
object.__setattr__(settings, 'get_database_connection', lambda: json.loads(sys.argv[1]))
base.init_pool(min_conn=1, max_conn=4)
async def forbidden(*a, **kw):
    raise AssertionError('terminal recovery must not perform Stop cleanup')
query_session_lifecycle.stop_all_background_tasks = forbidden
async def recover():
    assert turns.reap_expired() == []
    await retry_incomplete_notes()
    await retry_incomplete_notes()
try:
    asyncio.run(recover())
finally:
    base.close_pool()
"""


async def _child(script, cluster, tmp_path, *args):
    env = {key: os.environ[key] for key in ('PATH', 'SYSTEMROOT', 'WINDIR') if key in os.environ}
    env.update(TESTING='1', DATA_DIR=str(tmp_path / 'data'), HOME=str(tmp_path),
               PYTHONPATH=str(Path(__file__).resolve().parents[2] / 'fluid'))
    child = await asyncio.create_subprocess_exec(
        sys.executable, '-c', script, json.dumps(cluster), *args, env=env,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(child.communicate(), 12)
    finally:
        if child.returncode is None:
            child.kill()
            await child.wait()
    assert child.returncode == 0, (stdout + stderr).decode()
    return stdout.decode()


@pytest.mark.parametrize('source', ['user', 'queue'])
@pytest.mark.parametrize('late_stop', [False, True], ids=['restart', 'stop-then-restart'])
async def test_exhaustion_restart_recovers_receipt_without_reexecution(
    cluster, connect, session_id, tmp_path, source, late_stop, monkeypatch,
):
    holder = connect()
    holder.cursor().execute(
        'INSERT INTO session_event_counters (session_id,next_seq) VALUES (%s,0)', (session_id,),
    )
    holder.commit()
    holder.cursor().execute(
        'SELECT next_seq FROM session_event_counters WHERE session_id=%s FOR UPDATE', (session_id,),
    )
    try:
        output = await _child(_SAVE_AND_EXIT, cluster, tmp_path / 'old-process', session_id, source)
        saved = json.loads(output)
        turn_id = saved['turn_id']
        # The process that noticed failure has exited with the counter STILL
        # locked. Neither a reap nor a new runner may execute its input again.
        assert turns.reap_expired() == []
        assert turns.get_turn(turn_id)['state'] == 'draining'
        assert turns.requeue_turn(turn_id) is None
        replacement = runs.claim_run(session_id, 'replacement-owner', 60)
        assert turns.start_turn(turn_id, replacement['id'], 'replacement-owner') is None
        successor = turns.enqueue_turn(session_id, 'user', prompt='new input')
        assert turns.start_turn(successor['id'], replacement['id'], 'replacement-owner') is None
        runs.release_run(replacement['id'], replacement['fence_token'])
        assert session_events.get_events(session_id) == []
        assert stop_notices.pending() == ['recording-failed:' + turn_id]
        stop_error = None
        if late_stop:
            from app.core import turn_manager
            from psycopg2.errors import LockNotAvailable

            # The counter remains locked AFTER lease release. Before the fix,
            # Stop overwrites the marker and its own close exhausts as well.
            # Catch that confirmed rollback to exercise the eventual recovery.
            monkeypatch.setattr(session_events, 'LOCK_RETRY_DELAYS_SECONDS', (0,))
            try:
                await asyncio.wait_for(turn_manager.interrupt_async(
                    session_id, 'stop', stop_background=True, turn_id=turn_id,
                ), 6)
            except LockNotAvailable as exc:
                stop_error = exc
    finally:
        holder.rollback()
    # Fresh interpreter, real persisted outbox, actual recovery consumer. No
    # in-memory writer, callback or failed recorder batch can rescue this test.
    await _child(_RESTART_AND_RECOVER, cluster, tmp_path / 'new-process')
    assert stop_notices.pending() == []
    assert turns.get_turn(turn_id)['state'] == 'aborted'
    assert turns.pop_ready(session_id)['id'] == successor['id']
    rows = session_events.get_events(session_id)
    assert len(rows) == 1 and rows[0]['id'] == 'recording-failed:' + turn_id
    assert rows[0]['event_type'] == 'turn.aborted'
    assert rows[0]['turn_id'] == turn_id
    assert rows[0]['payload']['stop_reason'] == 'recording_failed'
    assert stop_error is None  # marked Stop must not try a contended append
    assert turns.start_turn(successor['id'], 'next-run', 'replacement-owner')
    assert turns.settle_turn(successor['id'], 'completed', run_id='next-run')


async def test_replacement_holding_turn_row_defeats_old_intent_guard(connect, session_id):
    old = runs.claim_run(session_id, 'old-owner', 60)
    turn = turns.enqueue_turn(session_id, 'user', prompt='already executed')
    assert turns.start_turn(turn['id'], old['id'], 'old-owner')
    holder = connect()
    holder.cursor().execute('UPDATE turns SET run_id=%s WHERE id=%s', ('new-run', turn['id']))
    settling = asyncio.create_task(asyncio.to_thread(
        turns.settle_turn, turn['id'], 'aborted', 'recording_failed', run_id=old['id'],
    ))
    try:
        # Observe the real row-lock wait before publishing the replacement.
        observer = connect(autocommit=True)
        async with asyncio.timeout(3):
            while True:
                cur = observer.cursor()
                cur.execute("SELECT 1 FROM pg_stat_activity WHERE wait_event_type='Lock' "
                            "AND query LIKE 'UPDATE turns SET state=%'")
                if cur.fetchone():
                    break
                await asyncio.sleep(.01)
        assert not settling.done()
        holder.commit()
        assert await asyncio.wait_for(settling, 3) is None
        row = turns.get_turn(turn['id'])
        assert row['state'] == 'running' and row['run_id'] == 'new-run'
        assert stop_notices.pending() == []
        assert session_events.get_events(session_id) == []
    finally:
        holder.rollback()
        await asyncio.gather(settling, return_exceptions=True)


async def test_delayed_delivery_cannot_close_replacement_holding_row(connect, session_id, monkeypatch):
    from psycopg2.errors import LockNotAvailable

    turn = turns.enqueue_turn(session_id, 'user', prompt='already executed')
    turns.start_turn(turn['id'], 'old-run', 'old-owner')
    deliver = stop_notices.deliver
    with monkeypatch.context() as patch:
        patch.setattr(session_events, 'LOCK_RETRY_DELAYS_SECONDS', ())
        patch.setattr(stop_notices, 'deliver', lambda _: (_ for _ in ()).throw(LockNotAvailable()))
        assert turns.settle_turn(turn['id'], 'aborted', 'recording_failed', run_id='old-run')
    notice_id = 'recording-failed:' + turn['id']
    assert stop_notices.pending() == [notice_id]
    holder = connect()
    holder.cursor().execute("UPDATE turns SET run_id='new-run', state='running', stop_reason=NULL "
                            "WHERE id=%s", (turn['id'],))

    async def retry_delivery():
        # Each 100ms timeout must roll back. Keep retrying so observing the wait
        # is deterministic even on a loaded runner, not a scheduler-speed bet.
        async with asyncio.timeout(3):
            while True:
                try:
                    return await asyncio.to_thread(deliver, notice_id)
                except LockNotAvailable:
                    await asyncio.sleep(.01)

    delivery = asyncio.create_task(retry_delivery())
    try:
        observer = connect(autocommit=True)
        async with asyncio.timeout(2):
            while True:
                cur = observer.cursor()
                cur.execute("SELECT 1 FROM pg_stat_activity WHERE wait_event_type='Lock' "
                            "AND query LIKE 'UPDATE turns SET state=%'")
                if cur.fetchone():
                    break
                await asyncio.sleep(.01)
        assert not delivery.done()
        holder.commit()
        assert await delivery
        assert stop_notices.pending() == []  # stale intent consumed
        row = turns.get_turn(turn['id'])
        assert row['state'] == 'running' and row['run_id'] == 'new-run'
        assert session_events.get_events(session_id) == []
    finally:
        holder.rollback()
        await asyncio.gather(delivery, return_exceptions=True)


async def test_failure_owned_stop_cleanup_uses_latest_run_even_when_released(session_id, monkeypatch):
    from app.core import turn_manager
    from psycopg2.errors import LockNotAvailable

    old = runs.claim_run(session_id, 'old-owner', 60)
    turn = turns.enqueue_turn(session_id, 'user', prompt='already executed')
    assert turns.start_turn(turn['id'], old['id'], 'old-owner')
    with monkeypatch.context() as blocked:
        blocked.setattr(session_events, 'LOCK_RETRY_DELAYS_SECONDS', ())
        blocked.setattr(stop_notices, 'deliver', lambda _: (_ for _ in ()).throw(LockNotAvailable()))
        assert turns.settle_turn(turn['id'], 'aborted', 'recording_failed', run_id=old['id'])
    marker = turns.get_turn(turn['id'])
    assert turn_manager.capture_stop_receipt(session_id, turn['id'], None) is not None
    assert runs.release_run(old['id'], old['fence_token'])
    assert turn_manager.capture_stop_receipt(session_id, turn['id'], None) is not None
    replacement = runs.claim_run(session_id, 'replacement-owner', 60)
    assert replacement
    assert turn_manager.capture_stop_receipt(session_id, turn['id'], None) is None
    assert runs.release_run(replacement['id'], replacement['fence_token'])
    assert turn_manager.capture_stop_receipt(session_id, turn['id'], None) is None
    assert turns.get_turn(turn['id']) == marker
    assert stop_notices.pending() == ['recording-failed:' + turn['id']]
    assert stop_notices.deliver('recording-failed:' + turn['id'])
    assert len(session_events.get_events(session_id)) == 1
