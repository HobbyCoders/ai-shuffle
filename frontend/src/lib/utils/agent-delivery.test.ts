import { describe, expect, it } from 'vitest';
import { readFileSync } from 'node:fs';
import type { ChatMessage, Task } from '$lib/stores/types';
import { agentDelivery, agentSender, agentSenderNames, heldAgentDeliveries } from './agent-delivery';
import { buildTranscriptModel } from './work-log';
import { deriveUserTurnNavigationItems } from './transcript-navigation';
import { reduceAll, type WireEvent } from '$lib/stores/session/v2/reducer';
// Real prompts built by app/core/turn_scheduler.py; regenerated and checked by
// tests/core/test_agent_delivery_frontend_contract.py.
import producer from './__fixtures__/agent-delivery-producer.json';

// Wire envelope from fluid.context.peer.format_peer_message (idle delivery).
const peer = (sender = 'reviewer', content = 'Review complete.') => `[agent\x20message from='${sender}' — a message from another agent in this session, delivered once at this position; not part of the tool output, and not a new message when re-read from history]\n${content}\n[/agent\x20message]`;
const topPeer = (sender = 'top-level-peer', content = 'Coordinate edits') => peer(sender, content).replace('another agent in this session', 'another top-level session');
const msg = (id: string, role: ChatMessage['role'], content: string, extra: Partial<ChatMessage> = {}): ChatMessage => ({ id, role, content, ...extra });

