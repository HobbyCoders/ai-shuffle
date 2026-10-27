"""Follow-ups to Fluid's mechanical compaction of Claude Code chats.

- #679: after a compaction Claude Code prints the conversation it now holds as
  ordinary frames with fresh uuids. Those rows are history, not new calls:
  none is stored again or counted again as tool activity.
- #727: a row dropped by an answer Claude Code never applied is archived once,
  and HistorySearch does not decode every archived entry on every search.
- #685: a mechanical compaction persists a portable checkpoint, so a re-seed
  on another worker does not replay the whole conversation.
"""
from __future__ import annotations

import asyncio
import itertools
import json
import re
import time
from types import SimpleNamespace
from typing import Any

from fluid.context.recall import HistoryRecall, expand_ref, recall_nodes
from fluid.engine.claude_mod_compaction import (
    COMPACT_PATH,
    ReplayFilter,
    archive_ref,
    compact_messages,
    project,
    validate_rows,
)
from fluid.engine.delegated import DelegatedLoop
from fluid.engine.delegated_proto import StreamJsonMapper
from fluid.events import CompactionCheckpoint, ContextArchived, ContextCompaction, ToolResult, ToolUse
from fluid.messages import Message, Text, ToolResultBlock, ToolUseBlock
from fluid.store.base import MemoryStore, SessionNode, iter_events_of_class
from fluid.tools.builtins import register_builtins
from fluid.tools.builtins.history_expand import handler as expand_tool
from fluid.tools.builtins.history_search import handler as search_tool
from fluid.tools.registry import ToolCtx, ToolRegistry


class Chat:
    """A stored path and the rows Claude Code holds for it."""

    def __init__(self) -> None:
        self.stored: list[SessionNode] = []
        self.cli: list[dict] = []
        self._ids = itertools.count()

    def add(self, message: Message) -> dict:
        uuid = f'u{next(self._ids)}'
        message.provider_meta['claude_uuid'] = uuid
        node = SessionNode(f'node_{len(self.stored):012x}',
                           self.stored[-1].node_id if self.stored else None, message)
        self.stored.append(node)
        row = {**project(message), 'handle': uuid}
        self.cli.append(row)
        return row

    def grow(self, turns: int, *, start: int = 0) -> None:
        for t in range(start, start + turns):
            if t % 3 == 0:  # Claude Code's own row, never stored
                self.cli.append({'role': 'user', 'toolUses': [], 'handle': f'att{next(self._ids)}',
                                 'text': f'<system-reminder>attachment {t} ' + 'a' * 800 + '</system-reminder>'})
            self.add(Message('user', [Text(f'Request {t}: keep the migration going.')]))
            for g in range(2):
                tid = f't{t}-{g}'
                self.add(Message('assistant', [ToolUseBlock(tid, 'Bash', {'command': f'step {t} {g}'})]))
                self.add(Message('user', [ToolResultBlock(tid, [Text(f'OUT-{tid} ' + 'x' * 4000)])]))
            self.add(Message('assistant', [Text(f'Done with {t}. ' + 'y' * 300)]))


async def _loop_with(chat: Chat) -> tuple[DelegatedLoop, str]:
    registry = ToolRegistry()
    register_builtins(registry)
    store = MemoryStore()
    loop = DelegatedLoop(store=store, model='m', registry=registry, compaction_window=200_000)
    meta = await loop._ensure_session()
    for node in chat.stored:
        await store.append(meta.session_id, node)
    await loop._start_bundled_server()
    return loop, meta.session_id


async def _request(loop: DelegatedLoop, body: dict) -> tuple[int, Any]:
    address = loop._socket_path
    if address.startswith('tcp:'):
        host, _, port = address[4:].rpartition(':')
        reader, writer = await asyncio.open_connection(host, int(port))
    else:
        reader, writer = await asyncio.open_unix_connection(address)
    raw = json.dumps(body).encode()
    writer.write((f'POST {COMPACT_PATH} HTTP/1.1\r\nHost: localhost\r\n'
                  f'Authorization: Bearer {loop._mod_token}\r\n'
                  f'Content-Type: application/json\r\nContent-Length: {len(raw)}\r\n\r\n').encode() + raw)
    await writer.drain()
    data = await asyncio.wait_for(reader.read(), 60)
    writer.close()
    await writer.wait_closed()
    head, _, content = data.partition(b'\r\n\r\n')
    return int(head.split()[1]), json.loads(content) if content else None


