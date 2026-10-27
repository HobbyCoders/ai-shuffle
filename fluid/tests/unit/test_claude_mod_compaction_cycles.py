"""Repeated mechanical compaction of a delegated (Claude Code) chat.

A long chat compacts many times. Each answer becomes Claude Code's
conversation, so the next compaction reads Fluid's own digests, epochs,
rebuilt rows, notes and separators back. These tests drive that loop: the
conversation grows, compacts, and the "CLI" takes the answer (built rows get
fresh handles, as Claude Code gives them), sometimes after a respawn whose
handles no longer match the stored uuids. Every cycle must reclaim, the
retained size must stay bounded however long the chat runs, and what left the
view must stay readable through HistoryExpand.
"""
from __future__ import annotations

import asyncio
import itertools
import json
import re

import pytest
from fluid.context.recall import expand_ref, parse_ref
from fluid.engine.claude_mod_compaction import (
    COMPACT_PATH,
    NOTE_EXCERPT_CHARS,
    SPACER_TEXT,
    NothingToCompact,
    compact_messages,
    project,
)
from fluid.engine.delegated import NOTHING_TO_COMPACT, DelegatedLoop
from fluid.engine.delegated_proto import StreamJsonMapper
from fluid.messages import Image, Message, Text, Thinking, ToolResultBlock, ToolUseBlock
from fluid.store.base import MemoryStore, SessionNode
from fluid.tools.builtins import register_builtins
from fluid.tools.registry import ToolRegistry


class Chat:
    """A stored path and the rows Claude Code holds for it."""

    def __init__(self, *, result_chars: int = 5_000):
        self.stored: list[SessionNode] = []
        self.cli: list[dict] = []
        self.turn = 0
        self.result_chars = result_chars
        self._ids = itertools.count()

    def _add(self, message: Message) -> dict:
        uuid = f'u{next(self._ids)}'
        message.provider_meta['claude_uuid'] = uuid
        node = SessionNode(f'node_{len(self.stored):012x}',
                           self.stored[-1].node_id if self.stored else None, message)
        self.stored.append(node)
        return {**project(message), 'handle': uuid}

    def grow(self, turns: int) -> None:
        """Turns with parallel tool groups listed both ways, an error result
        now and then, and Claude Code attachments the store never sees."""
        for _ in range(turns):
            t = self.turn
            self.turn += 1
            if t % 3 == 0:
                self.cli.append({'role': 'user', 'toolUses': [], 'handle': f'att{next(self._ids)}',
                                 'text': f'<system-reminder>attachment {t} ' + 'a' * 800 + '</system-reminder>'})
            self.cli.append(self._add(Message('user', [Text(f'Request {t}: REQ-{t} keep the migration going.')])))
            for g in range(3):
                ids = [f't{t}-{g}-{k}' for k in range(2)]
                calls = [self._add(Message('assistant', [ToolUseBlock(i, 'Read', {'file_path': f'{i}.py'})]))
                         for i in ids]
                results = []
                for k, i in enumerate(ids):
                    error = t % 4 == 1 and g == 0 and k == 0
                    body = 'E' * 300 if error else 'x' * self.result_chars
                    results.append(self._add(Message('user', [ToolResultBlock(
                        i, [Text(f'RESULT-{i} {body}')], is_error=error)])))
                self.cli += ([calls[0], calls[1], results[0], results[1]] if g % 2
                             else [calls[0], results[0], calls[1], results[1]])
            self.cli.append(self._add(Message('assistant', [Text(f'Done with {t}. ' + 'y' * 400)])))

    def compact(self, trigger: str = 'manual', window: int = 200_000) -> list[dict]:
        posted = [dict(r) for r in self.cli]
        out = compact_messages(posted, self.stored, context_window=window, trigger=trigger)
        assert_mod_accepts(out, posted)
        # Claude Code keeps a handled row as it is and builds the rest anew.
        self.cli = [r if 'handle' in r else {**r, 'handle': f'built{next(self._ids)}'} for r in out]
        return out

    def respawn(self) -> None:
        """A resumed CLI whose handles no longer match the stored uuids."""
        self.cli = [{**r, 'handle': f'resumed{next(self._ids)}'} for r in self.cli]

    def size(self) -> int:
        return len(json.dumps(self.cli))