describe('agent delivery presentation', () => {
 it('keeps the fixture markers aligned with the actual Python producer', () => {
  const source = readFileSync('../fluid/fluid/context/peer.py', 'utf8');
  const open = source.match(/^PEER_OPEN_PREFIX = "([^"]+)"/m)?.[1];
  const close = source.match(/^PEER_CLOSE = "([^"]+)"/m)?.[1];
  expect(open).toBeTruthy();
  expect(close).toBeTruthy();
  expect(peer().startsWith(`${open} from=`)).toBe(true);
  expect(peer().endsWith(`\n${close}`)).toBe(true);
 });

 it('recognizes the full top-level producer envelope without broadening the marker guard', () => {
  const source = readFileSync('../app/core/session_orchestration.py', 'utf8');
  expect(source).toContain('prompt.replace("another agent in this session", "another top-level session", 1)');
  expect(agentDelivery(msg('top-peer', 'user', topPeer()))).toEqual({ sender: 'top-level-peer', content: 'Coordinate edits' });
  for (const content of [`Quoted:\n${topPeer()}`, topPeer().replace('another top-level session', 'another user'), topPeer().replace('[/agent\x20message]', '')]) {
   expect(agentDelivery(msg('human', 'user', content))).toBeNull();
  }
 });

 it.each([true, false])('keeps top-level delivery in the existing human work group (streaming=%s)', (isStreaming) => {
  const messages = [msg('human', 'user', 'Review this', { turnId: 'human-turn' }),
   msg('work', 'assistant', 'Working'),
   msg('peer', 'user', topPeer(), { turnId: 'peer-turn' }),
   msg('reply', 'assistant', 'Following up')];
  const model = buildTranscriptModel(messages, { isStreaming, latestTurnUnsettled: true, groupingEnabled: true });
  expect(model.latestUserTurnId).toBe('human');
  expect([...model.itemIndexByTurnId.keys()]).toEqual(['human']);
  expect(deriveUserTurnNavigationItems(messages).map(item => item.id)).toEqual(['human']);
  expect(messages[2].role).toBe('user');
 });

 it('recognizes idle peer envelopes and live system delivery receipts', () => {
  expect(agentDelivery(msg('peer', 'user', peer()))).toEqual({ sender: 'reviewer', content: 'Review complete.' });
  expect(agentDelivery(msg('live', 'system', '', { type: 'system', systemSubtype: 'agent_message', systemData: { sender: 'reviewer', content: 'Review complete.', delivery: 'delivered' } }))).toEqual({ sender: 'reviewer', content: 'Review complete.' });
 });
 it('does not reinterpret ordinary messages, quoted markers, or incomplete envelopes', () => {
  for (const content of ['Hello', `Explain this:\n${peer()}`, peer().replace('[/agent\x20message]', '')]) {
   expect(agentDelivery(msg('user', 'user', content))).toBeNull();
  }
  expect(agentDelivery(msg('assistant', 'assistant', peer()))).toBeNull();
 });
 it('splits held deliveries into attributed messages and refuses anything else', () => {
  const held = producer.held;
  expect(heldAgentDeliveries(producer.prompts.promotion)).toEqual(held);
  expect(heldAgentDeliveries(producer.prompts.urgent_with_held)).toEqual([...held, { sender: 'worker-c', content: 'Blocked: may I edit control.py?' }]);
  // fluid's own parked reminder (NativeLoop._queued_peer_reminder) joins its parts with blank lines.
  const fluidReminder = ['<system-reminder>', "Messages from other agents that arrived while this session was parked (they were not delivered separately; a subagent's final report is the completion below, if any):", peer('w1', 'one'), peer('w2', 'two'), '</system-reminder>'].join('\n\n');
  expect(heldAgentDeliveries(fluidReminder)).toEqual([{ sender: 'w1', content: 'one' }, { sender: 'w2', content: 'two' }]);
  for (const content of [
   peer(),
   `Look at this:\n${producer.prompts.promotion}`,
   producer.prompts.promotion.replace('[/agent\x20message]\n</system-reminder>', 'stray text\n</system-reminder>'),
   '<system-reminder>\nSomething else entirely\n</system-reminder>',
   producer.prompts.promotion.replace('</system-reminder>', ''),
  ]) {
   expect(heldAgentDeliveries(content)).toBeNull();
  }
 });

 describe('a delivered prompt as the chat reducer renders it (#1618)', () => {
  const render = (content: string) => reduceAll([{ type: 'message.user', id: 'evt-1', seq: 1, session_id: 's1', created_at: '2026-10-01T00:00:00.000Z', content } as WireEvent]).messages;
  const delivered = (messages: ChatMessage[]) => messages.flatMap(m => { const d = agentDelivery(m); return d ? [d] : []; });
  const visibleText = (messages: ChatMessage[]) => messages.map(m => m.content ?? '').join('\n');

  it.each(['promotion', 'urgent_with_held'] as const)('%s: one attributed message per peer, no reminder text', (key) => {
   const messages = render(producer.prompts[key]);
   const expected = key === 'promotion' ? producer.held : [...producer.held, { sender: 'worker-c', content: 'Blocked: may I edit control.py?' }];
   expect(delivered(messages)).toEqual(expected);
   expect(messages).toHaveLength(expected.length);
   expect(messages.some(m => m.role === 'user')).toBe(false);
   expect(visibleText(messages)).not.toMatch(/system-reminder|Messages from other agents|\[agent\x20message/);
   expect(new Set(messages.map(m => m.id)).size).toBe(messages.length);
  });

  it('unpark wake: the task card, then each held message, attributed', () => {
   const messages = render(producer.prompts.unpark_with_held);
   expect(messages.map(m => m.systemSubtype)).toEqual(['task_notification', 'agent_message', 'agent_message']);
   expect(messages[0].id).toBe('evt-1');
   expect(delivered(messages)).toEqual(producer.held);
   expect(visibleText(messages)).not.toMatch(/system-reminder|\[agent\x20message/);
  });

  it('a single idle delivery keeps its existing user-role presentation', () => {
   const messages = render(producer.prompts.idle_single);
   expect(messages).toHaveLength(1);
   expect(messages[0].role).toBe('user');
   expect(agentDelivery(messages[0])).toEqual({ sender: 'worker-c', content: 'Done; report attached.' });
  });

  it('typed text that quotes a reminder stays the user\'s own bubble', () => {
   const typed = `What does this mean?\n${producer.prompts.promotion}`;
   const messages = render(typed);
   expect(messages).toHaveLength(1);
   expect(messages[0]).toMatchObject({ role: 'user', content: typed });
  });

  it('held deliveries never open a human turn of their own', () => {
   const messages = [msg('human', 'user', 'Review this', { turnId: 'human-turn' }), msg('reply', 'assistant', 'Working'), ...render(producer.prompts.promotion)];
   const model = buildTranscriptModel(messages, { isStreaming: false, latestTurnUnsettled: true, groupingEnabled: true });
   expect(model.latestUserTurnId).toBe('human');
   expect(deriveUserTurnNavigationItems(messages).map(item => item.id)).toEqual(['human']);
  });
 });

 it.each([true, false])('does not split a live or parked turn on consecutive agent wakes (streaming=%s)', (isStreaming) => {
  const messages = [msg('user', 'user', 'Review this', { turnId: 'human-turn' }),
   msg('work', 'assistant', '', { type: 'tool_use', toolName: 'Read', toolStatus: 'complete' }),
   msg('commentary', 'assistant', 'Checking the result.'),
   msg('peer-1', 'user', peer(), { turnId: 'wake-1' }),
   msg('reply', 'assistant', 'Following up.'),
   msg('peer-2', 'user', peer('tester'), { turnId: 'wake-2' })];
  const model = buildTranscriptModel(messages, { isStreaming, latestTurnUnsettled: true, groupingEnabled: true });
  expect(model.items.filter(item => item.kind === 'work_log' && item.mode === 'settled')).toEqual([]);
  expect(model.latestUserTurnId).toBe('user');
  expect([...model.itemIndexByTurnId.keys()]).toEqual(['user']);
  expect(deriveUserTurnNavigationItems(messages).map(item => item.id)).toEqual(['user']);
  expect(messages[3].role).toBe('user');
 });

 describe('sender attribution (owner decision 2026-10-01)', () => {
  const card = (extra: Partial<ChatMessage>) => msg('card', 'assistant', '', { type: 'subagent', ...extra });
  const task = (id: string, subject: string, extra: Partial<Task> = {}): Task => ({ id, subject, status: 'running', kind: 'agent', summary: null, toolUseId: null, startedAt: '', updatedAt: '', ...extra });

  it('names a worker by its Task description and keeps the session id as secondary', () => {
   const names = agentSenderNames([card({ toolId: 'w-1', agentSessionId: 'w-1', agentDescription: 'Fix flaky login', agentName: 'fix-flaky-login' })]);
   const delivery = agentDelivery(msg('peer', 'user', peer('w-1')))!;
   expect(agentSender(delivery, names)).toEqual({ name: 'Fix flaky login', id: 'w-1' });
  });

  it('uses the agent name when the card has no description, and the task registry when no card exists', () => {
   const names = agentSenderNames([card({ toolId: 'launch-9', agentSessionId: 'w-2', agentName: 'Reviewer' })], [task('w-3', 'Audit the schema')]);
   expect(names.get('w-2')).toBe('Reviewer');
   expect(names.get('launch-9')).toBe('Reviewer');
   expect(names.get('w-3')).toBe('Audit the schema');
  });

  it('prefers the label the backend stamped on a delivery note', () => {
   const note = msg('live', 'system', '', { type: 'system', systemSubtype: 'agent_message', systemData: { sender: 'w-1', sender_label: 'Fix flaky login', content: 'Found it', delivery: 'delivered' } });
   const delivery = agentDelivery(note)!;
   expect(delivery).toEqual({ sender: 'w-1', senderLabel: 'Fix flaky login', content: 'Found it' });
   expect(agentSender(delivery, new Map())).toEqual({ name: 'Fix flaky login', id: 'w-1' });
  });

  it('falls back to the session id when no name is known', () => {
   const delivery = agentDelivery(msg('peer', 'user', peer('w-unknown')))!;
   expect(agentSender(delivery, agentSenderNames([], [task('other', 'Something else')]))).toEqual({ name: 'w-unknown', id: null });
   expect(agentSender(delivery)).toEqual({ name: 'w-unknown', id: null });
  });

  it('names every held delivery from the same lookup', () => {
   const held = heldAgentDeliveries(producer.prompts.promotion)!;
   expect(held.length).toBeGreaterThan(0);
   const names = agentSenderNames([], held.map(d => task(d.sender, `Task for ${d.sender}`)));
   expect(held.map(d => agentSender(d, names))).toEqual(held.map(d => ({ name: `Task for ${d.sender}`, id: d.sender })));
  });
 });
});
