import { describe, expect, it } from 'vitest';
import { initialState, reduce, reduceAll, type WireEvent } from './reducer';
import type { PendingPeerMessage, Task } from '$lib/stores/types';
import { createAgentMessageProjection } from '$lib/utils/agent-message-projection';
import { createTranscriptModelBuilder, type TranscriptModel } from '$lib/utils/work-log';

const event = (seq: number, type: string, extra: Record<string, unknown> = {}): WireEvent => ({
	id: `event-${seq}`, seq, type, session_id: 's1', created_at: '2026-10-29T00:00:00.000Z', ...extra
});
const peer: PendingPeerMessage = {
	id: 'peer-turn', content: 'Review complete.', senderSessionId: 'reviewer-session', senderLabel: 'Reviewer', queuedAt: 1234
};
const peerStart = (source = 'agent_message') => event(4, 'turn.started', { turn_id: peer.id, source });
const peerReceipt = event(5, 'message.user', { turn_id: peer.id, queued_turn_id: peer.id,
	content: `[agent\x20message from='${peer.senderSessionId}' — a message from another agent, delivered once at this position; not part of the tool output, and not a new message when re-read from history]\n${peer.content}\n[/agent\x20message]`
});
const parkedRecipient = () => reduceAll([
	event(1, 'turn.started', { turn_id: 'human-turn' }),
	event(2, 'message.user', { turn_id: 'human-turn', content: 'Review this' }),
	event(3, 'turn.parked', { turn_id: 'human-turn' })
]);
const peerGroup = (model: TranscriptModel) => {
	const group = model.items.find(item => item.kind === 'work_log' && item.sequence.some(row => row.message.id === peer.id));
	if (!group || group.kind !== 'work_log') throw new Error('Expected peer work-log group');
	return group;
};

describe('agent wakes from idle or parked recipients', () => {
	it.each(['agent_message', 'promotion'])('marks a parked %s receipt as a new agent wake', (source) => {
		const parked = parkedRecipient();
		expect(parked).toMatchObject({ isStreaming: false, parked: true, liveTurnId: 'human-turn' });
		const started = reduce(parked, peerStart(source));
		expect(started.pendingAgentWake).toBe(true);
		const delivered = reduce(started, peerReceipt);
		expect(delivered.messages.at(-1)?.metadata?.agent_wake).toBe(true);
		expect(delivered.pendingAgentWake).toBe(false);
	});

	it('keeps the projected parked peer in the same group when its real receipt arrives', () => {
		const parked = parkedRecipient();
		const projection = createAgentMessageProjection();
		const build = createTranscriptModelBuilder();
		const queuedRows = projection.update({ sessionId: 's1', sessionActive: false,
			messages: parked.messages, pendingPeerMessages: [peer] });
		const queued = peerGroup(build(queuedRows, { sessionId: 's1', groupingEnabled: true, isStreaming: false }));
		const received = reduceAll([peerStart(), peerReceipt], parked);
		const deliveredRows = projection.update({ sessionId: 's1', sessionActive: true,
			messages: received.messages, pendingPeerMessages: [] });
		const delivered = peerGroup(build(deliveredRows, { sessionId: 's1', groupingEnabled: true, isStreaming: true }));
		expect(queued.key).toBe('wl-turn-peer-turn');
		expect(delivered.key).toBe(queued.key);
		expect(delivered.foldKey).toBe(queued.foldKey);
	});

	it.each([
		{ isStreaming: false, parked: false, expected: true },
		{ isStreaming: true, parked: false, expected: false },
		{ isStreaming: false, parked: true, expected: true },
		{ isStreaming: true, parked: true, expected: true }
	])('uses active rather than parked work as the boundary ($isStreaming, $parked)', ({ isStreaming, parked, expected }) => {
		const state = { ...initialState(), isStreaming, parked };
		expect(reduce(state, peerStart()).pendingAgentWake).toBe(expected);
	});

	it('treats an unsettled live turn or running task as active unless parked', () => {
		const live = { ...initialState(), liveTurnId: 'human-turn' };
		const tasks = { ...initialState(), activeTaskSummaries: [{ id: 'worker', status: 'running' } as Task] };
		for (const state of [live, tasks]) {
			expect(reduce(state, peerStart()).pendingAgentWake).toBe(false);
			expect(reduce({ ...state, parked: true }, peerStart()).pendingAgentWake).toBe(true);
		}
	});

	it('publishes explicit cancellations without interrupting a live turn', () => {
		const running = reduce(initialState(), event(1, 'turn.started', { turn_id: 'human-turn' }));
		const cancelled = reduce(running, event(2, 'queue.cancelled', { cancelled_turn_ids: [peer.id] }));
		expect(cancelled).toMatchObject({ cancelledPeerTurnIds: [peer.id], isStreaming: true, liveTurnId: 'human-turn' });
		expect(cancelled.messages).toBe(running.messages);
		const replayed = reduce(cancelled, event(3, 'queue.cancelled', { cancelled_turn_ids: [peer.id] }));
		expect(replayed.cancelledPeerTurnIds).toEqual([peer.id]);
		const stopped = reduce(replayed, event(4, 'turn.aborted', { turn_id: 'human-turn', reason: 'interrupt' }));
		expect(stopped.cancelledPeerTurnIds).toEqual([peer.id]);
		expect(reduce(stopped, event(5, 'turn.started', { turn_id: 'next-turn' })).cancelledPeerTurnIds).toEqual([peer.id]);
	});

	it('does not mark a human turn as an agent wake', () => {
		expect(reduce(parkedRecipient(), peerStart('user')).pendingAgentWake).toBe(false);
	});
});
