import { afterEach, expect, it, vi } from 'vitest';
import { cleanup, render, screen } from '@testing-library/svelte';
import { flushSync } from 'svelte';
import { swarms } from '$lib/stores/swarms';
import { reduceAll, type WireEvent } from '$lib/stores/session/v2/reducer';
import SwarmReplayHarness from './SwarmReplayHarness.svelte';

vi.mock('$lib/services/session-navigation', () => ({ openSessionLink: vi.fn(), openSubagentLink: vi.fn() }));
vi.mock('$lib/api/client', () => ({ recheckAuthOn401: vi.fn() }));
afterEach(() => { cleanup(); vi.unstubAllGlobals(); vi.restoreAllMocks(); });

it.each([true, false])('settles saved swarm results with grouping=%s and still accepts changed results', async groupingEnabled => {
 vi.stubGlobal('fetch', vi.fn(() => new Promise<Response>(() => {})));
 const sessionId = `swarm-replay-loop-${groupingEnabled}`;
 const snapshots = [0, 1].map(i => ({
  id: `swarm-${i}`, session_id: sessionId, title: `Workflow ${i}`, status: 'completed' as const,
  created_at: '2026-09-15T00:00:00Z', updated_at: '2026-09-15T00:01:00Z', tasks: []
 }));
 let seq = 0;
 const event = (type: string, fields: Record<string, unknown> = {}): WireEvent => ({
  type, seq: ++seq, id: `event-${seq}`, session_id: sessionId, ...fields
 });
 // Produce the tool rows through the event reducer, as a history replay does.
 const replay = reduceAll([
  event('message.user', { content: 'Review both workflows' }), event('turn.started'),
  ...snapshots.flatMap((snapshot, i) => [
   event('tool.use', { tool_use_id: `call-${i}`, name: 'Swarm', input: { action: 'status', swarm_id: snapshot.id } }),
   event('tool.result', { tool_use_id: `call-${i}`, content_preview: JSON.stringify({ swarm: snapshot }), is_error: false })
  ]),
  event('turn.completed')
 ]);
 const receive = swarms.receive;
 const publish = vi.spyOn(swarms, 'receive').mockImplementation((...args) => {
  // Fail promptly instead of letting a broken synchronous render hang the runner.
  if (publish.mock.calls.length > 20) throw new Error('Swarm snapshot feedback loop');
  receive(...args);
 });
 const view = render(SwarmReplayHarness, { messages: replay.messages, sessionId, groupingEnabled });
 flushSync();
 expect(screen.getAllByTestId('swarm-card')).toHaveLength(2);
 expect(publish).toHaveBeenCalledTimes(2);
 expect(screen.getAllByTestId('swarm-card').every(card => card.closest('.swarm-history'))).toBe(true);

 // New parent row objects must not republish the same saved text.
 await view.rerender({ messages: replay.messages.map(message => ({ ...message })), sessionId, groupingEnabled });
 flushSync();
 expect(publish).toHaveBeenCalledTimes(2);

 // A real result change must still publish, and a subsequent live update wins.
 const changed = { ...snapshots[0], title: 'Updated result', updated_at: '2026-09-15T00:02:00Z' };
 await view.rerender({ messages: replay.messages.map(message => message.toolId === 'call-0'
  ? { ...message, toolResult: JSON.stringify({ swarm: changed }) } : message), sessionId, groupingEnabled });
 flushSync();
 expect(publish).toHaveBeenCalledTimes(3);
 expect(screen.getByRole('button', { name: 'Swarm Updated result' })).toBeTruthy();
 swarms.receive({ ...changed, title: 'Live update', updated_at: '2026-09-15T00:03:00Z' });
 flushSync();
 expect(publish).toHaveBeenCalledTimes(4);
 expect(screen.getByRole('button', { name: 'Swarm Live update' })).toBeTruthy();
 expect(fetch).toHaveBeenCalledTimes(1);
});
