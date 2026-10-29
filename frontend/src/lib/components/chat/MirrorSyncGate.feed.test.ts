/**
 * #2227: a mirrored chat's later sync drains must not rebuild its transcript.
 *
 * Wires MirrorSyncGate to the REAL feed controller the way ChatView does
 * (onCaughtUp → activeSession.reloadSession() → feedController.restart) and
 * replays the owner's sync-status sequence. Only the SSE transport is faked.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { cleanup, render } from '@testing-library/svelte';
import { createRawSnippet } from 'svelte';

const feeds = vi.hoisted(() => ({
	onEvent: null as ((e: Record<string, unknown>) => void) | null,
	opened: [] as number[],
}));

vi.mock('$lib/stores/session/v2/sse-feed', () => ({
	createSseFeed: (_id: string, opts: { onEvent: (e: Record<string, unknown>) => void; fromSeq?: number }) => {
		feeds.onEvent = opts.onEvent;
		feeds.opened.push(opts.fromSeq ?? 0);
		return {
			get running() { return true; },
			get lastSeq() { return 0; },
			get epoch() { return 'e1'; },
			revive() {},
			stop() {},
		};
	},
}));

import MirrorSyncGate from './MirrorSyncGate.svelte';
import { feedController } from '$lib/stores/session/v2/feed-controller';
import type { ActiveSessionState } from '$lib/stores/active-session';

const children = createRawSnippet(() => ({ render: () => '<div>transcript</div>' }));
const status = (state: string, pending_updates = 0) => new Response(JSON.stringify({ state, pending_updates }));
const userMessage = (seq: number) => ({
	type: 'message.user', id: `evt-${seq}`, seq, session_id: 's1',
	created_at: '2026-10-11T00:00:00.000Z', content: `message ${seq}`,
});

/** Every state the feed publishes, so a transient empty transcript is caught. */
function attachRecordingDeps(initial: Partial<ActiveSessionState> = {}) {
	let state = {
		sessionId: 's1', messages: [], isStreaming: false, activeTasks: [], queuedMessages: [],
		pendingPeerMessages: [], pendingPermissions: [], deferredPermissions: [], pendingQuestions: [],
		...initial,
	} as unknown as ActiveSessionState;
	const published: ActiveSessionState[] = [];
	feedController.attach({
		update: (fn) => { state = fn(state); published.push(state); },
		getState: () => state,
		requestPendingQuestions: () => {},
		requestPendingPermissions: () => {},
		resolveInteractionByRequestId: () => {},
	});
	return { published, get state() { return state; } };
}

beforeEach(() => {
	feeds.onEvent = null;
	feeds.opened = [];
	vi.useFakeTimers();
	// Fold synchronously-enough: the controller coalesces live folds onto rAF.
	vi.stubGlobal('requestAnimationFrame', (cb: FrameRequestCallback) => setTimeout(() => cb(0), 0));
	vi.stubGlobal('cancelAnimationFrame', (id: number) => clearTimeout(id));
});
afterEach(() => {
	cleanup();
	feedController.stop();
	feedController.invalidate();
	vi.useRealTimers();
	vi.unstubAllGlobals();
});

