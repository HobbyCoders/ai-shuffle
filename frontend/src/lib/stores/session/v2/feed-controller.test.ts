/**
 * Feed-controller tests (docs/V2_PHASE1_DESIGN.md §6).
 *
 * Two behaviours are pinned here:
 *
 * 1. History-batch hydration. The controller reduces backfilled events WITHOUT
 *    folding until a `caught_up` sentinel arrives, so the whole persisted
 *    transcript paints in ONE batch instead of repainting per backfilled frame
 *    (the "history paints one message at a time on open" bug). After catch-up,
 *    live events keep the incremental per-frame paint. The flag re-arms only on
 *    the FIRST connect (`onOpen` before any catch-up); reconnects after catch-up
 *    keep folding live events immediately (#611). A no-sentinel fallback means
 *    the view can never be stuck blank against an old backend.
 *
 * 2. Flush scheduling. The post-catch-up fold coalesces onto requestAnimationFrame
 *    with a 250ms setTimeout fallback (browsers pause rAF in hidden tabs). These
 *    tests pin the timer-fallback fold and the exactly-once rAF fold.
 *
 * 3. Optimistic seeding through the immediate fold (worktree double-load fix):
 *    start() seeds optimistic messages BEFORE the immediate fold so a freshly
 *    committed worktree session's optimistic user message + isStreaming survive.
 */

import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';

// Capture the SSE feed's onEvent/onOpen so we can drive events directly, without a
// real fetch/stream. `vi.hoisted` keeps the shared state accessible from the
// hoisted `vi.mock` factory.
const hoisted = vi.hoisted(() => ({
	onEvent: null as ((e: Record<string, unknown>) => void) | null,
	onOpen: null as (() => void) | null,
	stops: 0,
}));

vi.mock('./sse-feed', () => ({
	createSseFeed: (
		_sessionId: string,
		opts: { onEvent: (e: Record<string, unknown>) => void; onOpen?: () => void },
	) => {
		hoisted.onEvent = opts.onEvent;
		hoisted.onOpen = opts.onOpen ?? null;
		return {
			get running() {
				return true;
			},
			get lastSeq() {
				return 0;
			},
			stop() {
				hoisted.stops++;
			},
		};
	},
}));

import { feedController } from './feed-controller';
import type { ActiveSessionState } from '../../active-session';

// ─── rAF stub: capture callbacks, flush on demand (never auto-fires) ──────────

let rafQueue: Array<{ id: number; cb: FrameRequestCallback }> = [];
let rafId = 0;
function flushRaf(): void {
	const q = rafQueue;
	rafQueue = [];
	for (const r of q) r.cb(0);
}

function makeDeps(initial?: Partial<ActiveSessionState>) {
	let state = {
		sessionId: 's1',
		messages: [],
		isStreaming: false,
		activeTasks: [],
		queuedMessages: [],
		...initial,
	} as unknown as ActiveSessionState;
	const update = vi.fn((fn: (s: ActiveSessionState) => ActiveSessionState) => {
		state = fn(state);
	});
	const getState = vi.fn(() => state);
	return {
		update,
		getState,
		get state() {
			return state;
		},
	};
}

// A text_delta event; the reducer opens a streaming assistant text message and
// subsequent deltas append to it. Two deltas → one message with joined content,
// proving both intake calls were reduced before the fold.
function textDelta(id: string, text: string): Record<string, unknown> {
	return {
		type: 'assistant.text_delta',
		id,
		seq: 0,
		session_id: 's1',
		created_at: '2026-07-12T00:00:00.000Z',
		text,
	};
}

// A delivered user message carrying the turn_id that correlates to a queue chip
// (queue entry id === turn id). Firing this proves the queued turn ran.
function userMessage(id: string, turnId: string, content = 'queued text'): Record<string, unknown> {
	return {
		type: 'message.user',
		id,
		seq: 0,
		session_id: 's1',
		created_at: '2026-07-12T00:00:00.000Z',
		turn_id: turnId,
		content,
	};
}

// The history/live boundary sentinel emitted by the backend after backfill.
function caughtUp(seq = 0): Record<string, unknown> {
	return { type: 'caught_up', seq };
}

