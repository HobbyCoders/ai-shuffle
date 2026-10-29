/** Durable queue identity must survive the real work-log component's keyed lists. */
import { afterEach, describe, expect, it } from 'vitest';
import { cleanup, fireEvent, render, screen } from '@testing-library/svelte';
import type { ChatMessage, PendingPeerMessage } from '$lib/stores/types';
import { reduce, reduceAll, releaseMessages, type WireEvent } from '$lib/stores/session/v2/reducer';
import AgentMessageTranscript from './test-fixtures/AgentMessageTranscript.svelte';
import { chatPreferences } from '$lib/stores/chatPreferences.svelte';

const pending: PendingPeerMessage = {
	id: 'peer-one', senderLabel: 'Review session', senderSessionId: 'review-session', content: 'Same message body', queuedAt: 1
};
const busy: ChatMessage[] = [
	{ id: 'prompt', role: 'user', content: 'Start review', turnId: 'running-turn' },
	{ id: 'reply', role: 'assistant', type: 'text', content: 'Reviewing now.', turnId: 'running-turn', streaming: true }
];
function receipt(id = pending.id, turnId = 'running-turn'): ChatMessage {
	const content = `[agent\x20message from='${pending.senderSessionId}' — a message from another agent, delivered once at this position; not part of the tool output, and not a new message when re-read from history]\n${pending.content}\n[/agent\x20message]`;
	return { id: `event-${id}`, serverId: `server-${id}`, role: 'user', type: 'text',
		content, turnId, queuedTurnId: id };
}
function rows() { return screen.queryAllByRole('button', { name: /^Agent message/ }); }

afterEach(() => { cleanup(); chatPreferences.reset(); });