def _frame(row: dict, uuid: str) -> dict:
    """A row as Claude Code prints it on stdout after a compaction: an
    ordinary message frame with a fresh uuid."""
    if row['role'] == 'assistant':
        content: Any = ([{'type': 'text', 'text': row['text']}] if row['text'] else []) + [
            {'type': 'tool_use', 'id': u['tool_use_id'], 'name': u['tool'], 'input': u['input']}
            for u in row['toolUses']]
        return {'type': 'assistant', 'uuid': uuid, 'session_id': 'cli', 'parent_tool_use_id': None,
                'message': {'id': f'msg_{uuid}', 'role': 'assistant', 'model': 'claude-x',
                            'content': content}}
    results = row.get('toolResults', [])
    content = ([{'type': 'tool_result', 'tool_use_id': r['tool_use_id'],
                 'content': [{'type': 'text', 'text': r['text']}], 'is_error': r.get('isError', False)}
                for r in results] if results else row['text'])
    return {'type': 'user', 'uuid': uuid, 'session_id': 'cli', 'parent_tool_use_id': None,
            'message': {'role': 'user', 'content': content}}


async def _read(loop: DelegatedLoop, sid: str, frames: list[dict]) -> list:
    """Feed ``frames`` through the turn's real frame loop; the events it emitted."""
    emitted: list = []
    original = loop._emit

    async def emit(event: Any) -> Any:
        emitted.append(event)
        return await original(event)

    async def no_snapshot(*_: Any, **__: Any) -> None:
        return None

    lines = [json.dumps(f).encode() for f in frames]

    async def next_line() -> bytes:
        return lines.pop(0) if lines else b''

    loop._proc = SimpleNamespace(stdout=object())
    loop._emit = emit  # type: ignore[method-assign]
    loop._ask_usage_snapshot = no_snapshot  # type: ignore[method-assign]
    loop._next_stdout_line = next_line  # type: ignore[method-assign]
    path = await loop.store.load_path(sid)
    mapper = StreamJsonMapper(session_id=sid, turn_id='t1', model='m', user_node_id=path[-1].node_id)
    try:
        await loop._read_until_result(sid, 't1', mapper, path[-1])
    finally:
        loop._emit = original  # type: ignore[method-assign]
        loop._proc = None
    return emitted


COMPACTING = {'type': 'system', 'subtype': 'status', 'status': 'compacting'}
BOUNDARY = {'type': 'system', 'subtype': 'compact_boundary',
            'compact_metadata': {'trigger': 'manual', 'pre_tokens': 90_000, 'post_tokens': 20_000}}


# ─── #679: Claude Code's replay is not new activity ──────────────────────────


async def test_rows_claude_code_replays_after_a_compaction_are_not_stored_again():
    """The issue's shape: every tool id the answer carries comes back once
    per compaction under a fresh uuid. None of it is a new node or a new
    ToolUse/ToolResult; the turn's real new work still is."""
    chat = Chat()
    chat.grow(24)
    loop, sid = await _loop_with(chat)
    try:
        status, body = await _request(loop, {'v': 1, 'trigger': 'manual', 'messages': chat.cli})
        assert status == 200
        answer = body['messages']
        replayed_ids = {u['tool_use_id'] for r in answer for u in r['toolUses']}
        assert replayed_ids, 'the answer must carry tool calls for the test to mean anything'
        stored_before = len(await loop.store.load_path(sid))
        fresh = [
            {'type': 'assistant', 'uuid': 'new-1', 'session_id': 'cli', 'message': {
                'id': 'msg_new', 'role': 'assistant', 'model': 'claude-x',
                'content': [{'type': 'tool_use', 'id': 'fresh-1', 'name': 'Bash',
                             'input': {'command': 'ls'}}]}},
            {'type': 'user', 'uuid': 'new-2', 'session_id': 'cli', 'message': {
                'role': 'user', 'content': [{'type': 'tool_result', 'tool_use_id': 'fresh-1',
                                             'content': 'a.py', 'is_error': False}]}},
            {'type': 'assistant', 'uuid': 'new-3', 'session_id': 'cli', 'message': {
                'id': 'msg_new2', 'role': 'assistant', 'model': 'claude-x',
                'content': [{'type': 'text', 'text': 'All done.'}]}},
        ]
        uuids = (f'replay-{n}' for n in itertools.count())
        frames = [COMPACTING, BOUNDARY, *(_frame(r, next(uuids)) for r in answer), *fresh]
        emitted = await _read(loop, sid, frames)

        path = await loop.store.load_path(sid)
        assert len(path) == stored_before + 3
        assert [b.id for n in path[-3:] for b in n.message.content if isinstance(b, ToolUseBlock)] == ['fresh-1']
        assert [e.tool_use_id for e in emitted if isinstance(e, ToolUse)] == ['fresh-1']
        assert [e.tool_use_id for e in emitted if isinstance(e, ToolResult)] == ['fresh-1']
        # Every tool id is stored exactly once, call and result.
        calls = [b.id for n in path for b in n.message.content if isinstance(b, ToolUseBlock)]
        results = [b.tool_use_id for n in path for b in n.message.content if isinstance(b, ToolResultBlock)]
        assert len(calls) == len(set(calls)) and len(results) == len(set(results))
    finally:
        await loop.aclose()