beforeEach(() => {
	hoisted.onEvent = null;
	hoisted.onOpen = null;
	hoisted.stops = 0;
	rafQueue = [];
	rafId = 0;
	// Install fake timers FIRST — vitest's fake timers also replace
	// requestAnimationFrame, so our capturing stub must be applied afterwards to
	// win. Our stub captures rAF callbacks and never auto-fires them, modelling a
	// hidden/occluded tab where the browser has paused the rAF clock.
	vi.useFakeTimers();
	vi.stubGlobal('requestAnimationFrame', (cb: FrameRequestCallback) => {
		const id = ++rafId;
		rafQueue.push({ id, cb });
		return id;
	});
	vi.stubGlobal('cancelAnimationFrame', (id: number) => {
		rafQueue = rafQueue.filter((r) => r.id !== id);
	});
});

afterEach(() => {
	feedController.stop();
	vi.useRealTimers();
	vi.unstubAllGlobals();
});

describe('feedController history-batch hydration', () => {
	it('(a) batches backfill: no intermediate folds, one fold on caught_up with full list', () => {
		const deps = makeDeps();
		feedController.attach(deps);
		feedController.start('s1');
		expect(hoisted.onEvent).toBeTypeOf('function');

		// The immediate fold on start() is the only update so far.
		const callsAfterStart = deps.update.mock.calls.length;
		expect(callsAfterStart).toBe(1);

		// Backfill events arrive before the sentinel. None must fold.
		hoisted.onEvent!(textDelta('e1', 'foo'));
		hoisted.onEvent!(textDelta('e2', 'bar'));
		// Even if rAF/timers fire, nothing was scheduled during backfill.
		flushRaf();
		vi.advanceTimersByTime(500); // < CATCHUP_FALLBACK_MS (1000)
		expect(deps.update.mock.calls.length).toBe(callsAfterStart);
		expect((deps.state.messages as unknown[]).length).toBe(0);

		// The sentinel folds exactly once, with the full backfilled list.
		hoisted.onEvent!(caughtUp(2));
		expect(deps.update.mock.calls.length).toBe(callsAfterStart + 1);
		const msgs = deps.state.messages as Array<{ content: string }>;
		expect(msgs).toHaveLength(1);
		expect(msgs[0].content).toBe('foobar');
	});

	it('(a-fallback) folds once when caught_up never arrives (old backend safety net)', () => {
		const deps = makeDeps();
		feedController.attach(deps);
		feedController.start('s1');
		const callsAfterStart = deps.update.mock.calls.length;

		hoisted.onEvent!(textDelta('e1', 'foo'));
		hoisted.onEvent!(textDelta('e2', 'bar'));
		// No sentinel. The 1s no-sentinel fallback must fold what we have.
		expect(deps.update.mock.calls.length).toBe(callsAfterStart);
		vi.advanceTimersByTime(1000);
		expect(deps.update.mock.calls.length).toBe(callsAfterStart + 1);
		const msgs = deps.state.messages as Array<{ content: string }>;
		expect(msgs).toHaveLength(1);
		expect(msgs[0].content).toBe('foobar');
	});

	it('(c) #611: after catch-up, a reconnect (onOpen) folds live events immediately — no re-batch', () => {
		const deps = makeDeps();
		feedController.attach(deps);
		feedController.start('s1');
		expect(hoisted.onOpen).toBeTypeOf('function');

		// Complete initial catch-up (the FIRST connection still batches history).
		hoisted.onEvent!(caughtUp(0));

		// A reconnect after catch-up: onOpen must NOT re-arm the batch flag
		// (`everCaughtUp` guard). Re-batching here would strand live events behind
		// the throttleable CATCHUP_FALLBACK_MS timer, which a hidden tab freezes →
		// transcript stall (#611). The reconnect's near-empty backfill (cursor =
		// lastSeq) IS the live tail, so it must fold on the normal live cadence.
		hoisted.onOpen!();

		// A live event arrives with NO new `caught_up` sentinel. It must fold via
		// the normal rAF/250ms cadence — NOT wait for a sentinel or the 1s catch-up
		// fallback (we never advance timers to 1000ms here).
		hoisted.onEvent!(textDelta('e1', 'foo'));
		flushRaf();

		const msgs = deps.state.messages as Array<{ content: string }>;
		expect(msgs).toHaveLength(1);
		expect(msgs[0].content).toBe('foo');
	});
});

