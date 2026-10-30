from __future__ import annotations

import asyncio

import pytest

from app.core.transcript_views.bridge import ReducerBridge
from app.core.transcript_views.manager import ProjectionProducer
from app.core.transcript_views.producer import ProjectionActor, database
from app.db import session_events as source
from app.db import transcript_projection_work as work
from app.db import transcript_projections as store
from app.db.profiles import create_profile
from app.db.sessions import create_session, delete_session


class SilentGateway:
    def __init__(self):
        self.listeners = []
    def add_global_listener(self, listener):
        self.listeners.append(listener)
    def remove_global_listener(self, listener):
        self.listeners.remove(listener)


def create(sid):
    create_profile('reconcile-profile', 'Fixture', None, {}, if_absent=True)
    create_session(sid, 'reconcile-profile', derive_provider_from_profile=False)


def test_source_reconciliation_pages_the_derived_side_before_joining():
    store.initialize()
    for index in range(39):
        sid = f'reconcile-{index:02}'
        if index % 2:
            create(sid)
        state = store.ProjectionState(sid, 'generation', 'pipeline', 'epoch', 0, None)
        store.commit(state, None)
    first = work.projection_sources(limit=1000)
    assert len(first) == 32
    second = work.projection_sources(first[-1]['session_id'])
    assert len(second) == 7
    rows = first + second
    assert [row['session_id'] for row in rows] == [f'reconcile-{i:02}' for i in range(39)]
    assert [bool(row['source_exists']) for row in rows] == [bool(i % 2) for i in range(39)]
    assert work.projection_sources(second[-1]['session_id']) == []


@pytest.mark.asyncio
@pytest.mark.parametrize('restart', [False, True], ids=['missed-hint', 'offline-restart'])
async def test_deleted_chat_is_retired_and_collected_without_a_delete_hint(monkeypatch, restart):
    monkeypatch.setenv('AI_SHUFFLE_TRANSCRIPT_PRODUCER', '1')
    monkeypatch.setattr(work, 'GARBAGE_ROWS', 16)
    sid = 'deleted-projection-fixture'
    store.initialize()
    create(sid)
    source.append_events(sid, [frame for index in range(90) for frame in [
        {'type': 'message.user', 'payload': {'content': f'Request {index}'}},
        {'type': 'assistant.text_delta', 'payload': {'text': f'Answer {index}'}},
        {'type': 'turn.completed', 'payload': {}}
    ]])
    bridge = ReducerBridge()
    actor = ProjectionActor(bridge)
    producer = None
    try:
        for _ in range(100):
            if not await actor.process(sid):
                break
        state = store.load(sid)
        assert state.ready == 1
        with store.get_db() as connection:
            cursor = connection.cursor()
            cursor.execute('SELECT COUNT(*) AS n FROM session_transcript_projection_records WHERE session_id=%s', (sid,))
            assert cursor.fetchone()['n'] > work.GARBAGE_ROWS
        loop, collected = asyncio.get_running_loop(), asyncio.Event()
        actual_collect = work.collect_garbage
        def collect():
            result = actual_collect()
            if not result and store.load(sid) is None:
                loop.call_soon_threadsafe(collected.set)
            return result
        monkeypatch.setattr(work, 'collect_garbage', collect)
        silent = SilentGateway()
        producer = ProjectionProducer(bridge=bridge, actor=actor, gateway=silent, scan_seconds=.005)
        producer.start()
        # Let startup finish; neither case ever invokes the gateway listeners.
        for _ in range(100):
            if silent.listeners:
                break
            await asyncio.sleep(.001)
        assert silent.listeners
        if restart:
            await producer.stop()
            assert silent.listeners == []
        assert await database(delete_session, sid)
        if restart:
            # No derived-state mutation is hidden in the offline deletion path.
            assert await database(store.load, sid) is not None
            bridge = ReducerBridge()
            producer = ProjectionProducer(bridge=bridge, gateway=SilentGateway(), scan_seconds=.005)
            producer.start()
        await asyncio.wait_for(collected.wait(), 5)
        assert await database(store.load, sid) is None
        with store.get_db() as connection:
            cursor = connection.cursor()
            for table in ('records', 'aliases', 'tools', 'inputs', 'retired'):
                cursor.execute(f'SELECT COUNT(*) AS n FROM session_transcript_projection_{table} WHERE session_id=%s', (sid,))
                assert cursor.fetchone()['n'] == 0, table
    finally:
        if producer is not None:
            await producer.stop()
        await bridge.close()
