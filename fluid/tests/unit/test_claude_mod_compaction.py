"""Mechanical Claude mod compaction: stored originals, scoped HTTP, safe fallback."""
from __future__ import annotations

import asyncio
import json

import pytest

from fluid.engine.claude_mod_compaction import (
    COMPACT_PATH, MAX_COMPACT_BODY_BYTES, compact_messages, project,
)
from fluid.context.compaction import CompactionConfig
from fluid.context.recall import expand_ref
from fluid.context.trim import TrimConfig
from fluid.engine.delegated import CLAUDE_MOD_COMPACT_URL_ENV, DelegatedLoop
from fluid.messages import Message, Text, ToolResultBlock, ToolUseBlock
from fluid.store.base import MemoryStore, SessionNode, new_id
from fluid.tools.mcp.bundled_server import JsonHttpRoute, serve_registry_socket
from fluid.tools.registry import ToolRegistry
from fluid.tools.builtins import register_builtins
from fluid.engine.delegated_proto import StreamJsonMapper


def history():
    nodes, rows = [], []
    for i in range(9):
        messages = [
            Message('user', [Text(f'Request {i}: keep the open migration task.')]),
            Message('assistant', [ToolUseBlock(f't{i}', 'Read', {'file_path': f'{i}.py'})]),
            Message('user', [ToolResultBlock(f't{i}', [Text(f'original-{i} ' + 'x' * 6000)])]),
            Message('assistant', [Text(f'Inspected {i}.')]),
        ]
        for m in messages:
            node = SessionNode(f'node_{len(nodes):012x}', nodes[-1].node_id if nodes else None, m)
            nodes.append(node)
            rows.append(dict(role=m.role, text=''.join(b.text for b in m.content if isinstance(b, Text)),
                             toolUses=[dict(tool_use_id=b.id, tool=b.name, input=b.input) for b in m.content if isinstance(b, ToolUseBlock)],
                             toolResults=[dict(tool_use_id=b.tool_use_id, text=''.join(c.text for c in b.content if isinstance(c, Text)), isError=b.is_error) for b in m.content if isinstance(b, ToolResultBlock)],
                             handle=f'cli-{len(rows)}'))
    return nodes, rows


def config(**kw):
    return CompactionConfig(trim=TrimConfig(keep_recent_tool_results=2), **kw)


def test_on_by_default_and_the_host_decides():
    assert DelegatedLoop(store=MemoryStore(), model='m').claude_mod_compaction is True
    assert DelegatedLoop(store=MemoryStore(), model='m', claude_mod_compaction=False).claude_mod_compaction is False


class _Recording(dict):
    """An environment that remembers every name looked up in it."""

    def __init__(self, *args):
        super().__init__(*args)
        self.read = set()

    def get(self, key, default=None):
        self.read.add(key)
        return super().get(key, default)

    def __getitem__(self, key):
        self.read.add(key)
        return super().__getitem__(key)

    def __contains__(self, key):
        self.read.add(key)
        return super().__contains__(key)


@pytest.mark.parametrize('chosen', [True, False])
async def test_no_environment_variable_decides_compaction(monkeypatch, chosen):
    import os
    # Every compaction-looking name set to the opposite of the host's choice.
    env = _Recording({**os.environ, **{
        name: ('0' if chosen else '1')
        for name in ('FLUID_COMPACTION', 'CLAUDE_MOD_COMPACTION', 'FLUID_CLAUDE_COMPACTION')
    }})
    monkeypatch.setattr(os, 'environ', env)
    loop = DelegatedLoop(store=MemoryStore(), model='m', registry=ToolRegistry(),
                         env={'FLUID_COMPACTION': '0' if chosen else '1'},
                         claude_mod_compaction=chosen)
    await loop._ensure_session()
    await loop._start_bundled_server()
    loop._claude_mod_dir = 'staged'
    try:
        assert loop.claude_mod_compaction is chosen
        assert loop._compaction_route is chosen
        assert (CLAUDE_MOD_COMPACT_URL_ENV in loop._claude_mod_env()) is chosen
        assert not [name for name in env.read if 'COMPACT' in name.upper()]
    finally:
        await loop.aclose()


