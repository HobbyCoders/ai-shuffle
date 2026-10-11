/**
 * The trailing reading-position write (#1282) must never lose the leaving
 * session's position or file it under the session being entered.
 *
 * The A→B→A case drives the REAL session switcher and ephemeral cache, wired
 * the way MessageArea wires them: scroll frames schedule the write, and the
 * session-switch effect (which runs after the switcher has already saved the
 * leaving session's snapshot and loaded the target into the store) calls
 * `leave(prevId, position)`.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { createScrollTopPersist } from './scroll-top-persist';
import { sessionEphemeralCache } from '$lib/stores/session-cache';
import { createSessionSwitcher, type SessionSwitcherDeps } from '$lib/stores/session/session-switcher';
import type { ActiveSessionState } from '$lib/stores/active-session';
import type { InternalMutableState } from '$lib/stores/session/ws-message-handlers';

vi.mock('$lib/stores/app-data.svelte', () => ({
	appData: { get current() { return { sessions: [] }; } }
}));
vi.mock('$lib/api/client', () => ({
	api: { get: vi.fn(async () => null), put: vi.fn(async () => null) }
}));
vi.mock('$lib/stores/session/v2/feed-controller', () => ({
	feedController: {
		attach: vi.fn(), start: vi.fn(), stop: vi.fn(),
		isFeeding: vi.fn(() => false),
		cachedPatch: vi.fn(() => null),
		hasCachedTranscript: vi.fn(() => false),
		invalidate: vi.fn(),
		hasAppliedUsage: vi.fn(() => false),
		isReducerStreaming: vi.fn(() => false),
		noteOptimisticSend: vi.fn(),
		clearOptimisticSend: vi.fn()
	},
	OPTIMISTIC_STREAMING_ON: { isStreaming: true, optimisticStreaming: true }
}));

const DELAY = 250;

describe('createScrollTopPersist', () => {
	beforeEach(() => vi.useFakeTimers());
	afterEach(() => vi.useRealTimers());

	function harness(current = 'A') {
		const key = { current };
		const writeCurrent = vi.fn();
		const writeLeft = vi.fn();
		const persist = createScrollTopPersist({ delayMs: DELAY, currentKey: () => key.current, writeCurrent, writeLeft });
		return { key, persist, writeCurrent, writeLeft };
	}

	it('writes once, with the last position, after scrolling pauses', () => {
		const { persist, writeCurrent } = harness();
		persist.schedule('A', 100);
		vi.advanceTimersByTime(DELAY - 1);
		persist.schedule('A', 180);
		vi.advanceTimersByTime(DELAY - 1);
		expect(writeCurrent).not.toHaveBeenCalled();
		vi.advanceTimersByTime(1);
		expect(writeCurrent).toHaveBeenCalledTimes(1);
		expect(writeCurrent).toHaveBeenCalledWith(180);
	});

	it('drops a pending write when the active session changed before it fired', () => {
		const { key, persist, writeCurrent } = harness();
		persist.schedule('A', 100);
		key.current = 'B';
		vi.advanceTimersByTime(DELAY);
		expect(writeCurrent).not.toHaveBeenCalled();
	});

	it('files a leaving position under the leaving key and never into the current store', () => {
		const { key, persist, writeCurrent, writeLeft } = harness();
		persist.schedule('A', 100);
		key.current = 'B';
		persist.leave('A', 140);
		vi.advanceTimersByTime(DELAY * 4);
		expect(writeLeft).toHaveBeenCalledWith('A', 140);
		expect(writeCurrent).not.toHaveBeenCalled();
	});

	it('writes immediately on destroy, only for the session still on screen', () => {
		const { key, persist, writeCurrent } = harness();
		persist.schedule('A', 90);
		persist.destroy('A', 95);
		expect(writeCurrent).toHaveBeenCalledWith(95);
		key.current = 'B';
		persist.destroy('A', 99);
		vi.advanceTimersByTime(DELAY);
		expect(writeCurrent).toHaveBeenCalledTimes(1);
	});
});

describe('reading position across a quick A → B → A switch (#1282)', () => {
	beforeEach(() => {
		vi.useFakeTimers();
		sessionEphemeralCache.clear();
	});
	afterEach(() => {
		vi.useRealTimers();
		sessionEphemeralCache.clear();
	});

	function state(overrides: Partial<ActiveSessionState>): ActiveSessionState {
		return {
			sessionId: 'session-A', mode: 'code', title: 'A', messages: [],
			isStreaming: false, wsConnected: true, error: null, profile: 'p', project: 'proj',
			projectV2Id: null, pendingPermissions: [], deferredPermissions: [], pendingQuestions: [],
			queuedMessages: [], pendingPeerMessages: [], activeTasks: [], draft: '', scrollTop: 0,
			isRestoringSession: false, isLoadingSession: false,
			...overrides
		} as ActiveSessionState;
	}

	it('keeps A\'s fresh position for A and never writes it into B', async () => {
		let store = state({ sessionId: 'session-A', scrollTop: 40 });
		const internal: InternalMutableState = {
			pendingTools: new Set<string>(), awaitingNewSession: false, pendingRequestId: null,
			stopRequestedBeforeSession: false, worktreeCreationGeneration: 0, worktreeCreateAbort: null
		};
		const deps: SessionSwitcherDeps = {
			update: (fn) => { store = fn(store); },
			getState: () => store,
			wsSend: vi.fn(() => true),
			internal,
			sessionEphemeralCache,
			onPersistTimerChanged: vi.fn()
		};
		const switcher = createSessionSwitcher(deps);
		// B was visited before and has its own saved position.
		sessionEphemeralCache.save('session-B', { ...switcherEphemeral(store), scrollTop: 900 });

		// MessageArea's wiring.
		const persist = createScrollTopPersist({
			delayMs: DELAY,
			currentKey: () => store.sessionId ?? 'new',
			writeCurrent: (position) => { store = { ...store, scrollTop: position }; },
			writeLeft: (key, position) => sessionEphemeralCache.patchScrollTop(key, position)
		});
		let tracked = store.sessionId;
		let domScrollTop = 40;
		const switchEffect = () => {
			const prevId = tracked;
			if (store.sessionId === prevId) return;
			persist.cancel();
			if (prevId) persist.leave(prevId, domScrollTop);
			tracked = store.sessionId;
		};

		try {
			// Read A up to 400; the trailing write has not fired yet.
			domScrollTop = 400;
			persist.schedule('session-A', 400);
			vi.advanceTimersByTime(DELAY - 50);
			expect(store.scrollTop).toBe(40);

			// Switch to B inside the 250 ms window: the switcher snapshots A's
			// STALE store value (40), then the effect runs against B's store.
			await switcher.switchSession('session-B');
			expect(store.sessionId).toBe('session-B');
			switchEffect();
			vi.advanceTimersByTime(DELAY * 4);
			expect(store.scrollTop).toBe(900);
			expect(sessionEphemeralCache.restore('session-A')?.scrollTop).toBe(400);

			// Back to A: its own fresh position comes back, B keeps its own.
			domScrollTop = 900;
			await switcher.switchSession('session-A');
			switchEffect();
			expect(store.sessionId).toBe('session-A');
			expect(store.scrollTop).toBe(400);
			expect(sessionEphemeralCache.restore('session-B')?.scrollTop).toBe(900);
		} finally {
			switcher.destroy();
		}
	});
});

function switcherEphemeral(s: ActiveSessionState) {
	return {
		profile: s.profile, title: 'B', draft: '', scrollTop: 0, project: s.project,
		projectV2Id: null, workspaceProjectIds: []
	} as unknown as Parameters<typeof sessionEphemeralCache.save>[1];
}
