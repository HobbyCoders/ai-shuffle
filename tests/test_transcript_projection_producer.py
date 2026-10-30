from __future__ import annotations

import json

import pytest

from app.api import events as serving
from app.core.transcript_views.bridge import ReducerBridge
from app.core.transcript_views.producer import ProjectionActor
from app.db import session_events as source
from app.db import transcript_projection_work as work
from app.db import transcript_projections as store
from app.db.sessions import create_session
from app.db.profiles import create_profile


def create(session_id='projection-fixture'):
    store.initialize()
    create_profile('fixture-profile', 'Fixture', None, {}, if_absent=True)
    create_session(session_id, 'fixture-profile', derive_provider_from_profile=False)
    return session_id


def event(kind, payload=None, turn='turn'):
    return {'type': kind, 'payload': payload or {}, 'turn_id': turn}


def corpus(turns=40):
    frames = [event('message.user', {'content': 'Start the task'}),
              event('subagent.started', {'task_id': 'background', 'tool_use_id': 'launch', 'description': 'Background'})]
    for index in range(turns):
        tool = f'tool-{index}'
        turn = f'turn-{index}'
        frames += [event('message.user', {'content': f'Request {index}'}, turn),
                   event('assistant.thinking_delta', {'text': 'Think'}, turn),
                   event('tool.input_delta', {'tool_use_id': tool, 'partial_json': '{'}, turn),
                   event('tool.use', {'tool_use_id': tool, 'name': 'Bash', 'input': {'command': 'pwd'}}, turn),
                   event('tool.result', {'tool_use_id': tool, 'content': 'done'}, turn),
                   event('assistant.text_delta', {'text': 'One'}, turn),
                   event('assistant.text_delta', {'text': ' two'}, turn),
                   event('turn.usage', {'input_tokens': 3, 'output_tokens': 4,
                                        'context_usage': {'entries': [{'tokens': 3}]}}, turn),
                   event('turn.completed', {}, turn)]
    frames += [event('subagent.progress', {'task_id': 'background', 'tool_use_id': 'launch', 'kind': 'text', 'text': 'A'}),
               event('subagent.progress', {'task_id': 'background', 'tool_use_id': 'launch', 'kind': 'text', 'text': 'B'}),
               event('subagent.completed', {'task_id': 'background', 'tool_use_id': 'launch', 'result': 'Finished'}),
               event('message.user', {'content': 'Continue'}, 'last')]
    return frames


async def drain(actor, session_id):
    for _ in range(200):
        more = await actor.process(session_id)
        state = store.load(session_id)
        if not more:
            assert state.ready == 1, (state.stage, state.error_code)
            return state
    pytest.fail('bounded actor did not converge')


def all_records(state):
    result, before = [], 2**53 - 1
    while True:
        page = store.page(state.session_id, state.generation, state.revision, before=before)
        result.extend(page['records'])
        if not page['hasOlder']:
            return result
        assert page['before'] < before
        before = page['before']


async def assert_full_serving_parity(bridge, state):
    frames = []
    async for _, frame in serving._replay_from(state.session_id, 0):
        data = next(line[6:] for line in frame.splitlines() if line.startswith('data: '))
        frames.append(json.loads(data))
    expected = await bridge.request('reference', carry=None, frames=frames, epoch=state.epoch)
    actual = await bridge.request('materialize', carry=state.preview_json,
                                  records=all_records(state), epoch=state.epoch)
    # Both sides are the lossless packed *actual state*: ordered fields/Maps,
    # explicit undefined, event seq/origin and epoch all remain in the oracle.
    assert actual == expected


@pytest.mark.asyncio
@pytest.mark.parametrize('progress,usage', [('0', '0'), ('0', '1'), ('1', '0'), ('1', '1')])
async def test_durable_producer_matches_real_full_cold_serving(progress, usage, monkeypatch):
    monkeypatch.setenv(serving._COALESCE_PROGRESS_ENV, progress)
    monkeypatch.setenv(serving._SLIM_USAGE_ENV, usage)
    sid = create()
    source.append_events(sid, corpus())
    bridge = ReducerBridge()
    try:
        state = await drain(ProjectionActor(bridge), sid)
        assert all_records(state), 'must exercise detached records, not only a carry fixture'
        await assert_full_serving_parity(bridge, state)
    finally:
        await bridge.close()


