import { describe, expect, it } from 'vitest';
import type { ChatMessage, PendingPeerMessage } from '$lib/stores/types';
import { reduce, reduceAll, releaseMessages, type WireEvent } from '$lib/stores/session/v2/reducer';
import { agentDelivery, agentSender, agentSenderNames } from './agent-delivery';
import { createAgentMessageProjection } from './agent-message-projection';
import { createTranscriptModelBuilder, type TranscriptModel } from './work-log';

const pendingPeer = (id = 'peer-1', extra: Partial<PendingPeerMessage> = {}): PendingPeerMessage => ({
	id, content: 'Review complete.', senderLabel: 'Reviewer', senderSessionId: 'reviewer-session', queuedAt: 1234, ...extra
});
const prompt: ChatMessage = { id: 'prompt', role: 'user', type: 'text', content: 'Review this', turnId: 'running-turn' };
const envelope = (peer: PendingPeerMessage) => `[agent\x20message from='${peer.senderSessionId}' — a message from another agent, delivered once at this position; not part of the tool output, and not a new message when re-read from history]\n${peer.content}\n[/agent\x20message]`;

// Drive the actual transcript producer: a received wire event, not a projected fixture.
const receipt = (peer: PendingPeerMessage, extra: Record<string, unknown> = {}, wakes = false): ChatMessage => {
	const event = {
		type: 'message.user', id: `event-${peer.id}`, seq: wakes ? 2 : 1, session_id: 's1',
		created_at: '2026-10-29T00:00:00.000Z', turn_id: wakes ? peer.id : 'running-turn', queued_turn_id: peer.id,
		content: envelope(peer), ...extra
	} as WireEvent;
	return reduceAll(wakes ? [{
		type: 'turn.started', id: `start-${peer.id}`, seq: 1, session_id: 's1',
		created_at: event.created_at, source: 'agent_message', turn_id: peer.id
	} as WireEvent, event] : [event]).messages[0];
};

function workLog(model: TranscriptModel, messageId = 'peer-1') {
	const group = model.items.find((item) => item.kind === 'work_log' && item.sequence.some(entry => entry.message.id === messageId));
	if (!group || group.kind !== 'work_log') throw new Error('Expected a work-log group');
	return group;
}