describe('AgentMessage transcript replacement', () => {
	it('replaces a busy recipient queue in place, even when queue-clear precedes receipt', async () => {
		const view = render(AgentMessageTranscript, { messages: busy, pendingPeerMessages: [pending] });
		const row = rows()[0];
		expect(row).toBeTruthy();
		await fireEvent.click(row);
		const result = screen.getByText(pending.content);
		const input = view.container.querySelector('.wl-pane pre')?.textContent;
		await view.rerender({ messages: busy, pendingPeerMessages: [] });
		expect(rows()).toEqual([row]);
		expect(row.getAttribute('aria-label')).toContain('Queued');
		await view.rerender({ messages: [...busy, receipt()], pendingPeerMessages: [] });
		expect(rows()).toEqual([row]);
		expect(row.getAttribute('aria-expanded')).toBe('true');
		expect(row.getAttribute('aria-label')).toContain('Complete');
		expect(screen.getByText(pending.content)).toBe(result);
		expect(view.container.querySelector('.wl-pane pre')?.textContent).toBe(input);
	});

	it('keeps sender and input unchanged for a real user/text receipt and after reload', async () => {
		const view = render(AgentMessageTranscript, { messages: busy, pendingPeerMessages: [pending] });
		const row = rows()[0];
		await fireEvent.click(row);
		const label = row.getAttribute('aria-label')?.replace(/Queued$/, 'Complete');
		const input = view.container.querySelector('.wl-pane pre')?.textContent;
		const envelope = `[agent\x20message from='${pending.senderSessionId}' — a message from another agent, delivered once at this position; not part of the tool output, and not a new message when re-read from history]\n${pending.content}\n[/agent\x20message]`;
		const received: ChatMessage = { id: 'real-receipt', role: 'user', type: 'text', content: envelope,
			turnId: 'running-turn', queuedTurnId: pending.id };
		await view.rerender({ messages: [...busy, received], pendingPeerMessages: [] });
		expect(rows()).toEqual([row]);
		expect(row.getAttribute('aria-label')).toBe(label);
		expect(row.getAttribute('aria-expanded')).toBe('true');
		expect(view.container.querySelector('.wl-pane pre')?.textContent).toBe(input);
		expect(screen.getByText(pending.content)).toBeTruthy();
		view.unmount();
		const reopened = render(AgentMessageTranscript, { messages: [...busy, received], pendingPeerMessages: [] });
		expect(rows()[0].getAttribute('aria-label')).toBe(label);
		await fireEvent.click(rows()[0]);
		expect(reopened.container.querySelector('.wl-pane pre')?.textContent).toBe(input);
	});

	it('does not resurrect a delivered row from a stale queue snapshot', async () => {
		const view = render(AgentMessageTranscript, { messages: busy, pendingPeerMessages: [pending] });
		const row = rows()[0];
		await view.rerender({ messages: [...busy, receipt()], pendingPeerMessages: [] });
		await view.rerender({ messages: [...busy, receipt()], pendingPeerMessages: [pending] });
		expect(rows()).toEqual([row]);
		expect(screen.queryByText('Queued')).toBeNull();
	});

	it('matches two identical bodies only by durable queued turn id', async () => {
		const second = { ...pending, id: 'peer-two', queuedAt: 2 };
		const view = render(AgentMessageTranscript, { messages: busy, pendingPeerMessages: [pending, second] });
		const [firstRow, secondRow] = rows();
		await fireEvent.click(secondRow);
		await view.rerender({ messages: [...busy, receipt()], pendingPeerMessages: [pending, second] });
		expect(rows()).toEqual([firstRow, secondRow]);
		expect(firstRow.getAttribute('aria-label')).toContain('Complete');
		expect(secondRow.getAttribute('aria-label')).toContain('Queued');
		expect(secondRow.getAttribute('aria-expanded')).toBe('true');
		await view.rerender({ messages: [...busy, receipt(), receipt(second.id)], pendingPeerMessages: [] });
		expect(rows()).toEqual([firstRow, secondRow]);
		expect(secondRow.getAttribute('aria-expanded')).toBe('true');
		expect(screen.queryByText('Queued')).toBeNull();
	});

	it('keeps the expanded Send now peer mounted across interrupt and successor receipt', async () => {
		let state = reduceAll([
			{ type: 'turn.started', id: 'start', seq: 1, turn_id: 'running-turn' },
			{ type: 'message.user', id: 'prompt', seq: 2, turn_id: 'running-turn', content: 'Start review' }
		] as WireEvent[]);
		const view = render(AgentMessageTranscript, { messages: releaseMessages(state), pendingPeerMessages: [pending],
			live: state.isStreaming });
		const row = rows()[0];
		await fireEvent.click(row);
		row.focus();
		const body = screen.getByText(pending.content);
		const groupKey = row.closest('[data-item-key]')?.getAttribute('data-item-key');
		state = reduce(state, { type: 'turn.aborted', id: 'send-now-interrupt', seq: 3, turn_id: 'running-turn', reason: 'interrupt' });
		await view.rerender({ messages: releaseMessages(state), pendingPeerMessages: [pending],
			live: state.isStreaming });
		expect(rows()).toEqual([row]);
		// The queue clears while the real reducer still reports no running turn.
		expect(state).toMatchObject({ isStreaming: false, parked: false, liveTurnId: null });
		await view.rerender({ messages: releaseMessages(state), pendingPeerMessages: [], live: state.isStreaming });
		expect(rows()).toEqual([row]);
		expect(document.activeElement).toBe(row);
		state = reduce(state, { type: 'turn.started', id: 'successor', seq: 4, turn_id: 'successor-turn', source: 'user' });
		await view.rerender({ messages: releaseMessages(state), pendingPeerMessages: [],
			live: state.isStreaming });
		expect(rows()).toEqual([row]);
		expect(row.getAttribute('aria-expanded')).toBe('true');
		expect(document.activeElement).toBe(row);
		expect(screen.getByText(pending.content)).toBe(body);
		state = reduce(state, { type: 'message.user', id: 'peer-receipt', seq: 5,
			turn_id: 'successor-turn', queued_turn_id: pending.id, content: receipt().content });
		await view.rerender({ messages: releaseMessages(state), pendingPeerMessages: [],
			live: state.isStreaming });
		expect(rows()).toEqual([row]);
		expect(row.getAttribute('aria-expanded')).toBe('true');
		expect(row.getAttribute('aria-label')).toContain('Complete');
		expect(document.activeElement).toBe(row);
		expect(screen.getByText(pending.content)).toBe(body);
		expect(row.closest('[data-item-key]')?.getAttribute('data-item-key')).toBe(groupKey);
	});

	it('retires an empty chat peer when its first event is cancellation and blocks stale snapshots', async () => {
		const view = render(AgentMessageTranscript, { messages: [], pendingPeerMessages: [pending], live: false });
		const row = rows()[0];
		await view.rerender({ messages: [], pendingPeerMessages: [], live: false });
		expect(rows()).toEqual([row]);
		const cancelled = reduceAll([{ type: 'queue.cancelled', id: 'cancelled', seq: 1,
			cancelled_turn_ids: [pending.id] }] as WireEvent[]);
		expect(cancelled.messages).toEqual([]); // No dummy transcript initialization event.
		expect(cancelled.cancelledPeerTurnIds).toEqual([pending.id]);
		await view.rerender({ messages: [], pendingPeerMessages: [], live: false, cancelledTurnIds: cancelled.cancelledPeerTurnIds });
		expect(rows()).toHaveLength(0);
		await view.rerender({ messages: [], pendingPeerMessages: [pending], live: false });
		expect(rows()).toHaveLength(0);
	});

	it('keeps the second idle peer expanded and focused through both real receipts', async () => {
		const second = { ...pending, id: 'peer-two', content: 'Second idle message body', queuedAt: 2 };
		const settled = busy.map(message => ({ ...message, streaming: false }));
		const view = render(AgentMessageTranscript, { messages: settled, pendingPeerMessages: [pending, second], live: false });
		const [firstRow, secondRow] = rows();
		await fireEvent.click(secondRow);
		secondRow.focus();
		const body = screen.getByText(second.content);
		const groupKey = secondRow.closest('[data-item-key]')?.getAttribute('data-item-key');
		expect(groupKey).toBe(firstRow.closest('[data-item-key]')?.getAttribute('data-item-key'));
		let state = reduceAll([
			{ type: 'turn.started', id: 'wake', seq: 1, turn_id: pending.id, source: 'agent_message' },
			{ type: 'message.user', id: 'first-receipt', seq: 2, turn_id: pending.id, queued_turn_id: pending.id, content: receipt().content }
		] as WireEvent[]);
		await view.rerender({ messages: [...settled, ...releaseMessages(state)], pendingPeerMessages: [second], live: true });
		expect(rows()).toEqual([firstRow, secondRow]);
		expect(secondRow.getAttribute('aria-expanded')).toBe('true');
		expect(document.activeElement).toBe(secondRow);
		state = reduce(state, { type: 'message.user', id: 'second-receipt', seq: 3,
			turn_id: pending.id, queued_turn_id: second.id, content: receipt().content.replace(pending.content, second.content) });
		await view.rerender({ messages: [...settled, ...releaseMessages(state)], pendingPeerMessages: [], live: true });
		expect(rows()).toEqual([firstRow, secondRow]);
		expect(secondRow.getAttribute('aria-expanded')).toBe('true');
		expect(secondRow.getAttribute('aria-label')).toContain('Complete');
		expect(document.activeElement).toBe(secondRow);
		expect(screen.getByText(second.content)).toBe(body);
		expect(secondRow.closest('[data-item-key]')?.getAttribute('data-item-key')).toBe(groupKey);
	});

	it('restores only this session queue after switching away and back', async () => {
		const view = render(AgentMessageTranscript, { sessionId: 'recipient', messages: busy, pendingPeerMessages: [pending] });
		await view.rerender({ sessionId: 'other-session', messages: [], pendingPeerMessages: [] });
		expect(rows()).toHaveLength(0);
		await view.rerender({ sessionId: 'recipient', messages: busy, pendingPeerMessages: [] });
		expect(rows()).toHaveLength(0);
		await view.rerender({ sessionId: 'recipient', messages: busy, pendingPeerMessages: [pending] });
		expect(rows()).toHaveLength(1);
		expect(rows()[0].getAttribute('aria-label')).toContain('Queued');
		await view.rerender({ sessionId: 'recipient', messages: [...busy, receipt()], pendingPeerMessages: [pending] });
		expect(rows()).toHaveLength(1);
		expect(rows()[0].getAttribute('aria-label')).toContain('Complete');
	});

	it('keeps an idle recipient peer in the same group when its wake turn starts', async () => {
		const settled = busy.map(message => ({ ...message, streaming: false }));
		const view = render(AgentMessageTranscript, { messages: settled, pendingPeerMessages: [pending], live: false });
		const row = rows()[0];
		expect(row).toBeTruthy();
		await fireEvent.click(row);
		const wake = { ...receipt(pending.id, pending.id), metadata: { agent_wake: true } };
		await view.rerender({ messages: [...settled, wake], pendingPeerMessages: [], live: true });
		expect(rows()).toEqual([row]);
		expect(row.getAttribute('aria-expanded')).toBe('true');
		expect(row.getAttribute('aria-label')).toContain('Complete');
	});

	it('keeps the same disclosure when the peer starts an otherwise empty recipient', async () => {
		const view = render(AgentMessageTranscript, { messages: [], pendingPeerMessages: [pending] });
		const row = rows()[0];
		await fireEvent.click(row);
		await view.rerender({ messages: [receipt(pending.id, pending.id)], pendingPeerMessages: [] });
		expect(rows()).toEqual([row]);
		expect(row.getAttribute('aria-expanded')).toBe('true');
	});
});