def assert_mod_accepts(output, originals):
    """What the mod's compactedRows (hooks/mechanical.ts) demands of an answer."""
    by_handle = {r['handle']: json.dumps(r) for r in originals}
    calls = {u['tool_use_id'] for r in originals for u in r['toolUses']}
    assert output[0]['role'] == 'user'
    pending = set()
    for previous, row in zip([None] + output, output):
        if pending:
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


def recoverable(nodes, rows, needle):
    """``needle`` is in a stored node that a HistoryExpand ref in ``rows``
    covers, read back verbatim through it."""
    index = {n.node_id: i for i, n in enumerate(nodes)}
    for row in rows:
        for ref in re.findall(r'HistoryExpand\("([^"]+)"', row['text']):
            start, end = parse_ref(ref)
            for node in nodes[index[start]:index[end] + 1]:
                text, error = expand_ref(nodes, node.node_id, max_chars=10**6)
                if not error and needle in text:
                    return True
    return False


def test_every_cycle_reclaims_and_the_retained_size_stays_bounded():
    chat = Chat()
    retained = []
    for cycle in range(16):
        chat.grow(8)
        if cycle in (5, 11):
            chat.respawn()
        before = chat.size()
        chat.compact('auto' if cycle % 2 else 'manual')
        assert chat.size() < before
        retained.append(chat.size())
    raw = sum(len(json.dumps(project(n.message))) for n in chat.stored)
    # The stored history keeps growing; what Claude Code holds does not.
    assert raw > 3_000_000
    assert max(retained) < 260_000
    assert max(retained[8:]) <= max(retained[:8]) * 1.1
    text = '\n'.join(r['text'] for r in chat.cli)
    # Fluid's own rows were folded again, not kept as barriers: one epoch,
    # one bounded note, no pile of separators.
    assert text.count('[epoch ') <= 1
    assert sum(r['text'] == SPACER_TEXT for r in chat.cli) <= 3
    [note] = [r for r in chat.cli if 'Fluid compaction note' in r['text']]
    assert len(note['text'].split('\n\n')[0]) < NOTE_EXCERPT_CHARS + 600


def test_what_left_the_view_in_early_cycles_reads_back_exactly():
    chat = Chat()
    for cycle in range(12):
        chat.grow(8)
        if cycle == 5:
            chat.respawn()
        chat.compact()
    held = json.dumps(chat.cli)
    for needle in ('RESULT-t0-0-1 ' + 'x' * 5_000, 'RESULT-t1-0-0 ' + 'E' * 300, 'Done with 3. '):
        assert needle not in held
        assert recoverable(chat.stored, chat.cli, needle), needle
    # The original request is pinned in the oldest fold.
    assert 'REQ-0' in held


def test_the_recent_tail_stays_verbatim_with_its_handles():
    chat = Chat()
    for _ in range(6):
        chat.grow(8)
        chat.compact()
    chat.grow(1)
    prompt = next(r for r in reversed(chat.cli) if r['text'].startswith('Request '))
    newest = list(chat.cli[-5:])  # the newest tool group and the answer
    chat.compact()
    # The tail is the conversation's newest tokens (not 3 turns per fragment):
    # the newest group, the answer and the turn's request keep their handles.
    assert chat.cli[-5:] == newest
    assert prompt in chat.cli


def test_legacy_separators_and_unmatched_rows_do_not_become_barriers():
    chat = Chat()
    chat.grow(10)
    chat.compact()
    # What the earlier implementation left behind: separator rows after
    # Fluid's built user rows, which no stored node matches.
    rows = []
    for row in chat.cli:
        rows.append(row)
        if 'handle' in row and row['handle'].startswith('built') and row['role'] == 'user' \
                and not row.get('toolResults'):
            rows.append({'role': 'assistant', 'text': SPACER_TEXT, 'toolUses': [],
                         'handle': f'legacy{len(rows)}'})
            rows.append({'role': 'user', 'text': SPACER_TEXT, 'toolUses': [],
                         'handle': f'legacy{len(rows)}'})
    chat.cli = rows
    chat.grow(10)
    chat.compact()
    assert sum(r['text'] == SPACER_TEXT for r in chat.cli) <= 3
    [note] = [r for r in chat.cli if 'Fluid compaction note' in r['text']]
    assert SPACER_TEXT not in note['text']