describe('agent-message transcript projection', () => {
	it('appends attributed queued rows at the tail without reclassifying human messages', () => {
		const project = createAgentMessageProjection();
		const peer = pendingPeer();
		const rows = project.update({ sessionId: 's1', messages: [prompt], pendingPeerMessages: [peer] });
		expect(rows).toHaveLength(2);
		expect(rows[0]).toBe(prompt);
		expect(rows[1]).toMatchObject({
			id: peer.id, serverId: peer.id, turnId: peer.id, role: 'system', type: 'system',
			systemSubtype: 'agent_message', systemData: { delivery: 'queued' },
			timestamp: '1970-01-01T00:00:01.234Z'
		});
		expect(agentDelivery(rows[1])).toEqual({ sender: peer.senderSessionId, content: peer.content });
	});

	it.each([false, true])('keeps sender Input identical when queued, delivered and reloaded (known worker=%s)', (knownWorker) => {
		const peer = pendingPeer('peer-1', { senderLabel: 'Transient queue title' });
		const worker: ChatMessage = {
			id: 'worker-card', role: 'assistant', type: 'subagent', content: '',
			agentSessionId: peer.senderSessionId, agentDescription: 'Review findings'
		};
		const history = knownWorker ? [worker] : [];
		const inputFor = (message: ChatMessage, messages: ChatMessage[]) => {
			const sender = agentSender(agentDelivery(message)!, agentSenderNames(messages));
			return sender.id ? { label: sender.name, session: sender.id } : { label: sender.name };
		};
		const project = createAgentMessageProjection();
		const queued = project.update({ sessionId: 's1', messages: history, pendingPeerMessages: [peer] });
		const received = receipt(peer);
		expect(received).toMatchObject({ role: 'user', type: 'text' });
		expect(received.systemData).toBeUndefined();
		const delivered = project.update({ sessionId: 's1', messages: [...history, received], pendingPeerMessages: [] });
		// Replay the wire event into a fresh reducer and projection, with no queue memory.
		const reloaded = createAgentMessageProjection().update({
			sessionId: 's1', messages: [...history, receipt(peer)], pendingPeerMessages: []
		});
		const expected = knownWorker ? { label: worker.agentDescription, session: peer.senderSessionId } : { label: peer.senderSessionId };
		expect(inputFor(queued.at(-1)!, queued)).toEqual(expected);
		expect(inputFor(delivered.at(-1)!, delivered)).toEqual(expected);
		expect(inputFor(reloaded.at(-1)!, reloaded)).toEqual(expected);
		expect(delivered.at(-1)?.systemData?.sender_label).toBeUndefined();
	});

	it('does not churn rows when only a queue-only sender label changes', () => {
		const project = createAgentMessageProjection();
		const peer = pendingPeer();
		const before = project.update({ sessionId: 's1', messages: [], pendingPeerMessages: [peer] });
		const after = project.update({ sessionId: 's1', messages: [], pendingPeerMessages: [{ ...peer, senderLabel: 'A new queue title' }] });
		expect(after).toBe(before);
	});

	it('swaps pending for delivered under the same presentation and server keys', () => {
		const project = createAgentMessageProjection();
		const peer = pendingPeer();
		const before = project.update({ sessionId: 's1', messages: [], pendingPeerMessages: [peer] });
		const received = { ...receipt(peer), serverId: 'original-server-id' };
		expect(received).toMatchObject({ id: `event-${peer.id}`, turnId: 'running-turn', queuedTurnId: peer.id });
		const after = project.update({ sessionId: 's1', messages: [received], pendingPeerMessages: [] });
		expect(before[0].systemData?.delivery).toBe('queued');
		expect(after).toHaveLength(1);
		expect(after[0]).toMatchObject({ id: before[0].id, serverId: before[0].serverId, systemData: { delivery: 'delivered' } });
		expect(agentDelivery(after[0])?.content).toBe(peer.content);
		expect(received.id).toBe(`event-${peer.id}`);
		expect(received.serverId).toBe('original-server-id');
		expect(received.systemData).toBeUndefined();
	});

	it('uses turnId when the receipt has no queuedTurnId', () => {
		const peer = pendingPeer();
		const rows = createAgentMessageProjection().update({
			sessionId: 's1', messages: [receipt(peer, { turn_id: peer.id, queued_turn_id: undefined })], pendingPeerMessages: [peer]
		});
		expect(rows).toHaveLength(1);
		expect(rows[0]).toMatchObject({ id: peer.id, serverId: peer.id, systemData: { delivery: 'delivered' } });
	});

	it('normalizes system delivery receipts and retains their other data', () => {
		const message: ChatMessage = {
			id: 'system-event', role: 'system', type: 'system', content: 'Done', turnId: 'peer-system',
			systemSubtype: 'agent_message', systemData: { sender: 'worker', content: 'Done', delivery: 'held', sender_label: 'Worker' }
		};
		const [row] = createAgentMessageProjection().update({ sessionId: 's1', messages: [message], pendingPeerMessages: [] });
		expect(row).toMatchObject({ id: 'peer-system', serverId: 'peer-system', systemData: { ...message.systemData, delivery: 'delivered' } });
		expect(message.systemData?.delivery).toBe('held');
	});

	it('does not resurrect a delivered row from a stale queue snapshot', () => {
		const project = createAgentMessageProjection();
		const peer = pendingPeer();
		const received = receipt(peer);
		const delivered = project.update({ sessionId: 's1', messages: [received], pendingPeerMessages: [] });
		const stale = project.update({ sessionId: 's1', messages: [received], pendingPeerMessages: [peer, { ...peer }] });
		expect(stale).toBe(delivered);
		expect(stale).toHaveLength(1);
		expect(stale[0].systemData?.delivery).toBe('delivered');
		// A temporarily empty history must not let a late snapshot requeue it either.
		expect(project.update({ sessionId: 's1', messages: [], pendingPeerMessages: [peer] })).toEqual([]);
	});

	it('retains the last-known queued row when the queue clears before its receipt', () => {
		const project = createAgentMessageProjection();
		const peer = pendingPeer();
		const queued = project.update({ sessionId: 's1', messages: [prompt], pendingPeerMessages: [peer] });
		const gap = project.update({ sessionId: 's1', messages: [prompt], pendingPeerMessages: [] });
		expect(gap).toBe(queued);
		expect(gap[1].systemData?.delivery).toBe('queued');
		const delivered = project.update({ sessionId: 's1', messages: [prompt, receipt(peer)], pendingPeerMessages: [] });
		expect(delivered).toHaveLength(2);
		expect(delivered[1].id).toBe(gap[1].id);
		expect(delivered[1].systemData?.delivery).toBe('delivered');
	});

	it.each([false, true])('retires Stop peers only after explicit cancellation (queue cleared first=%s)', (queueClearedFirst) => {
		let state = reduceAll([
			{ type: 'turn.started', id: 'start', seq: 1, turn_id: 'running-turn' },
			{ type: 'message.user', id: 'prompt', seq: 2, turn_id: 'running-turn', content: 'Review this' }
		] as WireEvent[]);
		const project = createAgentMessageProjection();
		const peer = pendingPeer();
		const queued = project.update({ sessionId: 's1', messages: releaseMessages(state), sessionActive: true, pendingPeerMessages: [peer] });
		if (queueClearedFirst) {
			expect(project.update({ sessionId: 's1', messages: releaseMessages(state), sessionActive: true, pendingPeerMessages: [] })).toBe(queued);
		}
		// A Stop before any assistant output still emits this durable event.
		state = reduce(state, { type: 'turn.aborted', id: 'stop', seq: 3, turn_id: 'running-turn', reason: 'interrupt' });
		const stoppedInput = { sessionId: 's1', messages: releaseMessages(state), sessionActive: state.isStreaming,
			pendingPeerMessages: queueClearedFirst ? [] : [peer] };
		expect(project.update(stoppedInput).at(-1)).toBe(queued.at(-1));
		expect(project.update({ ...stoppedInput, pendingPeerMessages: [] }).at(-1)).toBe(queued.at(-1));
		state = reduce(state, { type: 'queue.cancelled', id: 'cancelled', seq: 4, cancelled_turn_ids: [peer.id] });
		const cleared = project.update({ ...stoppedInput, cancelledTurnIds: state.cancelledPeerTurnIds });
		expect(cleared.some(message => agentDelivery(message))).toBe(false);
		expect(project.update({ ...stoppedInput, pendingPeerMessages: [] })).toBe(cleared);
		expect(project.update({ ...stoppedInput, pendingPeerMessages: [peer] })).toBe(cleared);
		// A later, genuinely new queued message is not retired by the old abort.
		expect(project.update({ ...stoppedInput, pendingPeerMessages: [pendingPeer('next-peer')] }).at(-1)?.id).toBe('next-peer');
	});

	it('retains Send now peers when the queue clears before successor start, using production flags', () => {
		let state = reduceAll([
			{ type: 'turn.started', id: 'start', seq: 1, turn_id: 'running-turn' },
			{ type: 'message.user', id: 'prompt', seq: 2, turn_id: 'running-turn', content: 'Review this' }
		] as WireEvent[]);
		const project = createAgentMessageProjection();
		const build = createTranscriptModelBuilder();
		const peer = pendingPeer();
		const options = { sessionId: 's1', isStreaming: true, groupingEnabled: true };
		const queued = project.update({ sessionId: 's1', messages: releaseMessages(state),
			pendingPeerMessages: [peer], sessionActive: state.isStreaming });
		const peerRow = queued.at(-1);
		const groupKey = workLog(build(queued, options)).key;
		state = reduce(state, { type: 'turn.aborted', id: 'send-now-interrupt', seq: 3, turn_id: 'running-turn', reason: 'interrupt' });
		// The successor is starting; this interrupt did not cancel the peer queue.
		const interrupted = project.update({ sessionId: 's1', messages: releaseMessages(state),
			pendingPeerMessages: [peer], sessionActive: state.isStreaming });
		expect(interrupted.at(-1)).toBe(peerRow);
		// These real flags now look fully settled, before the successor is announced.
		expect(state).toMatchObject({ isStreaming: false, parked: false, liveTurnId: null });
		expect(project.update({ sessionId: 's1', messages: releaseMessages(state),
			pendingPeerMessages: [], sessionActive: state.isStreaming }).at(-1)).toBe(peerRow);
		state = reduce(state, { type: 'turn.started', id: 'successor', seq: 4, turn_id: 'successor-turn', source: 'user' });
		const gap = project.update({ sessionId: 's1', messages: releaseMessages(state), pendingPeerMessages: [],
			sessionActive: state.isStreaming });
		expect(gap.at(-1)).toBe(peerRow);
		expect(gap.filter(message => agentDelivery(message))).toHaveLength(1);
		expect(workLog(build(gap, options)).key).toBe(groupKey);
		state = reduce(state, { type: 'message.user', id: 'peer-receipt', seq: 5, turn_id: 'successor-turn',
			queued_turn_id: peer.id, content: envelope(peer) });
		const delivered = project.update({ sessionId: 's1', messages: releaseMessages(state), pendingPeerMessages: [],
			sessionActive: state.isStreaming });
		expect(delivered.filter(message => agentDelivery(message))).toHaveLength(1);
		expect(delivered.at(-1)).toMatchObject({ id: peerRow!.id, serverId: peerRow!.serverId, systemData: { delivery: 'delivered' } });
		expect(workLog(build(delivered, options)).key).toBe(groupKey);
	});

	it('retires a queue-only Stop only when explicit cancelled ids arrive', () => {
		const project = createAgentMessageProjection();
		const peer = pendingPeer();
		const input = { sessionId: 's1', messages: [], sessionActive: false };
		const queued = project.update({ ...input, pendingPeerMessages: [peer] });
		expect(project.update({ ...input, pendingPeerMessages: [] })).toBe(queued);
		expect(project.update({ ...input, pendingPeerMessages: [], cancelledTurnIds: [peer.id] })).toEqual([]);
		expect(project.update({ ...input, pendingPeerMessages: [peer] })).toEqual([]);
	});

	it.each([false, true])('retains a receipt gap while active, parked or starting (active=%s)', (sessionActive) => {
		const project = createAgentMessageProjection();
		const peer = pendingPeer();
		const input = { sessionId: 's1', messages: [], sessionActive };
		const queued = project.update({ ...input, pendingPeerMessages: [peer] });
		expect(project.update({ ...input, pendingPeerMessages: [] })).toBe(queued);
		expect(project.update({ ...input, pendingPeerMessages: [] })).toBe(queued);
		const delivered = project.update({ ...input, messages: [receipt(peer, {}, !sessionActive)], pendingPeerMessages: [] });
		expect(delivered).toHaveLength(1);
		expect(delivered[0].id).toBe(queued[0].id);
		expect(delivered[0].systemData?.delivery).toBe('delivered');
	});

	it('invalidates the memo only when explicit cancelled ids arrive', () => {
		const project = createAgentMessageProjection();
		const peer = pendingPeer();
		const messages: ChatMessage[] = [];
		const pendingPeerMessages: PendingPeerMessage[] = [];
		project.update({ sessionId: 's1', messages, pendingPeerMessages: [peer], sessionActive: false });
		expect(project.update({ sessionId: 's1', messages, pendingPeerMessages, sessionActive: false })).toHaveLength(1);
		expect(project.update({ sessionId: 's1', messages, pendingPeerMessages, sessionActive: false, cancelledTurnIds: [peer.id] })).toEqual([]);
	});

	it('clears session memory when switching away and back, then uses the returning snapshot', () => {
		const project = createAgentMessageProjection();
		const a = pendingPeer('a');
		const b = pendingPeer('b');
		project.update({ sessionId: 'a', messages: [], pendingPeerMessages: [a] });
		expect(project.update({ sessionId: 'b', messages: [], pendingPeerMessages: [b] }).map(row => row.id)).toEqual(['b']);
		expect(project.update({ sessionId: 'a', messages: [], pendingPeerMessages: [] })).toEqual([]);
		expect(project.update({ sessionId: 'a', messages: [], pendingPeerMessages: [a] }).map(row => row.id)).toEqual(['a']);
		expect(project.update({ sessionId: null, messages: [], pendingPeerMessages: [] })).toEqual([]);
	});

	it('does not carry delivery suppression between sessions or projection instances', () => {
		const project = createAgentMessageProjection();
		const peer = pendingPeer();
		project.update({ sessionId: 'a', messages: [receipt(peer)], pendingPeerMessages: [] });
		const b = project.update({ sessionId: 'b', messages: [], pendingPeerMessages: [peer] });
		expect(b[0].systemData?.delivery).toBe('queued');
		const other = createAgentMessageProjection().update({ sessionId: 'a', messages: [], pendingPeerMessages: [peer] });
		expect(other[0].systemData?.delivery).toBe('queued');
	});

	it('keeps identical text under different keys and delivers only the matching row', () => {
		const project = createAgentMessageProjection();
		const first = pendingPeer('first');
		const second = pendingPeer('second');
		const rows = project.update({ sessionId: 's1', messages: [], pendingPeerMessages: [first, second] });
		expect(rows.map(row => row.id)).toEqual(['first', 'second']);
		const after = project.update({ sessionId: 's1', messages: [receipt(first)], pendingPeerMessages: [first, second] });
		expect(after.map(row => [row.id, row.systemData?.delivery])).toEqual([['first', 'delivered'], ['second', 'queued']]);
		expect(after[1]).toBe(rows[1]);
	});

	it('reuses arrays and rows for unchanged input, including replacement queue snapshots', () => {
		const project = createAgentMessageProjection();
		const peer = pendingPeer();
		const input = { sessionId: 's1', messages: [prompt], pendingPeerMessages: [peer] };
		const first = project.update(input);
		expect(project.update(input)).toBe(first);
		expect(project.update({ ...input, messages: [prompt], pendingPeerMessages: [{ ...peer }] })).toBe(first);
		const received = receipt(peer);
		const delivered = project.update({ ...input, messages: [prompt, received], pendingPeerMessages: [] });
		expect(project.update({ ...input, messages: [prompt, received], pendingPeerMessages: [] })).toBe(delivered);
		expect(delivered[0]).toBe(prompt);
	});

	it('updates queued data without changing the key or mutating earlier output', () => {
		const project = createAgentMessageProjection();
		const peer = pendingPeer();
		const first = project.update({ sessionId: 's1', messages: [], pendingPeerMessages: [peer] });
		const updated = { ...peer, content: 'Updated report', senderSessionId: 'another-session' };
		const next = project.update({ sessionId: 's1', messages: [], pendingPeerMessages: [updated] });
		expect(next[0]).not.toBe(first[0]);
		expect(next[0].id).toBe(first[0].id);
		expect(agentDelivery(next[0])).toEqual({ content: updated.content, sender: updated.senderSessionId });
		expect(first[0].content).toBe(peer.content);
	});

	it('passes through ordinary messages and does not detect deliveries by text fragments or turn ids alone', () => {
		const peer = pendingPeer();
		const human = { ...prompt, content: `Please explain this:\n${envelope(peer)}`, turnId: peer.id };
		const messages = [human];
		const project = createAgentMessageProjection();
		expect(project.update({ sessionId: 's1', messages, pendingPeerMessages: [] })).toBe(messages);
		const withPending = project.update({ sessionId: 's1', messages, pendingPeerMessages: [peer] });
		expect(withPending).toHaveLength(2);
		expect(withPending[0]).toBe(human);
		expect(withPending[1].systemData?.delivery).toBe('queued');
	});

	it('keeps a legacy delivery identity when neither turn id is available', () => {
		const peer = pendingPeer();
		const message = receipt(peer, { turn_id: undefined, queued_turn_id: undefined });
		const [row] = createAgentMessageProjection().update({ sessionId: 's1', messages: [message], pendingPeerMessages: [] });
		expect(row.id).toBe(message.id);
		expect(row.serverId).toBe(message.id);
	});

	it('accepts an unknown queue timestamp without losing reference stability', () => {
		const project = createAgentMessageProjection();
		const peer = pendingPeer('unknown-time', { queuedAt: NaN });
		const first = project.update({ sessionId: 's1', messages: [], pendingPeerMessages: [peer] });
		expect(first[0].timestamp).toBeUndefined();
		expect(project.update({ sessionId: 's1', messages: [], pendingPeerMessages: [{ ...peer }] })).toBe(first);
	});

	it('does not mutate frozen store rows, their data, or input arrays', () => {
		const peer = pendingPeer();
		const message = receipt(peer);
		const messages = [prompt, message];
		const pendingPeerMessages = [peer];
		Object.freeze(message);
		Object.freeze(peer);
		Object.freeze(messages);
		Object.freeze(pendingPeerMessages);
		const before = JSON.stringify({ messages, pendingPeerMessages });
		createAgentMessageProjection().update({ sessionId: 's1', messages, pendingPeerMessages });
		expect(JSON.stringify({ messages, pendingPeerMessages })).toBe(before);
	});
});