async def test_a_replay_split_differently_is_still_recognised_by_its_tool_ids():
    """However Claude Code splits or words the replayed rows (one call per
    frame, a result shown in another form), a frame made only of recorded
    tool ids is never fresh execution."""
    chat = Chat()
    chat.grow(3)
    filter_ = ReplayFilter()
    filter_.seed(chat.stored)
    call = {'type': 'assistant', 'uuid': 'x1', 'message': {'role': 'assistant', 'content': [
        {'type': 'tool_use', 'id': 't1-0', 'name': 'Bash', 'input': {'command': 'other wording'}}]}}
    result = {'type': 'user', 'uuid': 'x2', 'message': {'role': 'user', 'content': [
        {'type': 'tool_result', 'tool_use_id': 't1-0', 'content': '[trimmed]'}]}}
    assert filter_.is_replay(call) and filter_.is_replay(result)
    # A frame with a new id among recorded ones is new work.
    mixed = {'type': 'assistant', 'uuid': 'x3', 'message': {'role': 'assistant', 'content': [
        {'type': 'tool_use', 'id': 't1-0', 'name': 'Bash', 'input': {}},
        {'type': 'tool_use', 'id': 'brand-new', 'name': 'Bash', 'input': {}}]}}
    assert not filter_.is_replay(mixed)
    # A subagent's frames are never the main conversation's replay.
    assert not filter_.is_replay({**call, 'parent_tool_use_id': 'toolu_parent'})


def test_the_replay_window_closes_at_the_next_model_call():
    """Text rows of the answer are a replay only until Claude Code calls the
    model again: a later reply with the same words is the model's."""
    filter_ = ReplayFilter()
    filter_.seed([])
    row = {'role': 'assistant', 'text': 'Done.', 'toolUses': []}
    frame = _frame(row, 'r1')
    filter_.expect([row])
    assert filter_.is_replay(frame)
    filter_.expect([row])
    assert not filter_.is_replay({'type': 'stream_event', 'event': {'type': 'message_start'}})
    assert not filter_.is_replay(_frame(row, 'r2'))
    # Any assistant frame outside the answer also ends it.
    filter_.expect([row])
    assert not filter_.is_replay(_frame({'role': 'assistant', 'text': 'Other.', 'toolUses': []}, 'r3'))
    assert not filter_.is_replay(_frame(row, 'r4'))


def test_nothing_is_filtered_before_a_compaction_was_answered():
    filter_ = ReplayFilter()
    message = Message('assistant', [ToolUseBlock('t-1', 'Bash', {})])
    filter_.note(message)
    frame = _frame(project(message), 'u-1')
    assert not filter_.is_replay(frame)
    filter_.seed([])
    assert filter_.is_replay(frame)


# ─── #727: archive once; bounded archive reads ───────────────────────────────


async def _archived_entries(loop: DelegatedLoop, sid: str) -> list[dict]:
    events = [e async for e in iter_events_of_class(loop.store, sid, 0, (ContextArchived,))]
    return [entry for event in events for entry in event.entries]


async def test_a_compaction_claude_code_never_applied_is_not_archived_twice():
    """The mod's wait ran out, so Claude Code kept its rows and compacts the
    same ones again: the second answer cites the same refs and writes no
    second copy. HistorySearch lists each archived row once."""
    chat = Chat()
    chat.grow(30)
    loop, sid = await _loop_with(chat)
    try:
        status, first = await _request(loop, {'v': 1, 'trigger': 'manual', 'messages': chat.cli})
        assert status == 200
        once = await _archived_entries(loop, sid)
        assert once and len({e['node_id'] for e in once}) == len(once)
        # Claude Code never applied it: the same rows come again.
        status, second = await _request(loop, {'v': 1, 'trigger': 'manual', 'messages': chat.cli})
        assert status == 200
        assert await _archived_entries(loop, sid) == once
        note = [r['text'] for r in second['messages'] if 'Fluid compaction note' in r['text']]
        refs = set(re.findall(r'HistoryExpand\("(node_[0-9a-f]{24})"\)', note[0]))
        assert refs and refs <= {e['node_id'] for e in once}
        ctx = ToolCtx(extra={'history_recall': loop.ctx.extra['history_recall']})
        found = await search_tool({'query': 'attachment 3', 'max_results': 50}, ctx)
        assert found.text.count('attachment 3 ') == 1
    finally:
        await loop.aclose()