def test_handles_originals_pairs_and_recovery():
    nodes, rows = history()
    before = [n.message.to_dict() for n in nodes]
    answer = compact_messages(rows, nodes, config=config())
    assert len(json.dumps(answer)) < len(json.dumps(rows))
    assert answer[-1] == rows[-1]  # original handle and all opaque metadata survive
    assert before == [n.message.to_dict() for n in nodes]
    text = json.dumps(answer)
    assert 'HistoryExpand' in text
    import re
    for ref in re.findall(r'node_[0-9a-f]+(?:\.\.node_[0-9a-f]+)?', text):
        restored, error = expand_ref(nodes, ref)
        assert not error, ref
        assert restored
    pending = set()
    for row in answer:
        for result in row.get('toolResults', []):
            assert result['tool_use_id'] in pending
            pending.remove(result['tool_use_id'])
        for use in row['toolUses']:
            assert use['tool_use_id'] not in pending
            pending.add(use['tool_use_id'])
    assert not pending
    assert all(a['role'] != b['role'] for a, b in zip(answer, answer[1:]))


def test_errors_are_not_trimmed():
    nodes, rows = history()
    nodes[2].message.content[0].is_error = True
    rows[2]['toolResults'][0]['isError'] = True
    answer = compact_messages(rows, nodes, config=config())
    assert rows[2] in answer


@pytest.mark.parametrize('mutation', ['orphan', 'duplicate', 'no_handle', 'wrong_role'])
def test_invalid_input_fails_closed(mutation):
    nodes, rows = history()
    if mutation == 'orphan':
        rows[2]['toolResults'][0]['tool_use_id'] = 'absent'
    elif mutation == 'duplicate':
        rows[2]['handle'] = rows[1]['handle']
    elif mutation == 'no_handle':
        rows[2].pop('handle')
    else:
        rows[2]['role'] = 'system'
    with pytest.raises(ValueError):
        compact_messages(rows, nodes, config=config())


def test_unknown_cli_messages_are_kept_not_given_fake_refs():
    nodes, rows = history()
    rows.insert(0, dict(role='user', text='CLI-only attachment', toolUses=[], handle='opaque'))
    answer = compact_messages(rows, nodes, config=config())
    assert answer[0] == rows[0]
    assert 'HistoryExpand' in json.dumps(answer)


def test_all_three_tiers_keep_real_refs_and_never_invoke_a_summary():
    nodes, rows = history()
    answer = compact_messages(rows, nodes, config=config(
        keep_recent_turns=1, max_resident_span_digests=2, epoch_fold_spans=2,
        summary_enabled=True,
    ))
    text = json.dumps(answer)
    assert 'epoch' in text.lower()
    assert 'HistoryExpand' in text
    assert rows[-1] == answer[-1]


def test_unknown_history_does_not_create_a_fake_recall_path():
    _, rows = history()
    with pytest.raises(ValueError, match='no safe compaction saving'):
        compact_messages(rows, [])