def test_nothing_left_to_reclaim_is_a_no_op_and_auto_needs_real_room():
    chat = Chat(result_chars=100)
    chat.grow(3)
    with pytest.raises(NothingToCompact):
        chat.compact('manual')
    with pytest.raises(NothingToCompact):
        chat.compact('auto')
    # Compacting again and again ends in a no-op, never in growth.
    chat = Chat()
    chat.grow(12)
    sizes = [chat.size()]
    for _ in range(6):
        try:
            chat.compact('manual')
        except NothingToCompact:
            break
        sizes.append(chat.size())
    else:
        pytest.fail('repeated manual compactions never reached a fixed point')
    assert sizes == sorted(sizes, reverse=True)


async def _request(address, body, token):
    if address.startswith('tcp:'):
        host, _, port = address[4:].rpartition(':')
        reader, writer = await asyncio.open_connection(host, int(port))
    else:
        reader, writer = await asyncio.open_unix_connection(address)
    raw = json.dumps(body).encode()
    writer.write((f'POST {COMPACT_PATH} HTTP/1.1\r\nHost: localhost\r\nAuthorization: Bearer {token}\r\n'
                  f'Content-Type: application/json\r\nContent-Length: {len(raw)}\r\n\r\n').encode() + raw)
    await writer.drain()
    data = await asyncio.wait_for(reader.read(), 30)
    writer.close()
    await writer.wait_closed()
    head, _, content = data.partition(b'\r\n\r\n')
    return int(head.split()[1]), json.loads(content) if content else None


async def _loop_with(chat: Chat):
    registry = ToolRegistry()
    register_builtins(registry)
    store = MemoryStore()
    loop = DelegatedLoop(store=store, model='m', registry=registry, compaction_window=200_000)
    meta = await loop._ensure_session()
    for node in chat.stored:
        await store.append(meta.session_id, node)
    await loop._start_bundled_server()
    return loop, meta.session_id


async def test_the_bridge_answers_a_manual_no_op_and_the_turn_says_so_plainly():
    chat = Chat(result_chars=100)
    chat.grow(3)
    loop, sid = await _loop_with(chat)
    events = []

    async def emit(event):
        events.append(event)

    try:
        status, body = await _request(loop._socket_path, {'v': 1, 'trigger': 'manual', 'messages': chat.cli},
                                      loop._mod_token)
        assert (status, body) == (200, {'skip': NOTHING_TO_COMPACT})
        assert loop._compact_noop
        # The /compact turn ends without a boundary: a no-op card, not a failure.
        loop._emit = emit
        loop._turn_compact_requested, loop._turn_compactions = True, 0
        mapper = StreamJsonMapper(session_id=sid, turn_id='turn-c', model='m', user_node_id=None)
        await loop._report_skipped_compaction(sid, 'turn-c', mapper)
        [diagnostic] = events
        assert diagnostic.kind == 'compaction_failed'
        assert diagnostic.message == NOTHING_TO_COMPACT
        assert diagnostic.data == {'nothing_to_compact': True}
    finally:
        await loop.aclose()


async def test_the_bridge_lets_an_automatic_compaction_fall_back():
    chat = Chat(result_chars=100)
    chat.grow(3)
    loop, _ = await _loop_with(chat)
    try:
        status, body = await _request(loop._socket_path, {'v': 1, 'trigger': 'auto', 'messages': chat.cli},
                                      loop._mod_token)
        # The mod answers this skip with next(e) for an automatic compaction.
        assert (status, body) == (200, {'skip': NOTHING_TO_COMPACT})
        assert not loop._compact_noop
    finally:
        await loop.aclose()