def test_archive_refs_are_content_derived_and_never_a_stored_id():
    assert archive_ref('user', 'same text') == archive_ref('user', 'same text')
    assert archive_ref('user', 'same text') != archive_ref('assistant', 'same text')
    assert re.fullmatch(r'node_[0-9a-f]{24}', archive_ref('user', 'x'))
    rows = [{'role': 'user', 'text': 'skill body', 'toolUses': [], 'handle': 'h1'}]
    archive: list[dict] = []
    from fluid.engine.claude_mod_compaction import _note_row
    _note_row(rows + [dict(rows[0], handle='h2')], [], archive)
    assert len(archive) == 1  # the same text dropped twice in one answer


class CountingStore(MemoryStore):
    """A store that counts the events it hands out (what a search decodes)."""

    def __init__(self) -> None:
        super().__init__()
        self.read = 0

    async def events_since(self, session_id: str, seq: int):
        events = await super().events_since(session_id, seq)
        self.read += len(events)
        return events


async def test_history_search_reads_only_new_archive_events():
    """1,200 archived entries: the first search decodes them once; later
    searches decode only archive events written since, so a search's cost
    no longer grows with the session's archive."""
    store = CountingStore()
    sid = (await store.create_session()).session_id
    await store.append(sid, SessionNode('node_000000000001', None, Message('user', [Text('hello')])))
    seq = itertools.count(1)
    for batch in range(40):
        await store.append_event(sid, ContextArchived(
            session_id=sid, seq=next(seq),
            entries=tuple({'node_id': archive_ref('user', f'row {batch}-{n}'), 'role': 'user',
                           'text': f'row {batch}-{n} needle-{batch}-{n}'} for n in range(30)),
        ))
    recall = HistoryRecall(store, sid)
    ctx = ToolCtx(extra={'history_recall': recall})
    found = await search_tool({'query': 'needle-39-29'}, ctx)
    assert 'needle-39-29' in found.text
    assert store.read == 40

    timings = []
    for _ in range(5):
        store.read = 0
        started = time.perf_counter()
        found = await search_tool({'query': 'needle-0-0'}, ctx)
        timings.append(time.perf_counter() - started)
        assert 'needle-0-0' in found.text
        assert store.read == 0

    await store.append_event(sid, ContextArchived(
        session_id=sid, seq=next(seq),
        entries=({'node_id': archive_ref('user', 'late'), 'role': 'user', 'text': 'late needle-new'},),
    ))
    store.read = 0
    found = await search_tool({'query': 'needle-new'}, ctx)
    assert 'needle-new' in found.text
    assert store.read == 1
    assert len(await recall.archived()) == 1201


async def test_duplicates_archived_before_the_fix_are_listed_once_and_both_refs_expand():
    """Entries an older build archived twice under random ids: search shows
    one, and the ref the older note cites still expands."""
    store = MemoryStore()
    sid = (await store.create_session()).session_id
    await store.append(sid, SessionNode('node_000000000001', None, Message('user', [Text('hello')])))
    for n, ref in enumerate(('node_aaaaaaaaaaaa', 'node_bbbbbbbbbbbb'), start=1):
        await store.append_event(sid, ContextArchived(session_id=sid, seq=n, entries=(
            {'node_id': ref, 'role': 'user', 'text': 'SKILL-BODY old skill'},)))
    recall = HistoryRecall(store, sid)
    ctx = ToolCtx(extra={'history_recall': recall})
    found = await search_tool({'query': 'SKILL-BODY'}, ctx)
    assert found.text.startswith('1 match(es)') and 'node_bbbbbbbbbbbb' not in found.text
    for ref in ('node_aaaaaaaaaaaa', 'node_bbbbbbbbbbbb'):
        read = await expand_tool({'ref': ref}, ctx)
        assert not read.is_error and 'SKILL-BODY old skill' in read.text
        nodes = await recall_nodes(recall, ref)
        assert expand_ref(nodes, ref, max_chars=1000)[1] is False


# ─── #685: a portable checkpoint ─────────────────────────────────────────────