async def test_producing_stream_frames_through_loop_route_and_history_tools():
    from fluid.tools.builtins.history_expand import handler as expand
    from fluid.tools.builtins.history_search import handler as search
    registry = ToolRegistry()
    register_builtins(registry)
    store = MemoryStore()
    loop = DelegatedLoop(store=store, model='m', registry=registry)
    meta = await loop._ensure_session()
    parent = None
    rows = []
    for i in range(9):
        node = SessionNode(f'node_{i * 4:012x}', parent, Message('user', [Text(f'User request {i}')]))
        await store.append(meta.session_id, node)
        parent = node.node_id
        rows.append(dict(role='user', text=f'User request {i}', toolUses=[], handle=f'u{i}'))
        mapper = StreamJsonMapper(session_id=meta.session_id, turn_id=f'turn-{i}', model='m', user_node_id=parent)
        for frame in [
            {'type': 'assistant', 'uuid': f'a{i}', 'message': {'role': 'assistant', 'content': [
                {'type': 'tool_use', 'id': f't{i}', 'name': 'Read', 'input': {'file_path': f'{i}.py'}}]}},
            {'type': 'user', 'uuid': f'r{i}', 'message': {'role': 'user', 'content': [
                {'type': 'tool_result', 'tool_use_id': f't{i}', 'content': f'RECOVER-{i} ' + 'z' * 6000}]}},
            {'type': 'assistant', 'uuid': f'b{i}', 'message': {'role': 'assistant', 'content': [{'type': 'text', 'text': f'Completed {i}'}]}},
        ]:
            mapped = mapper.feed(frame)
            assert mapped.message is not None
            await store.append(meta.session_id, SessionNode(mapped.node_id, parent, mapped.message))
            parent = mapped.node_id
            from fluid.engine.claude_mod_compaction import project
            rows.append({**project(mapped.message), 'handle': frame['uuid']})
    await loop._start_bundled_server()
    loop._claude_mod_dir = 'staged'
    try:
        env = loop._claude_mod_env()
        assert env[CLAUDE_MOD_COMPACT_URL_ENV].endswith(COMPACT_PATH)
        assert env[CLAUDE_MOD_COMPACT_URL_ENV].removesuffix(COMPACT_PATH) == env['FLUID_CLAUDE_MOD_URL'].removesuffix('/claude-mod/usage')
        status, body = await request(loop._socket_path, {'v': 1, 'messages': rows}, token=loop._mod_token)
        assert status == 200
        assert 'HistoryExpand' in json.dumps(body)
        refs = await search({'query': 'RECOVER-0'}, loop.ctx)
        assert not refs.is_error
        import re
        ref = re.search(r'node_[0-9a-f]+', refs.text).group()
        result = await expand({'ref': ref}, loop.ctx)
        assert not result.is_error
        assert 'RECOVER-0' in result.text
    finally:
        await loop.aclose()


async def request(address, body, token='mod-token', path=COMPACT_PATH):
    if address.startswith('tcp:'):
        host, _, port = address[4:].rpartition(':')
        reader, writer = await asyncio.open_connection(host, int(port))
    else:
        reader, writer = await asyncio.open_unix_connection(address)
    raw = json.dumps(body).encode()
    auth = '' if token is None else f'Authorization: Bearer {token}\r\n'
    writer.write((f'POST {path} HTTP/1.1\r\nHost: localhost\r\n{auth}Content-Type: application/json\r\nContent-Length: {len(raw)}\r\n\r\n').encode() + raw)
    await writer.drain()
    data = await asyncio.wait_for(reader.read(), 10)
    writer.close()
    await writer.wait_closed()
    head, _, content = data.partition(b'\r\n\r\n')
    return int(head.split()[1]), json.loads(content) if content else None


@pytest.fixture
async def served():
    nodes, rows = history()
    async def route(body):
        if body == 'explode':
            raise RuntimeError('do not expose private content')
        return {'messages': compact_messages(body['messages'], nodes, config=config())}
    server = await serve_registry_socket(ToolRegistry(), 'tcp:127.0.0.1:0', token='tool-token',
        http_token='mod-token', http_routes={COMPACT_PATH: JsonHttpRoute(route, MAX_COMPACT_BODY_BYTES)})
    address = f'tcp:127.0.0.1:{server.sockets[0].getsockname()[1]}'
    try:
        yield address, rows
    finally:
        server.close()
        await server.wait_closed()


async def test_route_returns_large_compacted_json(served):
    address, rows = served
    status, body = await request(address, {'v': 1, 'messages': rows})
    assert status == 200
    assert body['messages'][-1] == rows[-1]


@pytest.mark.parametrize('token', [None, '', 'wrong', 'tool-token'])
async def test_route_auth(served, token):
    address, rows = served
    assert (await request(address, {'messages': rows}, token=token))[0] == 401


async def test_compaction_body_limit_rejects_before_reading_payload(served):
    address, _ = served
    reader, writer = await asyncio.open_connection('127.0.0.1', int(address.rsplit(':', 1)[1]))
    writer.write((f'POST {COMPACT_PATH} HTTP/1.1\r\nHost: localhost\r\n'
                  f'Authorization: Bearer mod-token\r\nContent-Type: application/json\r\n'
                  f'Content-Length: {MAX_COMPACT_BODY_BYTES + 1}\r\n\r\n').encode())
    await writer.drain()
    assert b'413' in await asyncio.wait_for(reader.readline(), 2)
    writer.close()
    await writer.wait_closed()