async def test_the_bridge_compacts_a_long_chat_with_the_host_window():
    chat = Chat()
    chat.grow(24)
    loop, _ = await _loop_with(chat)
    try:
        status, body = await _request(loop._socket_path, {'v': 1, 'trigger': 'manual', 'messages': chat.cli},
                                      loop._mod_token)
        assert status == 200
        assert len(json.dumps(body['messages'])) < chat.size() // 2
        assert not loop._compact_noop
    finally:
        await loop.aclose()


def test_a_long_running_chat_reaches_a_plateau():
    """Forty grow-and-compact cycles: once the oldest fold's bounded parts
    (the pinned requests, the note) are full, the retained size stops rising."""
    chat = Chat(result_chars=2_000)
    retained = []
    for _ in range(40):
        chat.grow(5)
        chat.compact()
        retained.append(chat.size())
    # No ratchet: the second half never holds more than the first did.
    assert max(retained[20:]) <= max(retained[:20])


def test_a_long_first_compaction_folds_deep_history_at_once():
    """A long chat compacted for the first time folds its old history into one
    epoch directly instead of digesting it span by span (which re-renders the
    whole path per span), and its oldest results stay recoverable."""
    import time

    chat = Chat(result_chars=1_000)
    chat.grow(250)
    started = time.monotonic()
    chat.compact('auto')
    assert time.monotonic() - started < 20
    assert '\n'.join(r['text'] for r in chat.cli).count('[epoch ') == 1
    assert recoverable(chat.stored, chat.cli, 'RESULT-t0-0-0 ' + 'x' * 1_000)


def test_a_rebuilt_parallel_group_stays_stored_not_noted():
    """A parallel group Fluid rebuilt as one call row and one result row
    matches no single node next time, but every tool id it carries is stored:
    it leaves the view with a recall ref, never through the note."""
    chat = Chat()
    for _ in range(4):
        chat.grow(8)
        chat.compact()
    [note] = [r for r in chat.cli if 'Fluid compaction note' in r['text']]
    assert '[calls:' not in note['text'] and 'tool result(s)' not in note['text']
    assert recoverable(chat.stored, chat.cli, 'RESULT-t9-1-1 ' + 'x' * 5_000)


# ── Review fixes: nothing a compaction evicts may become unreachable ─────────


def _held_or_named(chat: Chat, rows: list[dict]) -> list[int]:
    """Stored indexes neither held (a row equal to the node's projection, or
    its tool ids in a row) nor inside a range a row names (a HistoryExpand
    ref, a digest or epoch header)."""
    from fluid.engine.claude_mod_compaction import _signature

    index = {n.node_id: i for i, n in enumerate(chat.stored)}
    named: set[int] = set()
    for row in rows:
        refs = re.findall(r'HistoryExpand\("([^"]+)"', row['text'])
        refs += [m.group(1) + ('..' + m.group(2) if m.group(2) else '') for m in
                 re.finditer(r'\[(?:compacted turn|epoch) (\S+?)(?:\.\.(\S+?))? ', row['text'])]
        for ref in refs:
            parsed = parse_ref(ref)
            if parsed and parsed[0] in index and parsed[1] in index:
                assert not expand_ref(chat.stored, ref, max_chars=10**7)[1], ref
                named.update(range(index[parsed[0]], index[parsed[1]] + 1))
    held = {_signature(r) for r in rows}
    ids = ({u['tool_use_id'] for r in rows for u in r['toolUses']}
           | {x['tool_use_id'] for r in rows for x in r.get('toolResults', [])})
    lost = []
    for k, node in enumerate(chat.stored):
        if k in named or _signature(project(node.message)) in held:
            continue
        tools = [b.id for b in node.message.content if isinstance(b, ToolUseBlock)] + [
            b.tool_use_id for b in node.message.content if isinstance(b, ToolResultBlock)]
        if tools and all(t in ids for t in tools):
            continue
        lost.append(k)
    return lost


