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
import { cleanup, render } from '@testing-library/svelte';
import { tick } from 'svelte';
import { get, writable } from 'svelte/store';
import { createScrollTopPersist } from './scroll-top-persist';
import ScrollSwitchHarness from '$lib/test-stubs/ScrollSwitchHarness.svelte';
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

	it('files the captured leaving position under the leaving key and never into the current store', () => {
		const { key, persist, writeCurrent, writeLeft } = harness();
		persist.schedule('A', 100);
		key.current = 'B';
		// `known` (e.g. a clamped read after the switch) never beats the capture.
		expect(persist.leave('A', 0)).toBe(100);
		vi.advanceTimersByTime(DELAY * 4);
		expect(writeLeft).toHaveBeenCalledWith('A', 100);
		expect(writeCurrent).not.toHaveBeenCalled();
	});

	it('falls back to the known position, and writes nothing without one', () => {
		const { persist, writeLeft } = harness();
		expect(persist.leave('A', 70)).toBe(70);
		expect(writeLeft).toHaveBeenLastCalledWith('A', 70);
		writeLeft.mockClear();
		expect(persist.leave('A')).toBeUndefined();
		// A capture for another session is not used for this one.
		persist.schedule('C', 300);
		expect(persist.leave('A')).toBeUndefined();
		expect(writeLeft).not.toHaveBeenCalled();
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
		const scrollPositionCache = new Map<string, number>();
		// MessageArea's switch effect: no DOM read, the captured position wins.
		const switchEffect = () => {
			const prevId = tracked;
			if (store.sessionId === prevId) return;
			if (prevId) {
				const leftAt = persist.leave(prevId, scrollPositionCache.get(prevId));
				if (leftAt !== undefined) scrollPositionCache.set(prevId, leftAt);
			} else persist.cancel();
			tracked = store.sessionId;
		};

		try {
			// Read A up to 400; the trailing write has not fired yet.
			scrollPositionCache.set('session-A', 400);
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

describe('switching to a short session that clamps the scroller (#1282)', () => {
	beforeEach(() => {
		vi.useFakeTimers();
		sessionEphemeralCache.clear();
	});
	afterEach(() => {
		cleanup();
		vi.useRealTimers();
		sessionEphemeralCache.clear();
	});

	/**
	 * jsdom has no layout, so give the scroller a browser's geometry: 100 px per
	 * mounted row, a 100 px viewport, and a scrollTop that clamps to what the
	 * CURRENT content allows — so replacing a long transcript with a short one
	 * drops it to 0, exactly what Chromium does before the switch effect runs.
	 */
	function layout(el: HTMLElement) {
		let raw = 0;
		const max = () => Math.max(0, el.querySelectorAll('.row').length * 100 - 100);
		Object.defineProperty(el, 'clientHeight', { configurable: true, get: () => 100 });
		Object.defineProperty(el, 'scrollHeight', { configurable: true, get: () => el.querySelectorAll('.row').length * 100 });
		Object.defineProperty(el, 'scrollTop', {
			configurable: true,
			get: () => Math.min(raw, max()),
			set: (v: number) => { raw = Math.max(0, Math.min(v, max())); }
		});
	}

	it('saves the captured position, not the clamped scrollTop read after the switch', async () => {
		const longRows = Array.from({ length: 60 }, (_, i) => `a-${i}`);
		const session = writable({ sessionId: 'A', rows: longRows, scrollTop: 40 });
		const scrollPositionCache = new Map<string, number>();
		let scroller: HTMLDivElement | null = null;
		const { component } = render(ScrollSwitchHarness, {
			props: { session, scrollPositionCache, onContainer: (el: HTMLDivElement) => { scroller = el; layout(el); } }
		});
		await tick();
		expect(scroller).not.toBeNull();
		sessionEphemeralCache.save('A', { ...switcherEphemeral({ profile: 'p', project: 'x' } as ActiveSessionState), scrollTop: 40 });

		// The reader scrolls A to 400; the deferred write is still pending.
		scroller!.scrollTop = 400;
		(component as unknown as { scrollFrame(): void }).scrollFrame();
		vi.advanceTimersByTime(100);
		expect(get(session).scrollTop).toBe(40);

		// Switch to an uncached B whose transcript has not loaded: the DOM
		// replacement clamps the scroller to 0 before the switch effect runs.
		session.set({ sessionId: 'B', rows: [], scrollTop: 0 });
		await tick();
		expect(scroller!.scrollTop).toBe(0);
		vi.advanceTimersByTime(1000);

		expect(sessionEphemeralCache.restore('A')?.scrollTop).toBe(400);
		expect(scrollPositionCache.get('A')).toBe(400);
		expect(get(session).scrollTop).toBe(0);
	});
});

function switcherEphemeral(s: ActiveSessionState) {
	return {
		profile: s.profile, title: 'B', draft: '', scrollTop: 0, project: s.project,
		projectV2Id: null, workspaceProjectIds: []
	} as unknown as Parameters<typeof sessionEphemeralCache.save>[1];
}
