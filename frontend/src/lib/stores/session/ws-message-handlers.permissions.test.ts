/**
 * Permission / question queue frames, session lock frames and api_error
 * reporting in ws-message-handlers.ts, driven through a STATEFUL harness: the
 * update() callbacks really fold into one ActiveSessionState and one attention
 * map, so these tests see what the modal queue and the sessions board show.
 */
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { createMessageHandlers, type MessageHandlerDeps, type SessionAttention } from './ws-message-handlers';
import type { ActiveSessionState } from '../active-session';
import type { PermissionRequest } from '$lib/api/client';

vi.mock('$lib/services/error-reporter', () => ({ reportClientError: vi.fn() }));
vi.mock('$lib/services/fix-with-ai', () => ({ startFixChat: vi.fn() }));
vi.mock('../notifications', () => ({ notify: { ingest: vi.fn(), error: vi.fn() } }));
vi.mock('./v2/feed-controller', () => ({
	feedController: { attach: vi.fn(), start: vi.fn(), stop: vi.fn(), restart: vi.fn(), isFeeding: vi.fn(() => false), invalidate: vi.fn() },
	OPTIMISTIC_STREAMING_ON: { isStreaming: true, optimisticStreaming: true },
}));

import { reportClientError } from '$lib/services/error-reporter';
import { notify } from '../notifications';

function perm(id: string, session_id = 'current'): PermissionRequest {
	return { request_id: id, session_id, tool_name: 'Bash', tool_input: { command: 'ls' } } as PermissionRequest;
}

function harness(initial: Partial<ActiveSessionState> = {}) {
	let state = {
		sessionId: 'current',
		error: null,
		sessionLocked: false,
		sessionLockedBy: undefined,
		pendingPermissions: [],
		deferredPermissions: [],
		pendingQuestions: [],
		...initial,
	} as unknown as ActiveSessionState;
	let attention = new Map<string, SessionAttention>();
	const deps = {
		update: (fn: (s: ActiveSessionState) => ActiveSessionState) => { state = fn(state); },
		getState: () => state,
		wsSend: vi.fn(() => true),
		streamingSessions: { update: vi.fn(), set: vi.fn() },
		backgroundPermissions: {
			update: (fn: (m: Map<string, SessionAttention>) => Map<string, SessionAttention>) => { attention = fn(attention); },
		},
		onNotifyWaitingForResponse: vi.fn(),
	} as unknown as MessageHandlerDeps;
	const { handleMessage } = createMessageHandlers(deps);
	return {
		handleMessage,
		deps,
		state: () => state,
		attention: () => attention,
		pendingIds: () => state.pendingPermissions.map((p) => p.request_id),
	};
}