def test_a_long_paste_survives_repeated_compaction():
    """A request over the stub size renders as a stub naming its node. The
    stub must never be merged into Fluid's note or digest row, or the next
    compaction cannot tell it apart and the request falls out of context."""
    chat = Chat()
    chat.cli.append({'role': 'user', 'toolUses': [], 'handle': 'att-0',
                     'text': '<system-reminder>ctx</system-reminder>'})
    chat.cli.append({'role': 'assistant', 'toolUses': [], 'handle': 'att-1', 'text': 'ok'})
    chat.cli.append(chat._add(Message('user', [Text('SPEC-PASTE ' + 's' * 30_000)])))
    chat.cli.append(chat._add(Message('assistant', [Text('Understood.')])))
    paste = chat.stored[0].node_id
    chat.grow(10)
    for _ in range(3):
        chat.compact()
        assert any(paste in r['text'] for r in chat.cli)
        assert not _held_or_named(chat, chat.cli)
        chat.grow(4)
    assert recoverable(chat.stored, chat.cli, 'SPEC-PASTE ' + 's' * 30_000)


def _fuzz_grow(chat: Chat, rng, turns: int) -> None:
    for _ in range(turns):
        t = chat.turn
        chat.turn += 1
        if rng.random() < 0.3:
            chat.cli.append({'role': 'user', 'toolUses': [], 'handle': f'att{next(chat._ids)}',
                             'text': f'<system-reminder>att {t} ' + 'a' * rng.randint(50, 3000)
                                     + '</system-reminder>'})
        size = 30_000 if rng.random() < 0.1 else 60
        chat.cli.append(chat._add(Message('user', [Text(f'Request {t}: ' + 'r' * size)])))
        for g in range(rng.randint(0, 5)):
            ids = [f't{t}-{g}-{k}' for k in range(rng.randint(1, 3))]
            think = rng.random() < 0.5
            calls = [chat._add(Message('assistant', ([Thinking('hmm')] if think and k == 0 else [])
                                       + [ToolUseBlock(i, 'Read', {'p': i})]))
                     for k, i in enumerate(ids)]
            results = []
            for i in ids:
                blocks = [Text(f'RES-{i} ' + 'x' * rng.choice([50, 2000, 9000]))]
                if rng.random() < 0.1:
                    blocks.append(Image(media_type='image/png', data='AAAA'))
                results.append(chat._add(Message('user', [ToolResultBlock(
                    i, blocks, is_error=rng.random() < 0.1)])))
            chat.cli += (calls + results if rng.random() < 0.5
                         else [r for pair in zip(calls, results) for r in pair])
        chat.cli.append(chat._add(Message('assistant', [Text(f'Done {t} ' + 'y' * rng.randint(10, 800))])))


@pytest.mark.parametrize('seed', range(30))
def test_fuzz_no_stored_message_is_ever_lost(seed):
    """Random chats (parallel groups both ways, thinking, images, errors,
    long pastes, Claude Code attachments, respawns, both triggers and
    windows), 12 compactions each: every answer is one the mod accepts, and
    every stored message is still held or named by a ref that reads back."""
    import random

    rng = random.Random(seed)
    chat = Chat()
    for _ in range(12):
        _fuzz_grow(chat, rng, rng.randint(2, 10))
        if rng.random() < 0.15:
            chat.respawn()
        try:
            chat.compact(rng.choice(['manual', 'auto']), window=rng.choice([200_000, 1_000_000]))
        except NothingToCompact:
            continue
        assert not _held_or_named(chat, chat.cli)


def _skill_turn(chat: Chat, name: str, steps: int, *, first: int = 0) -> dict:
    """A request that runs a skill: the prompt (stored), the skill's body
    (Claude Code's own text, never stored) and some heavy tool calls."""
    chat.cli.append(chat._add(Message('user', [Text(f'/{name} run it')])))
    body = {'role': 'user', 'handle': f'body-{name}', 'toolUses': [],
            'text': f'SKILL-BODY {name}: step 1 never push to main; ' + 'S' * 3000}
    chat.cli.append(body)
    _steps(chat, name, first, steps)
    return body


