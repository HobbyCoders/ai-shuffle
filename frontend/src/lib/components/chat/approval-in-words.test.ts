/**
 * Approvals asked in words (#483, #484): replayed through the real v2 reducer
 * and rendered through the approval rows MessageArea uses.
 *
 * - one rendering per request, keyed by request id (no floating copy);
 * - an answered or expired request keeps its position and its outcome;
 * - two jobs asking at once read request, request, answer, outcome, answer,
 *   outcome, each outcome naming the request it settled;
 * - a retry is a new, numbered card, distinct from the one it replaces;
 * - live (event by event) and reload (replay from scratch) render the same.
 */
import { afterEach, describe, expect, it } from 'vitest';
import { cleanup, render } from '@testing-library/svelte';
import ApprovalTranscript from './test-fixtures/ApprovalTranscript.svelte';
import { initialState, reduce, reduceAll, type WireEvent } from '$lib/stores/session/v2/reducer';
import type { PermissionRequest } from '$lib/api/client';
import type { ChatMessage } from '$lib/stores/types';

afterEach(cleanup);

let seq = 0;
function ev(type: string, fields: Record<string, unknown>): WireEvent {
	seq += 1;
	return { id: `e${seq}`, seq, type, session_id: 'home', created_at: `2026-10-23T12:00:${String(seq).padStart(2, '0')}Z`, ...fields };
}

const question = (command: string, job: string) =>
	`The background job “${job}” asks: do you authorize it to run \`${command}\`? Please answer yes or no.`;

function requested(requestId: string, command: string, job: string): WireEvent {
	return ev('interaction.requested', {
		interaction_id: `i-${requestId}`,
		request_id: requestId,
		kind: 'permission',
		request: {
			request_id: requestId,
			tool_name: 'Bash',
			tool_input: { command },
			assistant_confirm: { question: question(command, job), action: `run \`${command}\``, asker: job, chat_session_id: `chat-${job}` }
		}
	});
}
const answered = (requestId: string, decision: 'allow' | 'deny' | 'defer') =>
	ev('interaction.answered', { interaction_id: `i-${requestId}`, request_id: requestId, answer: { decision, behavior: decision } });
const expired = (requestId: string, reason: 'ttl' | 'cancelled') =>
	ev('interaction.expired', { interaction_id: `i-${requestId}`, request_id: requestId, reason });
const user = (content: string) => ev('message.user', { content });
const outcome = (requestId: string, decision: string, command: string, job: string) =>
	ev('system.note', {
		subtype: 'assistant_confirm',
		data: { request_id: requestId, decision, question: question(command, job), action: `run \`${command}\``, asker: job }
	});

function pending(requestId: string, command: string, job: string): PermissionRequest {
	return {
		request_id: requestId,
		tool_name: 'Bash',
		tool_input: { command },
		assistant_confirm: { question: question(command, job), chat_session_id: `chat-${job}` }
	} as PermissionRequest;
}

/** Row kinds in order: card:<id>:<status>, outcome:<id>:<decision>, user:<text>. */
function rows(container: HTMLElement): string[] {
	return [...container.querySelectorAll<HTMLElement>('[data-testid="row"]')].map((row) => {
		const card = row.querySelector<HTMLElement>('[data-approval-card]');
		if (card) return `card:${card.dataset.approvalCard}:${card.dataset.status}`;
		const result = row.querySelector<HTMLElement>('[data-request-id]');
		if (result) return `outcome:${result.dataset.requestId}:${result.dataset.decision}`;
		const p = row.querySelector('p');
		return `${p?.dataset.role}:${p?.textContent}`;
	});
}

function cardFor(container: HTMLElement, requestId: string): HTMLElement {
	const cards = container.querySelectorAll<HTMLElement>(`[data-approval-card="${requestId}"]`);
	expect(cards).toHaveLength(1);
	return cards[0];
}

const statusOf = (card: HTMLElement) => card.querySelector('[data-testid="approval-status"]')?.textContent?.trim();

