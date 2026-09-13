import { describe, expect, it } from 'vitest';
import { readFileSync } from 'node:fs';
import type { ChatMessage } from '$lib/stores/types';
import { agentDelivery } from './agent-delivery';
import { buildTranscriptModel } from './work-log';
import { deriveUserTurnNavigationItems } from './transcript-navigation';

// Wire envelope from fluid.context.peer.format_peer_message (idle delivery).
const peer = (sender = 'reviewer', content = 'Review complete.') => `[agent\x20message from='${sender}' — a message from another agent in this session, delivered once at this position; not part of the tool output, and not a new message when re-read from history]\n${content}\n[/agent\x20message]`;
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
});