def _steps(chat: Chat, name: str, first: int, last: int) -> None:
    for g in range(first, last):
        tid = f'{name}-{g}'
        chat.cli.append(chat._add(Message('assistant', [ToolUseBlock(tid, 'Bash', {'command': f'{g}'})])))
        chat.cli.append(chat._add(Message('user', [ToolResultBlock(tid, [Text(f'OUT-{tid} ' + 'o' * 3000)])])))


def _note_cites(rows: list[dict], archive: list[dict], marker: str) -> bool:
    """The note names the archived copy of the row that carried ``marker``."""
    refs = {e['node_id'] for e in archive if marker in e['text']}
    return bool(refs) and any(ref in r['text'] for r in rows if 'Fluid compaction note' in r['text']
                              for ref in refs)


RESUME = ("[auto-resume] Your previous turn was stopped by the provider's usage limit, which has now "
          "reset. Continue the task you were working on, picking up exactly where you left off.")
CONTINUE = ("[auto-continue] Your previous turn was cut off at the context limit and the context was "
            "compacted. Continue where you left off.")


@pytest.mark.parametrize('newest', [None, RESUME, CONTINUE, '[hand-off] Moved from worker-a to worker-b',
                                    '<task-notification>bg done</task-notification>', '/compact',
                                    'also update the changelog'])
def test_claude_code_rows_of_the_newest_request_stay_verbatim(newest):
    """A skill body (Claude Code's own text, never stored) is the work in
    hand: it stays verbatim however much compacts after it, through turns the
    host starts on its own (a usage-limit resume, a context continuation, a
    hand-off, a notification, a compaction) and through a short follow-up."""
    chat = Chat()
    chat.grow(6)
    body = _skill_turn(chat, 'deploy-skill', 8)
    if newest is not None:
        chat.cli.append(chat._add(Message('assistant', [Text('API Error: usage limit')])))
        chat.cli.append(chat._add(Message('user', [Text(newest)])))
        _steps(chat, 'deploy-skill', 8, 14)
    for trigger in ('manual', 'auto'):
        archive: list[dict] = []
        out = compact_messages([dict(r) for r in chat.cli], chat.stored, trigger=trigger, archive=archive)
        assert body in out
        assert not any('SKILL-BODY' in e['text'] for e in archive)


def test_only_the_newest_earlier_skill_stays_pinned():
    """A short follow-up keeps the skill it follows in context. A second
    skill request replaces it: the newer skill stays, the older one leaves
    context with its whole text archived behind a real ref."""
    chat = Chat()
    chat.grow(4)
    first = _skill_turn(chat, 'skill-one', 8)
    chat.cli.append(chat._add(Message('assistant', [Text('Deployed.')])))
    chat.cli.append(chat._add(Message('user', [Text('also update the changelog')])))
    _steps(chat, 'follow', 0, 8)
    archive: list[dict] = []
    out = compact_messages([dict(r) for r in chat.cli], chat.stored, archive=archive)
    assert first in out
    assert not any('SKILL-BODY' in e['text'] for e in archive)

    chat.cli.append(chat._add(Message('assistant', [Text('Changelog updated.')])))
    second = _skill_turn(chat, 'skill-two', 8)
    chat.cli.append(chat._add(Message('assistant', [Text('Done.')])))
    chat.cli.append(chat._add(Message('user', [Text('thanks, one more check')])))
    _steps(chat, 'check', 0, 8)
    archive = []
    out = compact_messages([dict(r) for r in chat.cli], chat.stored, archive=archive)
    assert second in out
    assert first not in out
    assert _note_cites(out, archive, 'SKILL-BODY skill-one')
    [entry] = [e for e in archive if 'SKILL-BODY skill-one' in e['text']]
    assert entry['text'] == first['text']


@pytest.mark.parametrize('resume', [RESUME, CONTINUE])
def test_a_host_started_turn_is_not_a_request_of_its_own(resume):
    """Two skill requests, then the host resumes the second: the resume
    continues that request, so both skills stay pinned (counted as a request
    it would push the older skill out)."""
    chat = Chat()
    chat.grow(4)
    first = _skill_turn(chat, 'skill-one', 6)
    chat.cli.append(chat._add(Message('assistant', [Text('Done.')])))
    second = _skill_turn(chat, 'skill-two', 6)
    chat.cli.append(chat._add(Message('assistant', [Text('API Error: usage limit')])))
    chat.cli.append(chat._add(Message('user', [Text(resume)])))
    _steps(chat, 'skill-two', 6, 12)
    out = compact_messages([dict(r) for r in chat.cli], chat.stored, archive=[])
    assert first in out and second in out


