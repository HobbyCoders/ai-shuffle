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
async def test_exhaustion_restart_recovers_receipt_without_reexecution(cluster, connect, session_id, tmp_path, source):
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