async def test_wrong_use_and_handler_failure(served, caplog):
    address, rows = served
    assert (await request(address, {'messages': rows}, path='/not-authorized'))[0] == 404
    status, body = await request(address, 'explode')
    assert status == 500
    assert body is None
    assert 'do not expose private content' not in caplog.text
    port = int(address.rsplit(':', 1)[1])
    reader, writer = await asyncio.open_connection('127.0.0.1', port)
    writer.write(b'{"op":"list","token":"mod-token"}\n')
    await writer.drain()
    assert 'unauthorized' in (await reader.readline()).decode()
    writer.close()
    await writer.wait_closed()


async def test_parallel_frames_compact_as_paired_built_groups():
    from fluid.engine.claude_mod_compaction import compact_messages, project
    store = MemoryStore()
    meta = await store.create_session()
    parent = None
    rows = []
    nodes = []
    for turn in range(8):
        messages = [Message('user', [Text(f'request {turn}')])]
        for part in range(2):
            messages.append(Message('assistant', [ToolUseBlock(f'p{turn}-{part}', 'Read', {'file_path': f'f{turn}-{part}'})]))
        for part in range(2):
            messages.append(Message('user', [ToolResultBlock(f'p{turn}-{part}', [Text(f'parallel-{turn}-{part} ' * 2000)])]))
        messages.append(Message('assistant', [Text(f'finished {turn}')]))
        for message in messages:
            node = SessionNode(new_id('node'), parent, message)
            await store.append(meta.session_id, node)
            parent = node.node_id
            nodes.append(node)
            rows.append({**project(message), 'handle': new_id('handle')})
    output = compact_messages(rows, nodes)
    assert all(a['role'] != b['role'] for a, b in zip(output, output[1:]))
    calls = {u['tool_use_id'] for r in output for u in r['toolUses']}
    results = {t['tool_use_id'] for r in output for t in r.get('toolResults', [])}
    assert calls == results
    assert len(json.dumps(output)) < len(json.dumps(rows))


def streamed_parallel_history(order, *, error_at=None):
    """Stored the way a delegated turn streams it (call, result, call, result),
    listed the way Claude Code's session.compact lists it: every call before
    the results, or a call arriving after a result while others are open."""
    nodes, rows = [], []

    def add(message, uuid):
        nodes.append(SessionNode(new_id('node'), nodes[-1].node_id if nodes else None, message))
        message.provider_meta['claude_uuid'] = uuid
        row = {**project(message), 'handle': uuid}
        return row

    for turn in range(8):
        rows.append(add(Message('user', [Text(f'request {turn}')]), f'u{turn}'))
        calls, results = [], []
        for part in range(3):
            tid = f'p{turn}-{part}'
            calls.append(add(Message('assistant', [ToolUseBlock(tid, 'Read', {'file_path': f'{tid}.py'})]), f'c{tid}'))
            results.append(add(Message('user', [ToolResultBlock(
                tid, [Text(f'parallel-{tid} ' * 600)], is_error=(turn, part) == error_at)]), f'r{tid}'))
        if order == 'calls_first':
            rows += calls + results
        else:  # call, call, result, call, result, result
            rows += [calls[0], calls[1], results[0], calls[2], results[1], results[2]]
        rows.append(add(Message('assistant', [Text(f'finished {turn}')]), f'a{turn}'))
    return nodes, rows


def assert_mod_accepts(output, originals):
    """What the mod's compactedRows (hooks/mechanical.ts) demands of an answer."""
    by_handle = {r['handle']: json.dumps(r) for r in originals}
    calls = {u['tool_use_id'] for r in originals for u in r['toolUses']}
    assert output[0]['role'] == 'user'
    pending = set()
    for previous, row in zip([None] + output, output):
        if pending:  # an open group continues: more calls, or its results
            assert (row['role'] == previous['role'] == 'assistant' and row['toolUses']
                    or row['role'] == 'user' and row.get('toolResults'))
        else:
            assert previous is None or previous['role'] != row['role']
        if 'handle' in row:
            assert by_handle[row['handle']] == json.dumps(row)
        for result in row.get('toolResults', []):
            pending.remove(result['tool_use_id'])
        for use in row['toolUses']:
            assert use['tool_use_id'] in calls
            pending.add(use['tool_use_id'])
    assert not pending