def test_host_started_turns_are_the_ones_the_app_sends():
    """The prefixes this module skips are the ones AI Shuffle writes."""
    from fluid.engine.claude_mod_compaction import HOST_TURN_PREFIXES

    for text in (RESUME, CONTINUE, '[hand-off] Moved from worker-a to worker-b'):
        assert text.startswith(HOST_TURN_PREFIXES)


async def test_older_claude_code_rows_are_archived_and_read_back_whole():
    """Claude Code's own rows older than the newest request leave context,
    but their whole text is archived with the session first: the note cites
    a ref that HistoryExpand reads back, and HistorySearch finds it."""
    from fluid.context.recall import HistoryRecall
    from fluid.events.model import ContextArchived
    from fluid.store.base import iter_events_of_class
    from fluid.tools.builtins.history_expand import handler as expand
    from fluid.tools.builtins.history_search import handler as search
    from fluid.tools.registry import ToolCtx

    chat = Chat()
    chat.grow(30)
    loop, sid = await _loop_with(chat)
    try:
        status, body = await _request(loop._socket_path, {'v': 1, 'trigger': 'manual', 'messages': chat.cli},
                                      loop._mod_token)
        assert status == 200
        [note] = [r for r in body['messages'] if 'Fluid compaction note' in r['text']]
        assert 'were archived with the session' in note['text']
        ref = re.search(r'HistoryExpand\("(node_[0-9a-f]+)"\)', note['text']).group(1)
        archived = [e async for e in iter_events_of_class(loop.store, sid, 0, (ContextArchived,))]
        entries = {e['node_id']: e for event in archived for e in event.entries}
        assert ref in entries and entries[ref]['text'].startswith('<system-reminder>attachment')
        ctx = ToolCtx(extra={'history_recall': HistoryRecall(loop.store, sid)})
        read = await expand({'ref': ref}, ctx)
        assert not read.is_error and 'a' * 800 in read.text
        found = await search({'query': 'attachment', 'max_results': 50}, ctx)
        assert ref in found.text
    finally:
        await loop.aclose()


def test_rows_only_claude_code_can_show_are_never_rebuilt():
    """A ToolSearch result (Claude Code shows its tool_reference blocks, the
    store their text) and a result with an image are kept whole or left out
    whole with refs: never rebuilt without the blocks Fluid cannot write."""
    chat = Chat()
    chat.cli.append(chat._add(Message('user', [Text('Request: find the deploy tool')])))
    use = chat._add(Message('assistant', [ToolUseBlock('ts-1', 'ToolSearch', {'query': 'select:Deploy'})]))
    found = chat._add(Message('user', [ToolResultBlock('ts-1', [Text('Deploy ' + 'd' * 3000)])]))
    shot = chat._add(Message('assistant', [ToolUseBlock('img-1', 'Read', {'file_path': 'a.png'})]))
    seen = chat._add(Message('user', [ToolResultBlock('img-1', [Text('[image: a.png] ' + 'i' * 3000),
                                                                Image(media_type='image/png', data='AAAA')])]))
    found['toolResults'][0]['text'] = ''  # what Claude Code lists for tool_reference blocks
    chat.cli += [use, found, shot, seen]
    chat.cli.append(chat._add(Message('assistant', [Text('Found it.')])))
    ids = {'ts-1', 'img-1'}
    for _ in range(4):
        chat.grow(8)
        out = chat.compact()
        for row in out:
            for result in row.get('toolResults', []):
                if result['tool_use_id'] in ids:
                    assert 'handle' in row  # Claude Code's own row, never a rebuilt one
        assert not _held_or_named(chat, out)
    assert recoverable(chat.stored, chat.cli, 'Deploy ' + 'd' * 3000)