describe('permission queue frames', () => {
	it('drops only the resolved requests of the open session, and tracks a background session as a count', () => {
		const h = harness({ pendingPermissions: [perm('a'), perm('b'), perm('c')] });

		h.handleMessage({ type: 'permission_queue_update', session_id: 'current', resolved_ids: ['a', 'c'], remaining_count: 1 });
		expect(h.pendingIds()).toEqual(['b']);

		// A background session's queue never touches the open modal queue; it
		// lights the sessions board with the remaining count and clears at zero.
		h.handleMessage({ type: 'permission_queue_update', session_id: 'bg', resolved_ids: ['b'], remaining_count: 2 });
		expect(h.pendingIds()).toEqual(['b']);
		expect(h.attention().get('bg')).toEqual({ count: 2, kind: 'permission' });
		h.handleMessage({ type: 'permission_queue_update', session_id: 'bg', resolved_ids: ['x'], remaining_count: 0 });
		expect(h.attention().has('bg')).toBe(false);
	});

	it('keeps the assistant_confirm wording on a live request and adds nothing to ordinary ones', () => {
		const h = harness();
		const confirm = { question: 'Do you authorize me to run `git push`? Please answer yes or no.', chat_session_id: 'chat-1' };
		h.handleMessage({ type: 'permission_request', session_id: 'current', request_id: 'ask', tool_name: 'Bash', tool_input: { command: 'git push' }, assistant_confirm: confirm });
		h.handleMessage({ type: 'permission_request', session_id: 'current', request_id: 'card', tool_name: 'Bash', tool_input: { command: 'ls' } });
		expect(h.state().pendingPermissions[0].assistant_confirm).toEqual(confirm);
		expect(h.state().pendingPermissions[1]).not.toHaveProperty('assistant_confirm');
	});

	it('accepts the requeue reply, which names requeued_ids and no resolved_ids (backend requeue_deferred_permissions)', () => {
		const h = harness({ pendingPermissions: [perm('a')], deferredPermissions: [perm('d')] });
		// Regression: `resolved_ids` is absent on this frame; the handler used to
		// call `.includes` on undefined and throw, aborting the dispatch.
		expect(() => h.handleMessage({
			type: 'permission_queue_update',
			session_id: 'current',
			requeued_ids: ['d'],
			remaining_count: 2,
		})).not.toThrow();
		expect(h.pendingIds()).toEqual(['a']);
		// The requeued request then arrives as a fresh permission_request and
		// moves from the deferred list into the modal queue exactly once.
		const rebroadcast = { type: 'permission_request', session_id: 'current', request_id: 'd', tool_name: 'Bash', tool_input: {} };
		h.handleMessage(rebroadcast);
		h.handleMessage(rebroadcast); // the device fan-out echo
		expect(h.pendingIds()).toEqual(['a', 'd']);
		expect(h.state().deferredPermissions).toEqual([]);
	});

	it.each(['permission_response_ack', 'permission_timeout'])(
		'%s removes that request only, and ignores a frame for another session',
		(type) => {
			const h = harness({ pendingPermissions: [perm('a'), perm('b')] });
			// Out-of-order: a late ack for a session this tab switched away from
			// shares a request id with the open queue; it must not close the modal.
			h.handleMessage({ type, session_id: 'previous', request_id: 'a' });
			expect(h.pendingIds()).toEqual(['a', 'b']);
			h.handleMessage({ type, session_id: 'current', request_id: 'a' });
			expect(h.pendingIds()).toEqual(['b']);
			// Without a session id (legacy frame) it applies to the open session.
			h.handleMessage({ type, request_id: 'b' });
			expect(h.pendingIds()).toEqual([]);
		}
	);

	it('keeps a question open when the answer could not be relayed, and closes it on any other failed ack', () => {
		const q = { request_id: 'q1', session_id: 'current', tool_use_id: 't', questions: [] };
		const h = harness({ pendingQuestions: [q] as never });
		h.handleMessage({ type: 'user_question_response_ack', session_id: 'current', request_id: 'q1', success: false, reason: 'relay_offline' });
		expect(h.state().pendingQuestions).toHaveLength(1);
		expect(notify.error).toHaveBeenCalledOnce();
		// Another session's ack is ignored even when the id matches.
		h.handleMessage({ type: 'user_question_timeout', session_id: 'other', request_id: 'q1' });
		expect(h.state().pendingQuestions).toHaveLength(1);
		h.handleMessage({ type: 'user_question_response_ack', session_id: 'current', request_id: 'q1', success: false, reason: 'not_pending' });
		expect(h.state().pendingQuestions).toHaveLength(0);
	});
});

describe('session lock frames', () => {
	it('locks on session_locked and clears the lock (keeping an explanation) on session_takeover', () => {
		const h = harness();
		h.handleMessage({ type: 'session_locked', locked_by: 'phone-1' });
		expect(h.state()).toMatchObject({ sessionLocked: true, sessionLockedBy: 'phone-1' });
		expect(h.state().error).toMatch(/Take Over/);

		h.handleMessage({ type: 'session_takeover', new_device: 'laptop-2' });
		expect(h.state().sessionLocked).toBe(false);
		expect(h.state().sessionLockedBy).toBeUndefined();
		expect(h.state().error).toBe('This session was taken over by another device.');
	});
});

describe('api_error reporting', () => {
	beforeEach(() => vi.mocked(reportClientError).mockClear());

	it.each([
		[{ status: 429 }, 'Rate limited — slow down or wait for the next window.', 429],
		[{ status: 503 }, 'Service error: HTTP 503', 503],
		[{ status: 400 }, 'API error: HTTP 400', 400],
		[{ status: 'garbage' }, 'An API error occurred.', undefined],
		[{ status: 529, message: 'Overloaded' }, 'Overloaded', 529],
	])('logs %j as %s', (frame, message, status_code) => {
		const h = harness();
		h.handleMessage({ type: 'api_error', ...frame });
		expect(reportClientError).toHaveBeenCalledExactlyOnceWith({
			source: 'ws',
			message,
			status_code,
			session_id: 'current',
		});
		// Logged, not rendered: the notification funnel owns the banner.
		expect(h.state().error).toBeNull();
	});
});