@pytest.mark.asyncio
async def test_restart_resumes_committed_index_and_fold_without_duplicate_rows():
    sid = create()
    source.append_events(sid, corpus())
    first = ReducerBridge()
    actor = ProjectionActor(first)
    for _ in range(6):
        await actor.process(sid)
    checkpoint = store.load(sid)
    assert checkpoint.scan_seq > 0
    await first.close()
    second = ReducerBridge()
    try:
        state = await drain(ProjectionActor(second), sid)
        assert state.generation == checkpoint.generation
        ids = [row['id'] for row in all_records(state)]
        assert len(ids) == len(set(ids))
        await assert_full_serving_parity(second, state)
    finally:
        await second.close()


@pytest.mark.asyncio
async def test_later_tool_use_invalidates_an_already_committed_partial_origin():
    sid = create()
    source.append_events(sid, [event('message.user', {'content': 'First'}),
                               event('tool.input_delta', {'tool_use_id': 'late', 'partial_json': '{'}),
                               event('message.user', {'content': 'Next'})])
    bridge = ReducerBridge()
    try:
        actor = ProjectionActor(bridge)
        previous = await drain(actor, sid)
        originals = source.get_events(sid)
        source.append_events(sid, [event('tool.use', {'tool_use_id': 'late', 'name': 'Read', 'input': {'path': 'a'}})])
        current = await drain(actor, sid)
        assert current.generation != previous.generation
        assert source.get_events(sid)[:3] == originals
        await assert_full_serving_parity(bridge, current)
    finally:
        await bridge.close()


@pytest.mark.asyncio
async def test_rewind_cleans_detached_rows_before_ready_and_invalidates_old_cursor(monkeypatch):
    monkeypatch.setattr(work, 'CLEANUP_ROWS', 16)
    sid = create()
    source.append_events(sid, corpus(50))
    bridge = ReducerBridge()
    try:
        actor = ProjectionActor(bridge)
        previous = await drain(actor, sid)
        assert len(all_records(previous)) > 96
        source.append_events(sid, [event('transcript.rewound', {'from_seq': 100}),
                                   event('message.user', {'content': 'After rewind'}, 'rewound')])
        saw_cleanup = False
        for _ in range(200):
            more = await actor.process(sid)
            current = store.load(sid)
            if current.cleanup_json != '[]':
                saw_cleanup = True
                assert current.ready == 0
            if not more:
                break
        assert saw_cleanup
        assert current.ready == 1
        with pytest.raises(store.ProjectionStoreUnavailable, match='stale_projection'):
            store.page(sid, previous.generation, previous.revision, before=2**53 - 1)
        await assert_full_serving_parity(bridge, current)
        assert len(source.get_events(sid, limit=1000)) == len(corpus(50)) + 2
    finally:
        await bridge.close()


@pytest.mark.asyncio
async def test_recheck_and_epoch_replacement_never_reuse_a_previous_generation():
    sid = create()
    source.append_events(sid, corpus())
    bridge = ReducerBridge()
    try:
        actor = ProjectionActor(bridge)
        previous = await drain(actor, sid)
        # The task owns this disposable fixture, not a real session/database.
        with store.get_db() as connection:
            connection.cursor().execute('DELETE FROM session_events WHERE session_id=%s', (sid,))
        source.append_events(sid, [event('message.user', {'content': 'Replacement'})])
        current = await drain(actor, sid)
        assert current.generation != previous.generation
        assert current.epoch != previous.epoch
        await assert_full_serving_parity(bridge, current)
        await actor.process(sid, recheck=True)
        reset = await drain(actor, sid)
        assert reset.generation != current.generation
        await assert_full_serving_parity(bridge, reset)
    finally:
        await bridge.close()