describe('projected agent-message work-log grouping', () => {
	it('keeps a queued row visible in a settled turn instead of marking it completed work', () => {
		const project = createAgentMessageProjection();
		const rows = project.update({ sessionId: 's1', messages: [prompt], pendingPeerMessages: [pendingPeer()] });
		const model = createTranscriptModelBuilder()(rows, { isStreaming: false, groupingEnabled: true, sessionId: 's1' });
		const group = workLog(model);
		expect(group.mode).toBe('settled');
		expect(group.sequence.find(entry => entry.message.id === 'peer-1')?.hiddenWhenCollapsed).toBe(false);
		expect(group.folded.some(entry => entry.message.id === 'peer-1')).toBe(false);
	});

	it.each([
		{ isStreaming: true, hasPrompt: true }, { isStreaming: false, hasPrompt: true },
		{ isStreaming: true, hasPrompt: false }, { isStreaming: false, hasPrompt: false }
	])('keeps group and fold keys across delivery ($isStreaming, prompt=$hasPrompt)', ({ isStreaming, hasPrompt }) => {
		const project = createAgentMessageProjection();
		const build = createTranscriptModelBuilder();
		const peer = pendingPeer();
		const messages = hasPrompt ? [prompt] : [];
		const options = { isStreaming, groupingEnabled: true, sessionId: 's1' };
		const queued = workLog(build(project.update({ sessionId: 's1', messages, pendingPeerMessages: [peer] }), options));
		const delivered = workLog(build(project.update({ sessionId: 's1', messages: [...messages, receipt(peer)], pendingPeerMessages: [] }), options));
		expect(delivered.key).toBe(queued.key);
		expect(delivered.foldKey).toBe(queued.foldKey);
		expect(delivered.sequence[0].message.id).toBe(queued.sequence[0].message.id);
		expect(delivered.sequence[0].message.serverId).toBe(queued.sequence[0].message.serverId);
	});

	it.each([false, true])('keeps two idle peers in the first wake group (same snapshot=%s)', (sameSnapshot) => {
		const project = createAgentMessageProjection();
		const build = createTranscriptModelBuilder();
		const first = pendingPeer();
		const second = pendingPeer('peer-2');
		const history: ChatMessage[] = [prompt, { id: 'answer', role: 'assistant', type: 'text', content: 'Previous answer.' }];
		const options = { sessionId: 's1', isStreaming: false, groupingEnabled: true };
		if (!sameSnapshot) project.update({ sessionId: 's1', messages: history, pendingPeerMessages: [first], sessionActive: false });
		const queuedRows = project.update({ sessionId: 's1', messages: history, pendingPeerMessages: [first, second], sessionActive: false });
		const queuedModel = build(queuedRows, options);
		const key = workLog(queuedModel, first.id).key;
		expect(workLog(queuedModel, second.id).key).toBe(key);
		expect(queuedRows.filter(message => message.metadata?.agent_wake === true)).toHaveLength(1);
		let state = reduceAll([
			{ type: 'turn.started', id: 'wake', seq: 1, turn_id: first.id, source: 'agent_message' },
			{ type: 'message.user', id: 'received-first', seq: 2, turn_id: first.id, queued_turn_id: first.id, content: envelope(first) }
		] as WireEvent[]);
		const receiving = project.update({ sessionId: 's1', messages: [...history, ...releaseMessages(state)],
			pendingPeerMessages: [second], sessionActive: false });
		expect(workLog(build(receiving, { ...options, isStreaming: true }), second.id).key).toBe(key);
		state = reduce(state, { type: 'message.user', id: 'received-second', seq: 3,
			turn_id: first.id, queued_turn_id: second.id, content: envelope(second) });
		expect(state.messages.map(message => message.metadata?.agent_wake === true)).toEqual([true, false]);
		const delivered = project.update({ sessionId: 's1', messages: [...history, ...releaseMessages(state)],
			pendingPeerMessages: [], sessionActive: true });
		const deliveredModel = build(delivered, { ...options, isStreaming: true });
		expect(workLog(deliveredModel, second.id).key).toBe(key);
		expect(workLog(deliveredModel, second.id).foldKey).toBe(workLog(queuedModel, second.id).foldKey);
	});

	it.each([false, true, undefined])('predicts idle wakes without changing mid-turn grouping (active=%s)', (sessionActive) => {
		const project = createAgentMessageProjection();
		const build = createTranscriptModelBuilder();
		const peer = pendingPeer();
		const answer: ChatMessage = { id: 'answer', role: 'assistant', type: 'text', content: 'Previous answer.' };
		const messages = [prompt, answer];
		const options = { isStreaming: sessionActive === true, groupingEnabled: true, sessionId: 's1' };
		const queuedRows = project.update({ sessionId: 's1', sessionActive, messages, pendingPeerMessages: [peer] });
		const queued = workLog(build(queuedRows, options));
		expect(queued.key).toBe(sessionActive === false ? 'wl-turn-peer-1' : 'wl-turn-prompt');
		expect(queuedRows.at(-1)?.metadata?.agent_wake === true).toBe(sessionActive === false);
		const received = receipt(peer, {}, sessionActive === false);
		expect(received.metadata?.agent_wake === true).toBe(sessionActive === false);
		const delivered = workLog(build(project.update({ sessionId: 's1', sessionActive,
			messages: [...messages, received], pendingPeerMessages: [] }), options));
		expect(delivered.key).toBe(queued.key);
		expect(delivered.foldKey).toBe(queued.foldKey);
	});

	it('retains an idle wake marker if turn.started makes the recipient busy before its receipt', () => {
		const project = createAgentMessageProjection();
		const peer = pendingPeer();
		const input = { sessionId: 's1', messages: [prompt], pendingPeerMessages: [peer] };
		const queued = project.update({ ...input, sessionActive: false });
		expect(project.update({ ...input, sessionActive: true })).toBe(queued);
		expect(queued.at(-1)?.metadata?.agent_wake).toBe(true);
	});
});