describe('mirrored chat sync drains (#2227)', () => {
	it.each(['current', 'opening-checkpoint'])('keeps the transcript and feed cursor after %s', async (initial) => {
		const deps = attachRecordingDeps();
		const fetch = vi.fn()
			.mockResolvedValueOnce(initial === 'current' ? status('current') : new Response(JSON.stringify({
				state: 'syncing', pending_updates: 8, initial_caught_up: true,
			})))
			.mockResolvedValueOnce(status('syncing', 5))
			.mockResolvedValueOnce(status('current'))
			.mockResolvedValueOnce(status('syncing', 3))
			.mockImplementation(() => Promise.resolve(status('current')));
		vi.stubGlobal('fetch', fetch);
		const reloadSession = vi.fn(() => feedController.restart('s1'));

		feedController.start('s1');
		render(MirrorSyncGate, { sessionId: 's1', remote: true, deviceName: 'ServerWorker', onCaughtUp: reloadSession, children });
		await vi.advanceTimersByTimeAsync(0);
		// First catch-up: the one allowed full rebuild.
		expect(reloadSession).toHaveBeenCalledOnce();
		for (const seq of [1, 2, 3]) feeds.onEvent!(userMessage(seq));
		feeds.onEvent!({ type: 'caught_up', seq: 3 });
		expect(deps.state.messages).toHaveLength(3);
		const opensAfterHydration = feeds.opened.length;
		const publishedAfterHydration = deps.published.length;

		// The live feed keeps delivering replicated events between polls.
		let seq = 3;
		for (let poll = 0; poll < 6; poll++) {
			feeds.onEvent!(userMessage(++seq));
			await vi.advanceTimersByTimeAsync(2000);
		}

		expect(fetch.mock.calls.length).toBeGreaterThanOrEqual(5);
		expect(reloadSession).toHaveBeenCalledOnce();
		// No new SSE connection at all — in particular none from sequence zero.
		expect(feeds.opened).toHaveLength(opensAfterHydration);
		const later = deps.published.slice(publishedAfterHydration);
		expect(later.length).toBeGreaterThan(0);
		expect(later.every((s) => s.messages.length >= 3 && !s.isLoadingSession)).toBe(true);
		expect(deps.state.messages).toHaveLength(9);
	});

	it('keeps an unconfirmed first send through the opening sync and empty replay', async () => {
		const echo = { id: 'user-first', role: 'user' as const, content: 'first prompt' };
		const deps = attachRecordingDeps({ messages: [echo], isStreaming: true, optimisticStreaming: true });
		vi.stubGlobal('fetch', vi.fn().mockImplementation(() => Promise.resolve(status('current'))));
		feedController.start('s1');
		feeds.onEvent!({ type: 'caught_up', seq: 0, epoch: '0' });
		render(MirrorSyncGate, { sessionId: 's1', remote: true, onCaughtUp: () => feedController.restart('s1'), children });
		await vi.advanceTimersByTimeAsync(0);
		feeds.onEvent!({ type: 'caught_up', seq: 0, epoch: '0' });

		expect(feeds.opened).toEqual([0, 0]);
		// Every publication counts: even a one-frame empty fold centres the composer.
		expect(deps.published.every((s) => s.messages.length === 1 && s.messages[0].id === echo.id)).toBe(true);
		expect(deps.state.isStreaming).toBe(true);
		// Replication catches up: the actual event reconciles the SAME row once.
		feeds.onEvent!({ ...userMessage(1), content: echo.content });
		await vi.advanceTimersByTimeAsync(0);
		expect(deps.state.messages).toMatchObject([{ id: echo.id, serverId: 'evt-1', content: echo.content }]);

		// A confirmed row is history, not a pending send. A real clear/rewind
		// must still discard it, including its optimistic-looking stable id.
		feedController.restart('s1');
		feeds.onEvent!({ type: 'caught_up', seq: 0 });
		expect(deps.state.messages).toEqual([]);
	});

	it('settles a preserved first send when its completed turn replays in one batch', async () => {
		const echo = { id: 'user-first', role: 'user' as const, content: 'first prompt' };
		const deps = attachRecordingDeps({ messages: [echo], isStreaming: true, optimisticStreaming: true });
		feedController.start('s1');
		feeds.onEvent!({ type: 'caught_up', seq: 0 });
		feedController.restart('s1');
		feeds.onEvent!({ ...userMessage(1), content: echo.content });
		feeds.onEvent!({ type: 'turn.started', id: 'start', seq: 2, session_id: 's1', turn_id: 't1' });
		feeds.onEvent!({ type: 'turn.completed', id: 'end', seq: 3, session_id: 's1', turn_id: 't1' });
		feeds.onEvent!({ type: 'caught_up', seq: 3 });
		await vi.advanceTimersByTimeAsync(0);

		expect(deps.state.messages).toHaveLength(1);
		expect(deps.state.isStreaming).toBe(false);
		expect(deps.state.optimisticStreaming).toBe(false);
	});

	it('still rebuilds from zero when the server says the log was rewritten', async () => {
		const deps = attachRecordingDeps();
		vi.stubGlobal('fetch', vi.fn().mockImplementation(() => Promise.resolve(status('current'))));
		feedController.start('s1');
		render(MirrorSyncGate, { sessionId: 's1', remote: true, onCaughtUp: () => feedController.restart('s1'), children });
		await vi.advanceTimersByTimeAsync(0);
		feeds.onEvent!(userMessage(1));
		feeds.onEvent!({ type: 'caught_up', seq: 1 });
		const opens = feeds.opened.length;
		feeds.onEvent!({ type: 'reset' });
		expect(feeds.opened).toHaveLength(opens + 1);
		expect(feeds.opened.at(-1)).toBe(0);
		expect(deps.state.messages).toHaveLength(0);
	});
});