describe('feedController flush scheduling (post-catch-up live cadence)', () => {
	it('(b) folds via the 250ms timer when rAF never fires (hidden/occluded tab)', () => {
		const deps = makeDeps();
		feedController.attach(deps);
		feedController.start('s1');
		// Enter live mode so events flush per the incremental cadence.
		hoisted.onEvent!(caughtUp(0));

		// Two events arrive while the tab is backgrounded. rAF is captured but
		// NEVER flushed (simulating a paused rAF clock).
		hoisted.onEvent!(textDelta('e1', 'foo'));
		hoisted.onEvent!(textDelta('e2', 'bar'));

		// rAF is pending but frozen — nothing has folded the second event yet.
		expect(rafQueue.length).toBeGreaterThan(0);

		// The fallback timer fires the fold.
		vi.advanceTimersByTime(250);

		const msgs = deps.state.messages as Array<{ content: string }>;
		expect(msgs).toHaveLength(1);
		expect(msgs[0].content).toBe('foobar'); // both events folded via the timer
	});

	it('(b) folds via rAF on the normal path and runs exactly once (no double-run)', () => {
		const deps = makeDeps();
		feedController.attach(deps);
		feedController.start('s1');
		hoisted.onEvent!(caughtUp(0));

		hoisted.onEvent!(textDelta('e1', 'foo'));
		hoisted.onEvent!(textDelta('e2', 'bar'));

		// rAF fires first (visible tab) → fold happens and the fallback timer is
		// cancelled.
		flushRaf();

		const msgs = deps.state.messages as Array<{ content: string }>;
		expect(msgs).toHaveLength(1);
		expect(msgs[0].content).toBe('foobar');

		// No double-run: the cancelled timer must not fold again, and no new rAF
		// is pending.
		const callsAfterRaf = deps.update.mock.calls.length;
		vi.advanceTimersByTime(500);
		flushRaf();
		expect(deps.update.mock.calls.length).toBe(callsAfterRaf);
	});
});

describe('feedController queue reconciliation (#588)', () => {
	it('drops a stale queue chip once its turn is delivered on the transcript', () => {
		const deps = makeDeps();
		feedController.attach(deps);
		feedController.start('s1');
		// Enter live mode so per-event folds run (post-catch-up incremental cadence).
		hoisted.onEvent!(caughtUp(0));

		// A queued chip is present (as the WS queue_updated broadcast would seed it),
		// keyed by the turn id.
		deps.update((s) => ({
			...s,
			queuedMessages: [
				{ id: 'turn-1', content: 'queued text', queuedAt: 1 },
				{ id: 'turn-2', content: 'still waiting', queuedAt: 2 },
			],
		}));

		// The server drains and runs turn-1: its user message lands on the SSE feed,
		// carrying turn_id === the chip id. No clearing queue_updated is ever sent.
		hoisted.onEvent!(userMessage('evt-u1', 'turn-1'));
		flushRaf();

		const queued = deps.state.queuedMessages as Array<{ id: string }>;
		expect(queued.map((q) => q.id)).toEqual(['turn-2']); // turn-1 chip self-healed
	});

	it('leaves chips intact when no matching turn has been delivered', () => {
		const deps = makeDeps();
		feedController.attach(deps);
		feedController.start('s1');
		// Enter live mode so per-event folds run (post-catch-up incremental cadence).
		hoisted.onEvent!(caughtUp(0));

		deps.update((s) => ({
			...s,
			queuedMessages: [{ id: 'turn-9', content: 'pending', queuedAt: 1 }],
		}));

		// A DIFFERENT (non-queued) turn's user message must not clear the chip.
		hoisted.onEvent!(userMessage('evt-u2', 'turn-other'));
		flushRaf();

		const queued = deps.state.queuedMessages as Array<{ id: string }>;
		expect(queued.map((q) => q.id)).toEqual(['turn-9']);
	});
});

describe('feedController optimistic seeding (worktree double-load fix)', () => {
	it('(d) preserves the optimistic user message + isStreaming through the immediate fold', () => {
		// A freshly committed worktree session: an optimistic user message and
		// isStreaming=true are already on activeSession when start() runs.
		const deps = makeDeps({
			messages: [
				{
					id: 'opt-1',
					role: 'user',
					content: 'do the thing',
					commandId: 'req-abc',
					timestamp: '2026-07-15T00:00:00.000Z',
				},
			],
			isStreaming: true,
		} as unknown as Partial<ActiveSessionState>);
		feedController.attach(deps);
		feedController.start('s1');

		// The immediate fold must NOT have clobbered messages to [] / isStreaming
		// to false — seedOptimistic ran first, carrying both through the fold.
		const msgs = deps.state.messages as Array<{ id: string; role: string }>;
		expect(msgs.some((m) => m.id === 'opt-1')).toBe(true);
		expect(deps.state.isStreaming).toBe(true);
	});
});