describe('approvals asked in words', () => {
	it('shows each request once, keyed by its request id, and the model text never stands in for it', () => {
		seq = 0;
		const state = reduceAll([
			user('start both jobs'),
			requested('perm-a', 'git push', 'Release'),
			requested('perm-b', 'rm -rf build', 'Cleanup'),
			ev('message.assistant', { content: 'Both jobs are running.' })
		]);
		const view = render(ApprovalTranscript, {
			messages: state.messages,
			pendingPermissions: [pending('perm-a', 'git push', 'Release'), pending('perm-b', 'rm -rf build', 'Cleanup')]
		});
		cardFor(view.container, 'perm-a');
		cardFor(view.container, 'perm-b');
		// The full question text is not printed anywhere: no second rendering.
		expect(view.container.textContent).not.toContain('Please answer yes or no.');
		// The oldest one is the one being asked; the other is next.
		expect(statusOf(cardFor(view.container, 'perm-a'))).toBe('Waiting for you');
		expect(statusOf(cardFor(view.container, 'perm-b'))).toBe('Next');
		expect(cardFor(view.container, 'perm-a').textContent).toContain('Release');
		expect(cardFor(view.container, 'perm-a').querySelector('code')?.textContent).toBe('git push');
		expect(view.container.querySelectorAll('button')).toHaveLength(0);
	});

	it('replays two jobs interleaved as request, answer, outcome, each outcome naming its request', () => {
		seq = 0;
		const events = [
			user('start both jobs'),
			requested('perm-a', 'git push', 'Release'),
			requested('perm-b', 'rm -rf build', 'Cleanup'),
			answered('perm-a', 'allow'),
			user('yes'),
			outcome('perm-a', 'approved', 'git push', 'Release'),
			answered('perm-b', 'deny'),
			user('no'),
			outcome('perm-b', 'declined', 'rm -rf build', 'Cleanup')
		];
		const view = render(ApprovalTranscript, { messages: reduceAll(events).messages });
		expect(rows(view.container)).toEqual([
			'user:start both jobs',
			'card:perm-a:approved',
			'card:perm-b:declined',
			'user:yes',
			'outcome:perm-a:approved',
			'user:no',
			'outcome:perm-b:declined'
		]);
		const [first, second] = view.container.querySelectorAll<HTMLElement>('[data-request-id]');
		expect(first.textContent).toContain('Authorized');
		expect(first.textContent).toContain('git push');
		expect(second.textContent).toContain('Declined');
		expect(second.textContent).toContain('rm -rf build');
	});

	it('keeps an answered or expired request where it was, with its outcome, as later requests arrive', async () => {
		seq = 0;
		const events = [
			requested('perm-a', 'git push', 'Release'),
			requested('perm-b', 'npm publish', 'Release'),
			answered('perm-a', 'allow'),
			user('yes'),
			outcome('perm-a', 'approved', 'git push', 'Release'),
			expired('perm-b', 'ttl'),
			requested('perm-c', 'ls', 'Audit'),
			expired('perm-c', 'cancelled')
		];
		let state = initialState();
		const view = render(ApprovalTranscript, { messages: state.messages });
		const positions: number[] = [];
		for (const event of events) {
			state = reduce(state, event);
			await view.rerender({ messages: state.messages });
			const order = rows(view.container);
			if (order.some((row) => row.startsWith('card:perm-a:'))) {
				positions.push(order.findIndex((row) => row.startsWith('card:perm-a:')));
			}
		}
		// perm-a never moves.
		expect(new Set(positions)).toEqual(new Set([0]));
		expect(rows(view.container)).toEqual([
			'card:perm-a:approved',
			'card:perm-b:expired',
			'user:yes',
			'outcome:perm-a:approved',
			'card:perm-c:cancelled'
		]);
		expect(statusOf(cardFor(view.container, 'perm-a'))).toBe('Authorized');
		expect(statusOf(cardFor(view.container, 'perm-b'))).toBe('Expired');
		expect(statusOf(cardFor(view.container, 'perm-c'))).toBe('Cancelled');
	});

	it('marks a retry of the same request as a new, numbered request', () => {
		seq = 0;
		const state = reduceAll([
			requested('perm-1', 'git push', 'Release'),
			expired('perm-1', 'ttl'),
			requested('perm-2', 'git push', 'Release'),
			answered('perm-2', 'deny'),
			requested('perm-3', 'git push', 'Release')
		]);
		const view = render(ApprovalTranscript, {
			messages: state.messages,
			pendingPermissions: [pending('perm-3', 'git push', 'Release')]
		});
		expect(cardFor(view.container, 'perm-1').textContent).not.toContain('Asked again');
		expect(cardFor(view.container, 'perm-2').textContent).toContain('Asked again · 2nd request');
		expect(cardFor(view.container, 'perm-3').textContent).toContain('Asked again · 3rd request');
		expect(statusOf(cardFor(view.container, 'perm-3'))).toBe('Waiting for you');
		expect(statusOf(cardFor(view.container, 'perm-1'))).toBe('Expired');
	});

	it('renders the same live and after a reload', async () => {
		seq = 0;
		const events = [
			user('start both jobs'),
			requested('perm-a', 'git push', 'Release'),
			ev('assistant.text_delta', { text: 'Working on it' }),
			requested('perm-b', 'rm -rf build', 'Cleanup'),
			answered('perm-a', 'allow'),
			user('yes'),
			outcome('perm-a', 'approved', 'git push', 'Release'),
			answered('perm-b', 'defer'),
			requested('perm-b', 'rm -rf build', 'Cleanup'),
			answered('perm-b', 'deny'),
			user('no'),
			outcome('perm-b', 'declined', 'rm -rf build', 'Cleanup')
		];
		let live = initialState();
		const liveView = render(ApprovalTranscript, { messages: live.messages });
		for (const event of events) {
			live = reduce(live, event);
			await liveView.rerender({ messages: live.messages });
		}
		const liveHtml = liveView.container.innerHTML;
		cleanup();
		const reloaded = render(ApprovalTranscript, { messages: reduceAll(events).messages });
		expect(reloaded.container.innerHTML).toBe(liveHtml);
		// The deferred re-ask reused its card: still one per request id.
		cardFor(reloaded.container, 'perm-b');
		expect(rows(reloaded.container).filter((row) => row.startsWith('card:'))).toEqual([
			'card:perm-a:approved',
			'card:perm-b:declined'
		]);
	});

	it('leaves ordinary prompts and AskUserQuestion requests to their own cards', () => {
		seq = 0;
		const state = reduceAll([
			ev('interaction.requested', {
				interaction_id: 'i-x', request_id: 'perm-x', kind: 'permission',
				request: { request_id: 'perm-x', tool_name: 'Bash', tool_input: { command: 'ls' } }
			}),
			ev('interaction.requested', {
				interaction_id: 'i-q', request_id: 'ask-1', kind: 'question',
				request: { request_id: 'ask-1', questions: [{ question: 'Which one?' }] }
			}),
			requested('perm-a', 'git push', 'Release'),
			answered('perm-x', 'allow'),
			expired('ask-1', 'cancelled')
		]);
		expect(state.messages.map((m: ChatMessage) => m.id)).toEqual(['assistant-confirm-perm-a']);
		expect(state.messages[0].systemData?.status).toBe('pending');
	});
});