@pytest.mark.parametrize('order', ['calls_first', 'interleaved'])
def test_parallel_group_listed_out_of_stream_order_compacts(order):
    nodes, rows = streamed_parallel_history(order)
    output = compact_messages(rows, nodes)
    assert_mod_accepts(output, rows)
    assert len(json.dumps(output)) < len(json.dumps(rows))
    assert output[-1] == rows[-1]
    import re
    refs = re.findall(r'node_[0-9a-f]+(?:\.\.node_[0-9a-f]+)?', json.dumps(output))
    assert refs
    for ref in refs:
        restored, error = expand_ref(nodes, ref)
        assert not error and restored, ref


def test_group_with_an_error_result_is_kept_whole():
    nodes, rows = streamed_parallel_history('calls_first', error_at=(1, 1))
    output = compact_messages(rows, nodes)
    assert_mod_accepts(output, rows)
    text = json.dumps(output)
    for part in range(3):
        # Call and result of every member, untrimmed, not just the error.
        assert f'"p1-{part}.py"' in text
        assert f'parallel-p1-{part} ' * 600 in text


@pytest.mark.parametrize('order', ['calls_first', 'interleaved'])
def test_group_with_a_member_only_the_cli_can_rebuild_stays_its_own_rows(order):
    # ToolSearch's tool_reference result: the CLI lists it with no text, the
    # stream stored it as text, so it never matches and must keep its handle.
    nodes, rows = streamed_parallel_history(order)
    opaque = next(r for r in rows if r['handle'] == 'rp1-1')
    opaque['toolResults'][0]['text'] = ''
    output = compact_messages(rows, nodes)
    assert_mod_accepts(output, rows)
    # The calls may merge into one built row; the results keep their handles
    # (one of them cannot be rebuilt), right after the calls, in order.
    start = next(i for i, r in enumerate(output) if any(u['tool_use_id'] == 'p1-0' for u in r['toolUses']))
    import itertools
    call_rows = list(itertools.takewhile(lambda r: r['toolUses'], output[start:]))
    assert [u['tool_use_id'] for r in call_rows for u in r['toolUses']] == ['p1-0', 'p1-1', 'p1-2']
    after = output[start + len(call_rows):][:3]
    assert [r.get('handle') for r in after] == ['rp1-0', 'rp1-1', 'rp1-2']
    assert after[1] == opaque
    assert len(json.dumps(output)) < len(json.dumps(rows))


async def test_a_refused_compaction_is_logged_without_content(caplog):
    registry = ToolRegistry()
    register_builtins(registry)
    loop = DelegatedLoop(store=MemoryStore(), model='m', registry=registry)
    await loop._ensure_session()
    await loop._start_bundled_server()
    _, rows = history()  # nothing stored for them: no safe saving
    try:
        with caplog.at_level('INFO', logger='fluid.engine.delegated'):
            status, _ = await request(loop._socket_path, {'v': 1, 'messages': rows}, token=loop._mod_token)
        assert status == 400
        assert 'fell back to the CLI summarizer: no safe compaction saving' in caplog.text
        assert 'original-0' not in caplog.text
    finally:
        await loop.aclose()


async def test_loop_off_does_not_expose_route_and_on_injects_recall():
    loop = DelegatedLoop(store=MemoryStore(), model='m', registry=ToolRegistry(),
                         claude_mod_compaction=False)
    await loop._ensure_session()
    await loop._start_bundled_server()
    assert not loop._compaction_route
    await loop.aclose()
    loop = DelegatedLoop(store=MemoryStore(), model='m', registry=ToolRegistry())
    await loop._ensure_session()
    await loop._start_bundled_server()
    try:
        assert loop._compaction_route
        assert loop.ctx.extra['history_recall'].session_id == loop.session_id
        assert (await request(loop._socket_path, {'v': 2, 'messages': []}, token=loop._mod_token))[0] == 400
    finally:
        await loop.aclose()