def test_the_checkpoint_stands_for_the_stored_path_through_its_node():
    """The checkpoint's rows validate on their own, carry no handles, cover
    every stored node up to ``through`` (verbatim or by a recall ref) and
    repeat no tool id stored after it; replaying them plus the nodes after
    ``through`` is far smaller than the whole history."""
    chat = Chat()
    chat.grow(40)
    checkpoint: dict = {}
    out = compact_messages([dict(r) for r in chat.cli], chat.stored, archive=[], checkpoint=checkpoint)
    assert out and checkpoint
    rows = checkpoint['rows']
    validate_rows(rows, handles=False)
    assert not any('handle' in r for r in rows)
    index = {n.node_id: i for i, n in enumerate(chat.stored)}
    through = index[checkpoint['through']]
    assert 0 < through < len(chat.stored) - 1

    text = json.dumps(rows)
    covered: set[int] = set()
    for ref in re.findall(r'HistoryExpand\("(node_[0-9a-f]+(?:\.\.node_[0-9a-f]+)?)"\)', text):
        first, _, last = ref.partition('..')
        if first in index:
            covered.update(range(index[first], index[last or first] + 1))
    for match in re.finditer(r'\[(?:compacted turn|epoch) (node_[0-9a-f]+)(?:\.\.(node_[0-9a-f]+))?', text):
        covered.update(range(index[match.group(1)], index[match.group(2) or match.group(1)] + 1))
    signatures = {json.dumps({k: v for k, v in r.items() if k != 'handle'}, sort_keys=True) for r in rows}
    for i, node in enumerate(chat.stored[: through + 1]):
        verbatim = json.dumps(project(node.message), sort_keys=True) in signatures
        ids = {b.id if isinstance(b, ToolUseBlock) else b.tool_use_id for b in node.message.content
               if isinstance(b, (ToolUseBlock, ToolResultBlock))}
        by_id = bool(ids) and ids <= {u['tool_use_id'] for r in rows for u in r['toolUses']} | {
            x['tool_use_id'] for r in rows for x in r.get('toolResults', [])}
        assert i in covered or verbatim or by_id, f'stored node {i} lost from the checkpoint'
    later_ids = {b.id for n in chat.stored[through + 1:] for b in n.message.content
                 if isinstance(b, ToolUseBlock)}
    assert not later_ids & {u['tool_use_id'] for r in rows for u in r['toolUses']}
    portable = len(text) + len(json.dumps([project(n.message) for n in chat.stored[through + 1:]]))
    whole = len(json.dumps([project(n.message) for n in chat.stored]))
    assert portable < whole / 3


async def test_the_checkpoint_is_written_when_claude_code_applies_the_answer():
    chat = Chat()
    chat.grow(24)
    loop, sid = await _loop_with(chat)
    try:
        status, body = await _request(loop, {'v': 1, 'trigger': 'manual', 'messages': chat.cli})
        assert status == 200
        emitted = await _read(loop, sid, [COMPACTING, BOUNDARY])
        kinds = [type(e) for e in emitted if isinstance(e, (ContextCompaction, CompactionCheckpoint))]
        assert kinds == [ContextCompaction, CompactionCheckpoint]
        [checkpoint] = [e for e in emitted if isinstance(e, CompactionCheckpoint)]
        assert checkpoint.through in {n.node_id for n in chat.stored}
        validate_rows(list(checkpoint.rows), handles=False)
        stored = [e async for e in iter_events_of_class(loop.store, sid, 0, (CompactionCheckpoint,))]
        assert len(stored) == 1 and stored[0].through == checkpoint.through
        # A second boundary with no new answer writes nothing more.
        emitted = await _read(loop, sid, [COMPACTING, BOUNDARY])
        assert not [e for e in emitted if isinstance(e, CompactionCheckpoint)]
    finally:
        await loop.aclose()


async def test_no_checkpoint_for_an_answer_claude_code_did_not_apply(monkeypatch):
    """An answer older than the mod's wait was not what the boundary
    applied (it fell back, or a later compaction ran), so nothing is written."""
    from fluid.engine import delegated

    chat = Chat()
    chat.grow(24)
    loop, sid = await _loop_with(chat)
    try:
        status, _ = await _request(loop, {'v': 1, 'trigger': 'manual', 'messages': chat.cli})
        assert status == 200
        answered, checkpoint = loop._pending_checkpoint
        loop._pending_checkpoint = (answered - delegated.CHECKPOINT_APPLY_WINDOW_S - 1, checkpoint)
        emitted = await _read(loop, sid, [COMPACTING, BOUNDARY])
        assert [type(e) for e in emitted if isinstance(e, ContextCompaction)] == [ContextCompaction]
        assert not [e for e in emitted if isinstance(e, CompactionCheckpoint)]
    finally:
        await loop.aclose()
