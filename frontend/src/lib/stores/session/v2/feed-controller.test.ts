/**
 * Feed-controller flush-scheduling tests (docs/V2_PHASE1_DESIGN.md §6).
 *
 * The controller coalesces reducer folds onto requestAnimationFrame. Browsers
 * pause rAF in hidden / fully-occluded tabs, which would strand the coalescing
 * latch forever and freeze the live view until refocus. `scheduleFlush()`
 * therefore races the rAF against a 250ms setTimeout fallback so a fold always
 * lands. These tests pin: (1) the fold still happens via the timer when rAF
 * never fires (hidden tab), and (2) the normal path still folds via rAF exactly
 * once (no double-run when both handles are live).
 */

import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';

// Capture the SSE feed's onEvent so we can drive events directly, without a real
// fetch/stream. `vi.hoisted` keeps the shared state accessible from the hoisted
// `vi.mock` factory.
const hoisted = vi.hoisted(() => ({
	onEvent: null as ((e: Record<string, unknown>) => void) | null,
	stops: 0,
}));

vi.mock('./sse-feed', () => ({
	createSseFeed: (_sessionId: string, opts: { onEvent: (e: Record<string, unknown>) => void }) => {
		hoisted.onEvent = opts.onEvent;
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

function makeDeps() {
	let state = {
		sessionId: 's1',
		messages: [],
		isStreaming: false,
		activeTasks: [],
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

beforeEach(() => {
	hoisted.onEvent = null;
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

describe('feedController flush scheduling', () => {
	it('folds via the 250ms timer when rAF never fires (hidden/occluded tab)', () => {
		const deps = makeDeps();
		feedController.attach(deps);
		feedController.start('s1');
		expect(hoisted.onEvent).toBeTypeOf('function');

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

	it('folds via rAF on the normal path and runs exactly once (no double-run)', () => {
		const deps = makeDeps();
		feedController.attach(deps);
		feedController.start('s1');

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
