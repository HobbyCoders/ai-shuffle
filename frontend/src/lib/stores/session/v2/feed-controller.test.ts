/**
 * Feed-controller tests.
 *
 * Two behaviours are pinned here:
 *
 * 1. History-batch hydration. The controller reduces backfilled events WITHOUT
 *    folding until a `caught_up` sentinel arrives, so the whole persisted
 *    transcript paints in ONE batch instead of repainting per backfilled frame
 *    (the "history paints one message at a time on open" bug). After catch-up,
 *    live events keep the incremental per-frame paint. The flag re-arms only on
 *    the FIRST connect (`onOpen` before any catch-up); reconnects after catch-up
 *    keep folding live events immediately (#611). Initial hydration treats the
 *    backend's caught_up sentinel as the publish boundary, floored by an
 *    IDLE-based fallback (CATCHUP_FALLBACK_MS): a stream that goes quiet without
 *    ever sending the sentinel publishes what it has and completes hydration,
 *    because atomic-until-sentinel with no floor leaves a stalled feed blank
 *    forever with sends blocked. Frames re-arm the timer, so a flowing backfill
 *    of any length still folds exactly once.
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
	onError: null as ((e: unknown) => void) | null,
	onUnavailable: null as ((status: number) => void) | null,
	creates: 0,
	stops: 0,
	revives: 0,
	/** from_seq of the most recent createSseFeed (S2 resume cursor). */
	fromSeq: undefined as number | undefined,
	/** (sessionId, fromSeq) of every feed opened, oldest first. */
	opened: [] as Array<{ sessionId: string; fromSeq: number | undefined }>,
	/** `epoch` option of every feed opened, oldest first. */
	epochs: [] as Array<string | null | undefined>,
	/** What the fake feed reports as its log epoch (a real one learns it from
	 *  `caught_up`); null falls back to the seed the controller passed. */
	feedEpoch: null as string | null,
}));

vi.mock('./sse-feed', () => ({
	createSseFeed: (
		_sessionId: string,
		opts: {
			onEvent: (e: Record<string, unknown>) => void;
			onOpen?: () => void;
			onError?: (e: unknown) => void;
			onUnavailable?: (status: number) => void;
			fromSeq?: number;
			epoch?: string | null;
		},
	) => {
		hoisted.creates++;
		hoisted.epochs.push(opts.epoch);
		hoisted.onEvent = opts.onEvent;
		hoisted.onOpen = opts.onOpen ?? null;
		hoisted.onError = opts.onError ?? null;
		hoisted.onUnavailable = opts.onUnavailable ?? null;
		hoisted.fromSeq = opts.fromSeq;
		hoisted.opened.push({ sessionId: _sessionId, fromSeq: opts.fromSeq });
		return {
			get running() {
				return true;
			},
			get lastSeq() {
				return 0;
			},
			get epoch() {
				return hoisted.feedEpoch ?? opts.epoch ?? null;
			},
			revive() {
				hoisted.revives++;
			},
			stop() {
				hoisted.stops++;
			},
		};
	},
}));

import {
	feedController,
	CATCHUP_FALLBACK_MS,
	FIRST_FRAME_BUDGET_MS,
	HYDRATION_CEILING_MS,
	STREAM_FOLD_MS,
	TRANSCRIPT_CACHE_MAX,
	TRANSCRIPT_CACHE_TTL_MS,
	cachedTranscriptCount,
} from './feed-controller';
import { pendingQueue } from '../pending-queue';
import { swarms } from '../../swarms';
import { get } from 'svelte/store';
import { browserPanel } from '../../browser-panel.svelte';
import { sidePanel, sessionPanelKey } from '../../side-panel.svelte';
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
		pendingPeerMessages: [],
		pendingPermissions: [],
		deferredPermissions: [],
		pendingQuestions: [],
		...initial,
	} as unknown as ActiveSessionState;
	const update = vi.fn((fn: (s: ActiveSessionState) => ActiveSessionState) => {
		state = fn(state);
	});
	const getState = vi.fn(() => state);
	// Mirror the real activeSession.resolveInteractionByRequestId filtering so the
	// controller's cross-device settle wiring can be asserted against `deps.state`.
	const resolveInteractionByRequestId = vi.fn((requestId: string) => {
		if (!requestId) return;
		state = {
			...state,
			pendingPermissions: (state.pendingPermissions ?? []).filter((p) => p.request_id !== requestId),
			deferredPermissions: (state.deferredPermissions ?? []).filter((p) => p.request_id !== requestId),
			pendingQuestions: (state.pendingQuestions ?? []).filter((q) => q.request_id !== requestId),
		};
	});
	const requestPendingQuestions = vi.fn();
	const requestPendingPermissions = vi.fn();
	return {
		update,
		getState,
		requestPendingQuestions,
		requestPendingPermissions,
		resolveInteractionByRequestId,
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

// A tool.use event. `name` is the fully-qualified tool name; the reducer + the
// browser-panel auto-open hook both read `event.name`. tool_use_id is required
// (the reducer drops a tool.use without one).
function toolUse(id: string, name: string): Record<string, unknown> {
	return {
		type: 'tool.use',
		id: `evt-${id}`,
		seq: 0,
		session_id: 's1',
		created_at: '2026-07-12T00:00:00.000Z',
		tool_use_id: id,
		name,
		input: {},
	};
}

// A task.updated frame. The provider-lane subagent feed (WorkerFeed) emits one
// with state 'running' at dispatch and one with a terminal state at the end,
// both carrying task_id === tool_use_id === the worker session id.
function taskUpdated(
	taskId: string,
	state: string,
	fields: Record<string, unknown> = {},
): Record<string, unknown> {
	return {
		type: 'task.updated',
		id: `evt-task-${taskId}-${state}`,
		seq: 0,
		session_id: 's1',
		created_at: '2026-07-12T00:00:00.000Z',
		task_id: taskId,
		state,
		tool_use_id: taskId,
		...fields,
	};
}

// Durable interaction lifecycle events (#606 item 4). The FE consumes these only
// to SETTLE a card the legacy relay seeded — top-level `request_id` correlates to
// the pending permission / question's request_id.
function interaction(
	type: 'interaction.answered' | 'interaction.expired',
	requestId: string | null | undefined,
): Record<string, unknown> {
	return {
		type,
		id: `evt-${type}-${requestId ?? 'none'}`,
		seq: 0,
		session_id: 's1',
		created_at: '2026-07-12T00:00:00.000Z',
		interaction_id: 'int-1',
		request_id: requestId,
	};
}

beforeEach(() => {
	hoisted.onEvent = null;
	hoisted.onOpen = null;
	hoisted.onError = null;
	hoisted.onUnavailable = null;
	hoisted.creates = 0;
	hoisted.stops = 0;
	hoisted.revives = 0;
	hoisted.fromSeq = undefined;
	hoisted.opened = [];
	hoisted.epochs = [];
	hoisted.feedEpoch = null;
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
	// The transcript cache is module state: a session cached by one test must not
	// change the hydration path of the next.
	feedController.invalidate();
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
		vi.advanceTimersByTime(500);
		expect(deps.update.mock.calls.length).toBe(callsAfterStart);
		expect((deps.state.messages as unknown[]).length).toBe(0);

		// The sentinel folds exactly once, with the full backfilled list.
		hoisted.onEvent!(caughtUp(2));
		expect(deps.update.mock.calls.length).toBe(callsAfterStart + 1);
		const msgs = deps.state.messages as Array<{ content: string }>;
		expect(msgs).toHaveLength(1);
		expect(msgs[0].content).toBe('foobar');
	});

	it('(a-atomic) a flowing backfill never completes hydration early — caught_up is the boundary', () => {
		const deps = makeDeps({ isLoadingSession: true, isRestoringSession: true });
		feedController.attach(deps);
		feedController.start('s1');
		const callsAfterStart = deps.update.mock.calls.length;

		// Gaps SHORTER than the idle window are an alive stream, not a stall: each
		// frame re-arms the fallback, so hydration stays open no matter how many
		// pages it takes. caught_up remains the hydration boundary, and nothing
		// is painted before it however long the backfill runs.
		for (let i = 0; i < 20; i++) {
			hoisted.onEvent!(textDelta(`e${i}`, 'x'));
			vi.advanceTimersByTime(CATCHUP_FALLBACK_MS - 100);
			expect(deps.state.isLoadingSession).toBe(true);
			expect(deps.state.isRestoringSession).toBe(true);
		}
		expect(deps.update.mock.calls.length).toBe(callsAfterStart);
		expect(deps.state.messages).toHaveLength(0);

		hoisted.onEvent!(caughtUp(20));
		expect(deps.update.mock.calls.length).toBe(callsAfterStart + 1);
		expect((deps.state.messages as Array<{ content: string }>)[0].content).toBe('x'.repeat(20));
		expect(deps.state.isLoadingSession).toBe(false);
		expect(deps.state.isRestoringSession).toBe(false);
	});

	it('(a-quick) a short backfill publishes only on caught_up', () => {
		const deps = makeDeps({ isLoadingSession: true, isRestoringSession: true });
		feedController.attach(deps);
		feedController.start('s1');
		const callsAfterStart = deps.update.mock.calls.length;

		const steps = 10;
		for (let i = 0; i < steps; i++) {
			hoisted.onEvent!(textDelta(`e${i}`, 'x'));
			vi.advanceTimersByTime(Math.floor(1_499 / steps));
		}
		expect(deps.update.mock.calls.length).toBe(callsAfterStart);
		expect(deps.state.messages).toHaveLength(0);

		hoisted.onEvent!(caughtUp(10));
		expect(deps.update.mock.calls.length).toBe(callsAfterStart + 1);
		expect((deps.state.messages as Array<{ content: string }>)[0].content).toBe('x'.repeat(10));
	});

	it('(a-fallback) an idle stall mid-backfill publishes what arrived and completes hydration', () => {
		const deps = makeDeps({ isLoadingSession: true, isRestoringSession: true });
		feedController.attach(deps);
		feedController.start('s1');
		const callsAfterStart = deps.update.mock.calls.length;

		hoisted.onEvent!(textDelta('e1', 'foo'));
		hoisted.onEvent!(textDelta('e2', 'bar'));
		// Still inside the idle window — nothing published yet.
		vi.advanceTimersByTime(CATCHUP_FALLBACK_MS - 1);
		expect(deps.update.mock.calls.length).toBe(callsAfterStart);

		// The stream goes quiet and the sentinel never arrives. Atomic hydration
		// has to have a floor: without one the transcript stays blank forever and
		// isRestoringSession keeps blocking sends.
		vi.advanceTimersByTime(1);
		expect(deps.update.mock.calls.length).toBe(callsAfterStart + 1);
		expect((deps.state.messages as Array<{ content: string }>)[0].content).toBe('foobar');
		expect(deps.state.isLoadingSession).toBe(false);
		expect(deps.state.isRestoringSession).toBe(false);
	});

	it('(S1) a connect that has delivered NO frame never completes hydration — it goes slow', () => {
		const deps = makeDeps({ isLoadingSession: true, isRestoringSession: true });
		feedController.attach(deps);
		feedController.start('s1');

		// The old 1s floor fired here and published an EMPTY reducer, clearing
		// isLoadingSession — which the chat surface reads as `cleared` and renders
		// the "Conversation cleared" hero for a session that HAS messages. With
		// zero reduced events there is no transcript to publish, so hydration must
		// stay open.
		vi.advanceTimersByTime(CATCHUP_FALLBACK_MS * 3);
		expect(deps.state.isLoadingSession).toBe(true);
		expect(deps.state.isRestoringSession).toBe(true);
		expect(deps.state.messages).toHaveLength(0);
		expect(deps.state.isHydrationSlow).toBeFalsy();

		// Past the pre-first-frame budget the wait is abnormal — say so, without
		// pretending the load finished.
		vi.advanceTimersByTime(FIRST_FRAME_BUDGET_MS);
		expect(deps.state.isHydrationSlow).toBe(true);
		expect(deps.state.isLoadingSession).toBe(true);
		expect(deps.state.isRestoringSession).toBe(true);
	});

	it('(S1) a feed that produces nothing at all is released at the hard ceiling', () => {
		const deps = makeDeps({ isLoadingSession: true, isRestoringSession: true });
		feedController.attach(deps);
		feedController.start('s1');

		// Dead, not slow: sends must not stay blocked behind isRestoringSession
		// forever, so the ceiling still releases the flags.
		vi.advanceTimersByTime(HYDRATION_CEILING_MS + FIRST_FRAME_BUDGET_MS);
		expect(deps.state.isLoadingSession).toBe(false);
		expect(deps.state.isRestoringSession).toBe(false);
	});

	it('(S1) a frame arriving after a premature flip re-enters batch mode', () => {
		const deps = makeDeps({ isLoadingSession: true, isRestoringSession: true });
		feedController.attach(deps);
		feedController.start('s1');

		// Force a premature completion: two frames, then a stall past the idle
		// budget publishes what arrived (the transcript must never stay blank).
		hoisted.onEvent!(textDelta('e1', 'foo'));
		vi.advanceTimersByTime(CATCHUP_FALLBACK_MS);
		expect(deps.state.isLoadingSession).toBe(false);
		const callsAfterFlip = deps.update.mock.calls.length;

		// The backfill resumes. Every one of these folding individually is the
		// "transcript visibly rebuilds message by message" bug — they must batch
		// again and publish ONCE on the real sentinel.
		for (let i = 0; i < 10; i++) {
			hoisted.onEvent!(textDelta(`b${i}`, 'x'));
			vi.advanceTimersByTime(CATCHUP_FALLBACK_MS - 100);
		}
		flushRaf();
		expect(deps.update.mock.calls.length).toBe(callsAfterFlip);

		hoisted.onEvent!(caughtUp(11));
		expect(deps.update.mock.calls.length).toBe(callsAfterFlip + 1);
		expect((deps.state.messages as Array<{ content: string }>)[0].content).toBe(
			'foo' + 'x'.repeat(10)
		);
	});

	it('(a-fallback) live cadence resumes once the owed sentinel finally lands', () => {
		const deps = makeDeps();
		feedController.attach(deps);
		feedController.start('s1');
		// A frame then a stall → premature publish with the sentinel still owed.
		hoisted.onEvent!(textDelta('e0', 'early'));
		vi.advanceTimersByTime(CATCHUP_FALLBACK_MS);
		hoisted.onEvent!(caughtUp(1));
		const callsAfterCatchUp = deps.update.mock.calls.length;

		// Genuinely live now: a delta folds on the streaming cadence.
		hoisted.onEvent!(textDelta('e1', 'late'));
		vi.advanceTimersByTime(STREAM_FOLD_MS);
		expect(deps.update.mock.calls.length).toBe(callsAfterCatchUp + 1);
		expect((deps.state.messages as Array<{ content: string }>)[0].content).toBe('earlylate');
	});

	it('(a-fallback) a stopped feed does not fold through a pending fallback timer', () => {
		const deps = makeDeps({ isLoadingSession: true, isRestoringSession: true });
		feedController.attach(deps);
		feedController.start('s1');
		hoisted.onEvent!(textDelta('e1', 'foo'));
		const callsBeforeStop = deps.update.mock.calls.length;

		feedController.stop();
		vi.advanceTimersByTime(CATCHUP_FALLBACK_MS * 3);

		expect(deps.update.mock.calls.length).toBe(callsBeforeStop);
		expect(deps.state.messages).toHaveLength(0);
	});

	it('duplicate same-session start is idempotent', () => {
		const deps = makeDeps();
		feedController.attach(deps);
		feedController.start('s1');
		const owner = hoisted.onEvent;

		feedController.start('s1');

		expect(hoisted.creates).toBe(1);
		expect(hoisted.onEvent).toBe(owner);
	});

	it('stale callbacks cannot complete a replacement hydration', () => {
		const deps = makeDeps({ isLoadingSession: true });
		feedController.attach(deps);
		feedController.start('s1');
		const staleEvent = hoisted.onEvent!;

		feedController.restart('s1');
		const currentEvent = hoisted.onEvent!;
		const callsAfterRestart = deps.update.mock.calls.length;

		staleEvent(textDelta('stale', 'wrong'));
		staleEvent(caughtUp(1));
		expect(deps.update.mock.calls.length).toBe(callsAfterRestart);
		expect(deps.state.isLoadingSession).toBe(true);

		currentEvent(textDelta('current', 'right'));
		currentEvent(caughtUp(2));
		expect((deps.state.messages as Array<{ content: string }>)[0].content).toBe('right');
		expect(deps.state.isLoadingSession).toBe(false);
	});

	it('(c) #611: after catch-up, a reconnect (onOpen) folds live events immediately — no re-batch', () => {
		const deps = makeDeps();
		feedController.attach(deps);
		feedController.start('s1');
		expect(hoisted.onOpen).toBeTypeOf('function');

		// Complete initial catch-up (the FIRST connection still batches history).
		hoisted.onEvent!(caughtUp(0));

		// A reconnect after catch-up: onOpen must NOT re-arm the batch flag
		// (`everCaughtUp` guard). The reconnect's near-empty backfill (cursor =
		// lastSeq) IS the live tail, so it must fold on the normal live cadence.
		hoisted.onOpen!();

		// A live event arrives with NO new `caught_up` sentinel. It must fold via
		// the normal live cadence — it must not wait for another sentinel.
		hoisted.onEvent!(textDelta('e1', 'foo'));
		vi.advanceTimersByTime(STREAM_FOLD_MS);

		const msgs = deps.state.messages as Array<{ content: string }>;
		expect(msgs).toHaveLength(1);
		expect(msgs[0].content).toBe('foo');
	});
});

describe('feedController flush scheduling (post-catch-up live cadence)', () => {
	it('(R1) streaming deltas coalesce to one fold per STREAM_FOLD_MS, not per frame', () => {
		const deps = makeDeps();
		feedController.attach(deps);
		feedController.start('s1');
		hoisted.onEvent!(caughtUp(0));
		const callsAfterCatchUp = deps.update.mock.calls.length;

		// Nobody can read 60fps text, and a per-frame fold re-renders the whole
		// subscriber graph. A burst of deltas inside one window folds ONCE — and
		// a frozen rAF clock (hidden tab) is irrelevant, the throttle is a timer.
		for (let i = 0; i < 20; i++) hoisted.onEvent!(textDelta(`e${i}`, 'x'));
		flushRaf();
		expect(deps.update.mock.calls.length).toBe(callsAfterCatchUp);

		vi.advanceTimersByTime(STREAM_FOLD_MS);
		expect(deps.update.mock.calls.length).toBe(callsAfterCatchUp + 1);
		expect((deps.state.messages as Array<{ content: string }>)[0].content).toBe('x'.repeat(20));

		// Exactly once per window: no stray rAF or 250ms timer folds again.
		const calls = deps.update.mock.calls.length;
		vi.advanceTimersByTime(500);
		flushRaf();
		expect(deps.update.mock.calls.length).toBe(calls);
	});

	it('(R1) a non-streaming event keeps the immediate rAF cadence', () => {
		const deps = makeDeps();
		feedController.attach(deps);
		feedController.start('s1');
		hoisted.onEvent!(caughtUp(0));
		const callsAfterCatchUp = deps.update.mock.calls.length;

		// A permission prompt / turn boundary must not wait out a stream throttle.
		hoisted.onEvent!(userMessage('evt-u9', 'turn-9', 'hello'));
		flushRaf();
		expect(deps.update.mock.calls.length).toBe(callsAfterCatchUp + 1);
	});

	it('(R1) an immediate event upgrades a pending throttled fold', () => {
		const deps = makeDeps();
		feedController.attach(deps);
		feedController.start('s1');
		hoisted.onEvent!(caughtUp(0));
		const callsAfterCatchUp = deps.update.mock.calls.length;

		hoisted.onEvent!(textDelta('e1', 'foo'));      // schedules the throttle
		hoisted.onEvent!(userMessage('evt-u1', 't1')); // upgrades to next-frame
		flushRaf();
		expect(deps.update.mock.calls.length).toBe(callsAfterCatchUp + 1);

		// And the superseded throttle timer must not fold a second time.
		const calls = deps.update.mock.calls.length;
		vi.advanceTimersByTime(500);
		expect(deps.update.mock.calls.length).toBe(calls);
	});

	it('(b) folds via the 250ms timer when rAF never fires (hidden/occluded tab)', () => {
		const deps = makeDeps();
		feedController.attach(deps);
		feedController.start('s1');
		hoisted.onEvent!(caughtUp(0));

		// An immediate-cadence event while the tab is backgrounded: rAF is
		// captured but NEVER flushed (a paused rAF clock).
		hoisted.onEvent!(userMessage('evt-u1', 't1', 'hi'));
		expect(rafQueue.length).toBeGreaterThan(0);

		vi.advanceTimersByTime(250);
		expect((deps.state.messages as Array<{ content: string }>)).toHaveLength(1);
	});
});

describe('feedController hidden streaming publication (#1282)', () => {
	function visibility(hidden: boolean): void {
		vi.spyOn(document, 'hidden', 'get').mockReturnValue(hidden);
		document.dispatchEvent(new Event('visibilitychange'));
	}

	afterEach(() => vi.restoreAllMocks());

	it('reduces hidden deltas without publishing, then publishes the entire stream once visible', () => {
		const deps = makeDeps();
		feedController.attach(deps);
		feedController.start('s1');
		hoisted.onEvent!(caughtUp(0));
		const calls = deps.update.mock.calls.length;
		visibility(true);

		// Ten seconds of 20 Hz SSE input. Count actual subscriber publications,
		// not merely scheduled timers; intake still has to retain every delta.
		for (let i = 0; i < 200; i++) {
			hoisted.onEvent!(textDelta(`hidden-${i}`, 'x'));
			vi.advanceTimersByTime(STREAM_FOLD_MS);
		}
		expect(deps.update.mock.calls.length - calls).toBe(0);
		expect(deps.state.messages).toHaveLength(0);
		visibility(false);
		vi.advanceTimersByTime(STREAM_FOLD_MS);
		expect(deps.update.mock.calls.length - calls).toBe(1);
		expect((deps.state.messages as Array<{ content: string }>)[0].content).toBe('x'.repeat(200));
		vi.advanceTimersByTime(1000);
		expect(deps.update.mock.calls.length - calls).toBe(1);
	});

	it.each([true, false])('defers an already queued throttle even if visibilitychange dispatch is %s', (dispatch) => {
		const deps = makeDeps();
		feedController.attach(deps);
		feedController.start('s1');
		hoisted.onEvent!(caughtUp(0));
		const calls = deps.update.mock.calls.length;
		hoisted.onEvent!(textDelta('queued', 'retained'));
		vi.spyOn(document, 'hidden', 'get').mockReturnValue(true);
		if (dispatch) document.dispatchEvent(new Event('visibilitychange'));
		vi.advanceTimersByTime(1000);
		expect(deps.update.mock.calls.length).toBe(calls);
		visibility(false);
		vi.advanceTimersByTime(STREAM_FOLD_MS);
		expect(deps.update.mock.calls.length).toBe(calls + 1);
		expect((deps.state.messages as Array<{ content: string }>)[0].content).toBe('retained');
	});

	it('keeps lifecycle and interaction settlement prompt while hidden, without a duplicate resume fold', () => {
		const deps = makeDeps({ pendingPermissions: [{ request_id: 'perm-1' }] as ActiveSessionState['pendingPermissions'] });
		feedController.attach(deps);
		feedController.start('s1');
		hoisted.onEvent!(caughtUp(0));
		visibility(true);
		hoisted.onEvent!({ ...textDelta('start', ''), type: 'turn.started', turn_id: 't1' });
		vi.advanceTimersByTime(250);
		expect(deps.state.isStreaming).toBe(true);
		hoisted.onEvent!(textDelta('hidden', 'complete text'));
		hoisted.onEvent!({ ...textDelta('answered', ''), type: 'interaction.answered', request_id: 'perm-1' });
		expect(deps.resolveInteractionByRequestId).toHaveBeenCalledWith('perm-1');
		expect(deps.state.pendingPermissions).toEqual([]);
		hoisted.onEvent!({ ...textDelta('end', ''), type: 'turn.completed', turn_id: 't1' });
		vi.advanceTimersByTime(250);
		expect(deps.state.isStreaming).toBe(false);
		expect((deps.state.messages as Array<{ content: string }>)[0].content).toBe('complete text');
		const calls = deps.update.mock.calls.length;
		visibility(false);
		vi.advanceTimersByTime(1000);
		expect(deps.update.mock.calls.length).toBe(calls);
	});

	it('lets reconnect catch-up publish deferred data and clears the visibility dirty flag', () => {
		const deps = makeDeps();
		feedController.attach(deps);
		feedController.start('s1');
		hoisted.onEvent!(caughtUp(0));
		visibility(true);
		hoisted.onEvent!(textDelta('before-reconnect', 'kept'));
		hoisted.onOpen!();
		hoisted.onEvent!(caughtUp(1));
		expect((deps.state.messages as Array<{ content: string }>)[0].content).toBe('kept');
		const calls = deps.update.mock.calls.length;
		visibility(false);
		vi.advanceTimersByTime(1000);
		expect(deps.update.mock.calls.length).toBe(calls);
	});

	it('removes visibility work on stop and retains unpublished data in the session cache', () => {
		const deps = makeDeps();
		feedController.attach(deps);
		feedController.start('s1');
		hoisted.onEvent!(caughtUp(0));
		visibility(true);
		hoisted.onEvent!({ ...textDelta('hidden', 'cached'), seq: 1 });
		feedController.stop();
		const calls = deps.update.mock.calls.length;
		visibility(false);
		vi.advanceTimersByTime(1000);
		expect(deps.update.mock.calls.length).toBe(calls);
		feedController.start('s1');
		expect((deps.state.messages as Array<{ content: string }>)[0].content).toBe('cached');
		expect(hoisted.fromSeq).toBe(1);
	});
});

describe('feedController queue reconciliation (#588)', () => {
	it('correlates a live-turn peer receipt by queued id and preserves non-checkpoint provenance', () => {
		const deps = makeDeps();
		feedController.attach(deps);
		feedController.start('s1');
		hoisted.onEvent!(caughtUp(0));
		deps.update(s => ({ ...s, queuedMessages: [{ id: 'human', content: 'same text', queuedAt: 1 }],
			pendingPeerMessages: [{ id: 'peer-live', content: 'same text', senderLabel: 'Reviewer', queuedAt: 1 }] }));
		const delivered = userMessage('live-peer-event', 'original-running-turn', 'attributed peer envelope');
		Object.assign(delivered, { queued_turn_id: 'peer-live', checkpoint_backed: false });
		hoisted.onEvent!(delivered);
		flushRaf();
		expect(deps.state.pendingPeerMessages).toEqual([]);
		expect(deps.state.queuedMessages?.map(m => m.id)).toEqual(['human']);
		expect(deps.state.messages?.find(m => m.id === 'live-peer-event')).toMatchObject({
			turnId: 'original-running-turn', queuedTurnId: 'peer-live', checkpointBacked: false
		});
	});


	it('removes exactly the delivered peer id once without matching human queue text', () => {
		const deps = makeDeps();
		feedController.attach(deps);
		feedController.start('s1');
		hoisted.onEvent!(caughtUp(0));
		deps.update(s => ({ ...s, queuedMessages: [{ id: 'human', content: 'same text', queuedAt: 1 }],
			pendingPeerMessages: [
				{ id: 'peer-one', content: 'same text', senderLabel: 'Reviewer', queuedAt: 1 },
				{ id: 'peer-two', content: 'same text', senderLabel: 'Reviewer', queuedAt: 1 }
			] }));
		const delivered = userMessage('peer-event', 'peer-one', 'full attributed envelope');
		hoisted.onEvent!(delivered);
		flushRaf();
		expect(deps.state.pendingPeerMessages?.map(m => m.id)).toEqual(['peer-two']);
		expect(deps.state.queuedMessages?.map(m => m.id)).toEqual(['human']);
		hoisted.onEvent!(delivered);
		flushRaf();
		expect(deps.state.pendingPeerMessages?.map(m => m.id)).toEqual(['peer-two']);
		expect(deps.state.messages?.filter(m => m.id === 'peer-event')).toHaveLength(1);
	});

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

	// A message STEERED into the running turn is recorded on THAT turn's id, not
	// the queued row's, so the id match above cannot see it. Its text is the only
	// remaining handle on the bubble it answers.
	function seedServerQueue(
		deps: ReturnType<typeof makeDeps>,
		rows: Array<{ id: string; content: string; queuedAt: number }>,
	): void {
		pendingQueue.noteAuthoritative('s1', rows);
		deps.update((s) => ({ ...s, queuedMessages: pendingQueue.reconcile('s1', rows) }));
	}

	it('drops a chip whose text is steered into the running turn', () => {
		const deps = makeDeps();
		feedController.attach(deps);
		feedController.start('s1');
		hoisted.onEvent!(caughtUp(0));

		seedServerQueue(deps, [{ id: 'turn-q', content: 'steer me', queuedAt: 1 }]);

		hoisted.onEvent!(userMessage('evt-u1', 'turn-live', 'steer me'));
		flushRaf();

		expect(deps.state.queuedMessages).toEqual([]);
	});

	it('keeps a row the server put back after the interrupt that swallowed it', () => {
		const deps = makeDeps();
		feedController.attach(deps);
		feedController.start('s1');
		hoisted.onEvent!(caughtUp(0));

		seedServerQueue(deps, [{ id: 'turn-q', content: 'steer me', queuedAt: 1 }]);
		hoisted.onEvent!(userMessage('evt-u1', 'turn-live', 'steer me'));
		flushRaf();
		expect(deps.state.queuedMessages).toEqual([]);

		// The turn that swallowed it was interrupted, so the backend re-queues the
		// message as a FRESH row. Its text is already in the transcript above and
		// that is intended — the bubble must come back.
		seedServerQueue(deps, [{ id: 'turn-q2', content: 'steer me', queuedAt: 2 }]);
		flushRaf();

		const queued = deps.state.queuedMessages as Array<{ id: string }>;
		expect(queued.map((q) => q.id)).toEqual(['turn-q2']);
	});

	it('ignores backfilled history when matching by text', () => {
		const deps = makeDeps();
		feedController.attach(deps);
		feedController.start('s1');

		// Replayed history (pre-catch-up): an old prompt with the same text.
		hoisted.onEvent!(userMessage('evt-old', 'turn-old', 'continue'));
		hoisted.onEvent!(caughtUp(0));

		seedServerQueue(deps, [{ id: 'turn-q', content: 'continue', queuedAt: 1 }]);
		flushRaf();

		const queued = deps.state.queuedMessages as Array<{ id: string }>;
		expect(queued.map((q) => q.id)).toEqual(['turn-q']);
	});
});

describe('feedController cross-device interaction settle (#606 item 4)', () => {
	it('interaction.answered with request_id clears a seeded pending permission', () => {
		const deps = makeDeps({
			pendingPermissions: [
				{ request_id: 'perm-1' },
				{ request_id: 'perm-2' },
			],
		} as unknown as Partial<ActiveSessionState>);
		feedController.attach(deps);
		feedController.start('s1');
		hoisted.onEvent!(caughtUp(0));

		hoisted.onEvent!(interaction('interaction.answered', 'perm-1'));

		expect(deps.resolveInteractionByRequestId).toHaveBeenCalledWith('perm-1');
		const perms = deps.state.pendingPermissions as Array<{ request_id: string }>;
		expect(perms.map((p) => p.request_id)).toEqual(['perm-2']);
	});

	it('interaction.expired with request_id clears a seeded pending question', () => {
		const deps = makeDeps({
			pendingQuestions: [{ request_id: 'q-1' }],
		} as unknown as Partial<ActiveSessionState>);
		feedController.attach(deps);
		feedController.start('s1');
		hoisted.onEvent!(caughtUp(0));

		hoisted.onEvent!(interaction('interaction.expired', 'q-1'));

		expect(deps.resolveInteractionByRequestId).toHaveBeenCalledWith('q-1');
		const qs = deps.state.pendingQuestions as Array<{ request_id: string }>;
		expect(qs).toHaveLength(0);
	});



	it('does nothing when the interaction event carries no request_id', () => {
		const deps = makeDeps({
			pendingPermissions: [{ request_id: 'perm-1' }],
		} as unknown as Partial<ActiveSessionState>);
		feedController.attach(deps);
		feedController.start('s1');
		hoisted.onEvent!(caughtUp(0));

		hoisted.onEvent!(interaction('interaction.answered', undefined));
		hoisted.onEvent!(interaction('interaction.expired', ''));

		expect(deps.resolveInteractionByRequestId).not.toHaveBeenCalled();
		const perms = deps.state.pendingPermissions as Array<{ request_id: string }>;
		expect(perms.map((p) => p.request_id)).toEqual(['perm-1']);
	});

	it('a durable question request triggers one authoritative pending-row resync', () => {
		const deps = makeDeps();
		feedController.attach(deps);
		feedController.start('s1');
		hoisted.onEvent!(caughtUp(0));

		for (const seq of [1, 2, 3]) {
			hoisted.onEvent!({
				type: 'interaction.requested',
				id: `evt-req-${seq}`,
				seq,
				session_id: 's1',
				kind: 'question',
				request_id: `question-${seq}`,
			});
		}
		expect(deps.requestPendingQuestions).not.toHaveBeenCalled();
		vi.advanceTimersByTime(0);
		expect(deps.requestPendingQuestions).toHaveBeenCalledTimes(1);
	});

	it('coalesces requested events spread across hydration chunks until caught_up', () => {
		const deps = makeDeps();
		feedController.attach(deps);
		feedController.start('s1');

		for (const seq of [1, 2, 3]) {
			hoisted.onEvent!({
				type: 'interaction.requested',
				id: `evt-history-${seq}`,
				seq,
				session_id: 's1',
				kind: 'question',
				request_id: `history-question-${seq}`,
			});
			vi.advanceTimersByTime(0);
		}
		expect(deps.requestPendingQuestions).not.toHaveBeenCalled();

		hoisted.onEvent!(caughtUp(3));
		vi.advanceTimersByTime(0);
		expect(deps.requestPendingQuestions).toHaveBeenCalledTimes(1);
	});

	it('flushes the deferred resync when the hydration floor completes without a sentinel', () => {
		const deps = makeDeps();
		feedController.attach(deps);
		feedController.start('s1');
		hoisted.onEvent!({
			type: 'interaction.requested',
			id: 'evt-floor',
			seq: 1,
			session_id: 's1',
			kind: 'question',
			request_id: 'floor-question',
		});
		hoisted.onEvent!(textDelta('floor-text', 'still loading'));

		vi.advanceTimersByTime(FIRST_FRAME_BUDGET_MS);
		vi.runOnlyPendingTimers();
		expect(deps.requestPendingQuestions).toHaveBeenCalledTimes(1);
	});

	// #1802: a permission raised on another worker (a chat running there, or a
	// remote subagent's relayed prompt) never reaches this device as a live
	// permission_request frame. The durable event is its only signal, so the
	// open chat must re-read the authoritative pending rows at once.
	it('a durable permission request while the chat is open resyncs pending permissions', () => {
		const deps = makeDeps();
		feedController.attach(deps);
		feedController.start('s1');
		hoisted.onEvent!(caughtUp(0));

		for (const seq of [1, 2]) {
			hoisted.onEvent!({
				type: 'interaction.requested',
				id: `evt-perm-${seq}`,
				seq,
				session_id: 's1',
				kind: 'permission',
				request_id: `perm-${seq}`,
			});
		}
		vi.advanceTimersByTime(0);
		expect(deps.requestPendingPermissions).toHaveBeenCalledTimes(1);
		expect(deps.requestPendingQuestions).not.toHaveBeenCalled();
	});

	it('skips the permission resync when the card is already shown', () => {
		const deps = makeDeps({
			pendingPermissions: [{ request_id: 'perm-live' }],
		} as unknown as Partial<ActiveSessionState>);
		feedController.attach(deps);
		feedController.start('s1');
		hoisted.onEvent!(caughtUp(0));
		hoisted.onEvent!({
			type: 'interaction.requested', id: 'evt-live', seq: 1, session_id: 's1',
			kind: 'permission', request_id: 'perm-live',
		});
		vi.advanceTimersByTime(0);
		expect(deps.requestPendingPermissions).not.toHaveBeenCalled();
	});

	it('resyncs each requested kind once when both arrive together', () => {
		const deps = makeDeps();
		feedController.attach(deps);
		feedController.start('s1');
		hoisted.onEvent!({
			type: 'interaction.requested', id: 'evt-h-perm', seq: 1, session_id: 's1',
			kind: 'permission', request_id: 'perm-h',
		});
		hoisted.onEvent!({
			type: 'interaction.requested', id: 'evt-h-q', seq: 2, session_id: 's1',
			kind: 'question', request_id: 'q-h',
		});
		vi.advanceTimersByTime(0);
		expect(deps.requestPendingPermissions).not.toHaveBeenCalled();

		hoisted.onEvent!(caughtUp(2));
		vi.advanceTimersByTime(0);
		expect(deps.requestPendingPermissions).toHaveBeenCalledTimes(1);
		expect(deps.requestPendingQuestions).toHaveBeenCalledTimes(1);
	});

	it('does not resync for another session', () => {
		const deps = makeDeps({
			pendingPermissions: [{ request_id: 'perm-1' }],
		} as unknown as Partial<ActiveSessionState>);
		feedController.attach(deps);
		feedController.start('s1');
		hoisted.onEvent!(caughtUp(0));

		// A request for a different feed session must never refresh this
		// session's queue.
		for (const kind of ['permission', 'question']) {
			hoisted.onEvent!({
				type: 'interaction.requested',
				id: `evt-other-${kind}`,
				seq: 1,
				session_id: 's2',
				kind,
				request_id: `other-${kind}`,
			});
		}
		hoisted.onEvent!(textDelta('e1', 'foo'));
		flushRaf();
		vi.advanceTimersByTime(0);

		expect(deps.resolveInteractionByRequestId).not.toHaveBeenCalled();
		expect(deps.requestPendingQuestions).not.toHaveBeenCalled();
		expect(deps.requestPendingPermissions).not.toHaveBeenCalled();
		const perms = deps.state.pendingPermissions as Array<{ request_id: string }>;
		expect(perms.map((p) => p.request_id)).toEqual(['perm-1']);
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

	it('#1282: seeding before each streamed delta does not rescan history per user row', () => {
		const TURNS = 300;
		const deps = makeDeps();
		feedController.attach(deps);
		feedController.start('s1');
		for (let i = 0; i < TURNS; i++) {
			hoisted.onEvent!(userMessage(`u-${i}`, `turn-${i}`, `question ${i}`));
			hoisted.onEvent!(textDelta(`a-${i}`, `answer ${i}`));
			hoisted.onEvent!({ type: 'turn.completed', id: `c-${i}`, seq: 0, session_id: 's1', turn_id: `turn-${i}` });
		}
		hoisted.onEvent!(caughtUp(0));
		const published = deps.state.messages as Array<{ role: string }>;
		expect(published.filter((m) => m.role === 'user')).toHaveLength(TURNS);

		// A live reply: every delta re-runs optimistic seeding before it is reduced.
		// It used to scan the whole reducer transcript once per historic user row
		// (O(user rows x messages) per SSE event). Count full-history scans.
		const some = vi.spyOn(Array.prototype, 'some');
		const historyScans = () => some.mock.contexts.filter((ctx) => Array.isArray(ctx) && ctx.length >= TURNS).length;
		try {
			hoisted.onEvent!(userMessage('u-live', 'turn-live', 'live question'));
			for (let i = 0; i < 20; i++) {
				hoisted.onEvent!(textDelta(`live-${i}`, 'x'));
				vi.advanceTimersByTime(STREAM_FOLD_MS);
			}
			expect(historyScans()).toBeLessThan(21);
		} finally {
			some.mockRestore();
		}
		const messages = deps.state.messages as Array<{ id: string; role: string; content: string }>;
		// Semantics unchanged: no historic prompt was re-seeded as a duplicate.
		expect(messages.filter((m) => m.role === 'user')).toHaveLength(TURNS + 1);
		expect(messages.at(-1)?.content).toBe('x'.repeat(20));
	});
});

// ─── revive() — heal a possibly-half-open feed on an out-of-band signal ──────
// (WS reconnect → active-session onConnected calls revive when already feeding;
// unlike start() it must NOT reset the reducer or stop the feed.)

describe('feedController revive (half-open feed healing)', () => {
	it('delegates to the feed when feeding the given session, without stopping it', () => {
		const deps = makeDeps();
		feedController.attach(deps);
		feedController.start('s1');
		const stopsAfterStart = hoisted.stops;

		feedController.revive('s1');
		expect(hoisted.revives).toBe(1);
		// No teardown: reviving must not stop the feed (that would reset the
		// reducer and cause the #637 repaint start() was guarded against).
		expect(hoisted.stops).toBe(stopsAfterStart);
		expect(feedController.isFeeding('s1')).toBe(true);
	});

	it('is a no-op for a session other than the one being fed', () => {
		const deps = makeDeps();
		feedController.attach(deps);
		feedController.start('s1');

		feedController.revive('s2');
		expect(hoisted.revives).toBe(0);
	});

	it('is a no-op when nothing is feeding', () => {
		feedController.revive('s1');
		expect(hoisted.revives).toBe(0);
	});

	it('with no session argument revives whatever is being fed', () => {
		const deps = makeDeps();
		feedController.attach(deps);
		feedController.start('s1');

		feedController.revive();
		expect(hoisted.revives).toBe(1);
	});
});

// ─── activeTasks from task.updated ───────────────────────────────────────────
// deriveActiveTasks keeps only summaries in state 'running', so the background
// tasks strip, the spinner overlay and the StopButton all hang off the RUNNING
// task.updated frame. A provider-lane worker that emitted only its terminal
// frame never appeared in any of them.

describe('feedController Swarm updates', () => {
 it('routes a durable update before a tool result and rejects another session', () => {
  const deps = makeDeps({ sessionId: 'swarm-feed-session' });
  feedController.attach(deps);
  feedController.start('swarm-feed-session');
  const swarm = { id: 'swarm-feed-test', session_id: 'swarm-feed-session', title: 'Review', status: 'running',
   created_at: '2026-09-07T10:00:00Z', updated_at: '2026-09-07T10:00:00Z', tasks: [] };
  hoisted.onEvent!({ id: 'swarm-event', seq: 1, type: 'swarm.updated', session_id: 'swarm-feed-session', swarm });
  expect(get(swarms).bySession[swarm.session_id][swarm.id].status).toBe('running');
  hoisted.onEvent!({ id: 'swarm-wrong', seq: 2, type: 'swarm.updated', session_id: 'swarm-feed-session', swarm: { ...swarm, session_id: 'other', status: 'failed' } });
  expect(get(swarms).bySession.other?.[swarm.id]).toBeUndefined();
  hoisted.onEvent!({ id: 'swarm-done', seq: 3, type: 'swarm.updated', session_id: 'swarm-feed-session', swarm: { ...swarm, status: 'completed', updated_at: '2026-09-07T10:01:00Z' } });
  expect(get(swarms).bySession[swarm.session_id][swarm.id].status).toBe('completed');
 });
});

describe('feedController activeTasks', () => {
	it('a running task.updated puts the task on the strip; the terminal frame clears it', () => {
		const deps = makeDeps();
		feedController.attach(deps);
		feedController.start('s1');
		hoisted.onEvent!(caughtUp(0)); // enter live mode

		hoisted.onEvent!(taskUpdated('w1', 'running', { description: 'reviewer' }));
		flushRaf();
		const active = deps.state.activeTasks as Array<{ toolUseId: string }>;
		expect(active).toHaveLength(1);
		expect(active[0].toolUseId).toBe('w1');

		hoisted.onEvent!(taskUpdated('w1', 'completed', { summary: 'prov-openai · gpt-5.5' }));
		flushRaf();
		expect(deps.state.activeTasks as unknown[]).toHaveLength(0);
	});

	// Audit item 4: a long-lived Monitor shell (a dev server) now records a
	// durable RUNNING row at spawn, so a REPLAY of the log — which is what a
	// reload and a session switch both do — has to repaint the strip with it.
	it('replays a running shell onto the strip on a from_seq=0 hydration', () => {
		const deps = makeDeps();
		feedController.attach(deps);
		feedController.start('s1');

		// Backfill (pre-`caught_up`): the spawn row, no terminal row after it.
		hoisted.onEvent!(
			taskUpdated('sh1', 'running', {
				kind: 'shell',
				description: 'npm run dev',
				tool_use_id: null,
			})
		);
		hoisted.onEvent!(caughtUp(1));
		flushRaf();

		const active = deps.state.activeTasks as Array<{ id: string; kind: string }>;
		expect(active).toHaveLength(1);
		expect(active[0].id).toBe('sh1');
		expect(active[0].kind).toBe('shell');
	});

	it('settles a shell with no tool_use_id on its terminal row', () => {
		const deps = makeDeps();
		feedController.attach(deps);
		feedController.start('s1');
		hoisted.onEvent!(caughtUp(0));

		hoisted.onEvent!(
			taskUpdated('sh2', 'running', { kind: 'shell', tool_use_id: null })
		);
		flushRaf();
		expect(deps.state.activeTasks as unknown[]).toHaveLength(1);

		hoisted.onEvent!(
			taskUpdated('sh2', 'completed', { kind: 'shell', tool_use_id: null })
		);
		flushRaf();
		expect(deps.state.activeTasks as unknown[]).toHaveLength(0);
	});

	it('a running shell survives turn.completed (the staleness heal exempts shells)', () => {
		const deps = makeDeps();
		feedController.attach(deps);
		feedController.start('s1');
		hoisted.onEvent!(caughtUp(0));

		hoisted.onEvent!(taskUpdated('sh3', 'running', { kind: 'shell' }));
		hoisted.onEvent!({
			type: 'turn.completed',
			id: 'evt-turn-done',
			seq: 0,
			session_id: 's1',
			created_at: '2026-07-12T00:00:00.000Z',
		});
		flushRaf();

		expect(deps.state.activeTasks as unknown[]).toHaveLength(1);
	});
});

// ─── Browser-panel auto-open on a live browser tool.use ──────────────────────

describe('feedController browser-panel auto-open', () => {
	it('auto-opens on a LIVE mcp__browser__* tool.use', () => {
		const deps = makeDeps();
		deps.state.sessionId = 'browser-live';
		feedController.attach(deps);
		feedController.start('browser-live');
		hoisted.onEvent!(caughtUp(0)); // enter live mode

		expect(browserPanel.forSession('browser-live').open).toBe(false);
		hoisted.onEvent!(toolUse('t1', 'mcp__browser__browser_navigate'));
		expect(browserPanel.forSession('browser-live').open).toBe(true);
	});

	it('selects browser over diff on each live browser launch, not on backfill', () => {
		const id = 'browser-over-diff';
		const deps = makeDeps();
		deps.state.sessionId = id;
		feedController.attach(deps);
		feedController.start(id);
		browserPanel.openPanel(id);
		sidePanel.open(sessionPanelKey(id), 'changes');
		hoisted.onEvent!(toolUse('history-browser', 'mcp__browser__browser_navigate'));
		expect(sidePanel.forScope(sessionPanelKey(id)).active).toBe('changes');
		hoisted.onEvent!(caughtUp(1));
		hoisted.onEvent!(toolUse('live-browser', 'mcp__browser__browser_navigate'));
		expect(sidePanel.forScope(sessionPanelKey(id)).active).toBe('browser');
		sidePanel.open(sessionPanelKey(id), 'changes');
		hoisted.onEvent!(toolUse('live-browser-again', 'mcp__browser__browser_snapshot'));
		expect(sidePanel.forScope(sessionPanelKey(id)).active).toBe('browser');
	});

	it('does NOT open for a non-browser tool.use', () => {
		const deps = makeDeps();
		deps.state.sessionId = 'browser-other';
		feedController.attach(deps);
		feedController.start('browser-other');
		hoisted.onEvent!(caughtUp(0));

		hoisted.onEvent!(toolUse('t1', 'Bash'));
		expect(browserPanel.forSession('browser-other').open).toBe(false);
	});

	it('does NOT open during history backfill (before caught_up)', () => {
		const deps = makeDeps();
		deps.state.sessionId = 'browser-history';
		feedController.attach(deps);
		feedController.start('browser-history');

		// A backfilled browser tool.use (no caught_up yet) must not pop the panel.
		hoisted.onEvent!(toolUse('t1', 'mcp__browser__browser_navigate'));
		expect(browserPanel.forSession('browser-history').open).toBe(false);

		// Once caught up, the same backfilled event is not replayed, so still closed.
		hoisted.onEvent!(caughtUp(1));
		expect(browserPanel.forSession('browser-history').open).toBe(false);
	});

	it('does NOT open after the user closed the panel for that session', () => {
		const deps = makeDeps();
		deps.state.sessionId = 'browser-dismissed';
		feedController.attach(deps);
		feedController.start('browser-dismissed');
		hoisted.onEvent!(caughtUp(0));

		// User dismissed the panel for s1 → a later browser tool must not reopen it.
		browserPanel.closePanel('browser-dismissed');
		hoisted.onEvent!(toolUse('t1', 'mcp__browser__browser_navigate'));
		expect(browserPanel.forSession('browser-dismissed').open).toBe(false);
	});
});

// ─── S2: transcript cache + from_seq resume ──────────────────────────────────

/** A user message carrying an explicit seq, so the reducer's resume cursor
 *  (lastSeq) actually advances and the state becomes cacheable. */
function userMessageAt(seq: number, content: string): Record<string, unknown> {
	return {
		type: 'message.user',
		id: `evt-u-${seq}`,
		seq,
		session_id: 's1',
		created_at: '2026-07-12T00:00:00.000Z',
		content,
	};
}

/** Hydrate a session to a complete, cacheable state: N messages + a real
 *  `caught_up` sentinel (the ONLY thing that makes a state cacheable, R1). */
function hydrate(sessionId: string, seqs: number[]): void {
	feedController.start(sessionId);
	for (const seq of seqs) hoisted.onEvent!(userMessageAt(seq, `m${seq}`));
	hoisted.onEvent!(caughtUp(seqs[seqs.length - 1] ?? 0));
}

describe('feedController parked turn', () => {
	it('publishes turnParked from turn.parked until the wake turn starts', () => {
		const deps = makeDeps();
		feedController.attach(deps);
		feedController.start('s1');
		hoisted.onEvent!(caughtUp(0));
		flushRaf();
		const turn = (type: string, id: string, turnId: string) => ({
			type, id, seq: 0, session_id: 's1', created_at: '2026-07-12T00:00:00.000Z', turn_id: turnId,
		});
		hoisted.onEvent!(turn('turn.started', 'ts1', 't1'));
		hoisted.onEvent!(turn('turn.completed', 'tc1', 't1'));
		hoisted.onEvent!(turn('turn.parked', 'tp1', 't1'));
		flushRaf();
		expect(deps.state.turnParked).toBe(true);
		// The last background task finishing does not end the park on its own.
		hoisted.onEvent!(taskUpdated('bg-1', 'completed', { kind: 'agent' }));
		flushRaf();
		expect(deps.state.turnParked).toBe(true);
		hoisted.onEvent!(turn('turn.started', 'ts2', 't2'));
		flushRaf();
		expect(deps.state.turnParked).toBe(false);
	});
});

describe('feedController long backfill (no visible replay)', () => {
	it('publishes nothing until caught_up, then the whole transcript in one update', () => {
		const deps = makeDeps({ isLoadingSession: true, isRestoringSession: true });
		feedController.attach(deps);
		feedController.start('s1');
		const callsAfterStart = deps.update.mock.calls.length;

		// A long log: one frame every 100ms for 6s, plus a same-instant burst.
		let seq = 0;
		for (let t = 0; t < 6_000; t += 100) {
			seq++;
			hoisted.onEvent!(userMessageAt(seq, `m${seq}`));
			vi.advanceTimersByTime(100);
		}
		for (let i = 0; i < 3_000; i++) hoisted.onEvent!(textDelta(`d${i}`, 'x'));

		// The reader saw the loading state the whole time — no partial transcript.
		expect(deps.update.mock.calls.length).toBe(callsAfterStart);
		expect(deps.state.messages).toHaveLength(0);
		expect(deps.state.isLoadingSession).toBe(true);
		expect(deps.state.isRestoringSession).toBe(true);

		hoisted.onEvent!(caughtUp(seq));
		expect(deps.update.mock.calls.length).toBe(callsAfterStart + 1);
		expect(deps.state.messages).toHaveLength(seq + 1);
		expect(deps.state.isLoadingSession).toBe(false);
		expect(deps.state.isRestoringSession).toBe(false);
		feedController.stop();
		expect(feedController.hasCachedTranscript('s1')).toBe(true);
	});

	it('keeps the status the view already shows while history replays turns and tasks', () => {
		const deps = makeDeps({ isLoadingSession: true, isRestoringSession: true, isStreaming: false });
		feedController.attach(deps);
		feedController.start('s1');
		const snapshots: Array<[unknown, unknown, unknown]> = [];
		const turn = (type: string, id: string, extra: Record<string, unknown> = {}) => ({
			type, id, seq: 0, session_id: 's1', created_at: '2026-07-12T00:00:00.000Z', turn_id: 't1', ...extra,
		});
		const history = [
			turn('turn.started', 'ts1'),
			taskUpdated('bg-1', 'running', { kind: 'agent' }),
			turn('turn.completed', 'tc1'),
			turn('turn.parked', 'tp1'),
			taskUpdated('bg-1', 'completed', { kind: 'agent' }),
			turn('turn.started', 'ts2', { turn_id: 't2' }),
			turn('turn.completed', 'tc2', { turn_id: 't2' }),
		];
		for (const ev of history) {
			hoisted.onEvent!(ev);
			vi.advanceTimersByTime(400);
			snapshots.push([deps.state.isStreaming, deps.state.optimisticStreaming, deps.state.activeTasks]);
		}
		// Working → parked → done replayed without the view ever seeing a step.
		expect(new Set(snapshots.map((s) => JSON.stringify(s))).size).toBe(1);
		expect(deps.state.isStreaming).toBe(false);

		hoisted.onEvent!(caughtUp(7));
		expect(deps.state.isStreaming).toBe(false);
		expect(deps.state.isLoadingSession).toBe(false);
	});

	it('a replayed rewind before caught_up leaves only the surviving messages', () => {
		const deps = makeDeps({ isLoadingSession: true, isRestoringSession: true });
		feedController.attach(deps);
		feedController.start('s1');
		hoisted.onEvent!(userMessageAt(1, 'kept'));
		vi.advanceTimersByTime(750);
		hoisted.onEvent!(textDelta('reply', 'ok'));
		vi.advanceTimersByTime(750);
		hoisted.onEvent!(userMessageAt(2, 'rewound away'));
		hoisted.onEvent!({
			type: 'transcript.rewound',
			id: 'evt-rw',
			seq: 3,
			session_id: 's1',
			created_at: '2026-07-12T00:00:00.000Z',
			from_seq: 2,
		});
		hoisted.onEvent!(userMessageAt(4, 'after'));
		hoisted.onEvent!(caughtUp(4));

		const msgs = deps.state.messages as Array<{ content: string }>;
		expect(msgs.map((m) => m.content)).toEqual(['kept', 'ok', 'after']);
		expect(deps.state.isStreaming).toBe(false);
	});

	it('a cache resume never repaints mid-backfill', () => {
		const deps = makeDeps();
		feedController.attach(deps);
		hydrate('s1', [1, 2, 3]);
		feedController.stop();

		feedController.start('s1');
		const callsAfterResume = deps.update.mock.calls.length;
		for (let seq = 4; seq < 40; seq++) {
			hoisted.onEvent!(userMessageAt(seq, `m${seq}`));
			vi.advanceTimersByTime(100);
		}
		expect(deps.update.mock.calls.length).toBe(callsAfterResume);
	});
});

describe('feedController unreadable session (#1908)', () => {
	it.each([404, 403])('stops the feed and tells the store once on HTTP %i', (status) => {
		const onSessionUnavailable = vi.fn();
		const deps = { ...makeDeps({ isLoadingSession: true }), onSessionUnavailable };
		feedController.attach(deps);
		feedController.start('s1');
		expect(feedController.isFeeding('s1')).toBe(true);

		hoisted.onUnavailable!(status);
		expect(onSessionUnavailable).toHaveBeenCalledTimes(1);
		expect(onSessionUnavailable).toHaveBeenCalledWith('s1', status);
		expect(feedController.isFeeding()).toBe(false);
		expect(hoisted.stops).toBe(1);
		expect(feedController.hasCachedTranscript('s1')).toBe(false);

		// A late duplicate from the dead feed is ignored.
		hoisted.onUnavailable!(status);
		expect(onSessionUnavailable).toHaveBeenCalledTimes(1);
	});

	it('ignores an unavailable answer from a feed it already replaced', () => {
		const onSessionUnavailable = vi.fn();
		const deps = { ...makeDeps(), onSessionUnavailable };
		feedController.attach(deps);
		feedController.start('s1');
		const stale = hoisted.onUnavailable!;
		feedController.restart('s1');
		expect(hoisted.creates).toBe(2);
		stale(404);
		expect(onSessionUnavailable).not.toHaveBeenCalled();
		expect(feedController.isFeeding('s1')).toBe(true);
	});
});

describe('feedController log epoch (resume verification)', () => {
	it('caches the log epoch with the transcript and sends it on resume', () => {
		const deps = makeDeps();
		feedController.attach(deps);
		hoisted.feedEpoch = 'ep-1';
		hydrate('s1', [1, 2, 3]);
		expect(feedController.hasLogEpoch('s1')).toBe(true);
		expect(feedController.hasLogEpoch('other')).toBe(false);
		feedController.stop();
		hoisted.feedEpoch = null;

		feedController.start('s1');
		expect(hoisted.fromSeq).toBe(3);
		expect(hoisted.epochs.at(-1)).toBe('ep-1');
		expect(feedController.hasLogEpoch('s1')).toBe(true);
	});

	it('a fresh load sends no epoch, and a feed without one reports none', () => {
		const deps = makeDeps();
		feedController.attach(deps);
		feedController.start('s1');
		expect(hoisted.epochs.at(-1)).toBeNull();
		expect(feedController.hasLogEpoch('s1')).toBe(false);
	});

	it('a reset frame drops the cached transcript and reloads the session from seq 0', () => {
		const deps = makeDeps();
		feedController.attach(deps);
		hoisted.feedEpoch = 'ep-1';
		hydrate('s1', [1, 2, 3]);
		feedController.stop();
		hoisted.feedEpoch = null;

		feedController.start('s1');
		expect(hoisted.fromSeq).toBe(3);
		const opened = hoisted.creates;
		hoisted.onEvent!({ type: 'reset', reason: 'epoch', epoch: 'ep-2' });

		expect(feedController.hasCachedTranscript('s1')).toBe(false);
		expect(hoisted.creates).toBe(opened + 1);
		expect(hoisted.fromSeq).toBe(0);
		expect(hoisted.epochs.at(-1)).toBeNull();
		expect(deps.state.messages).toHaveLength(0);
		expect(deps.state.isLoadingSession).toBe(true);

		// The new log hydrates atomically as a fresh load.
		hoisted.onEvent!(userMessageAt(1, 'new log'));
		hoisted.onEvent!(caughtUp(1));
		expect((deps.state.messages as Array<{ content: string }>).map((m) => m.content)).toEqual([
			'new log',
		]);
	});

	it('invalidateUnverified() keeps only transcripts the server can vouch for', () => {
		const deps = makeDeps();
		feedController.attach(deps);
		hoisted.feedEpoch = 'ep-a';
		hydrate('with-epoch', [1, 2]);
		feedController.stop();
		hoisted.feedEpoch = null;
		hydrate('no-epoch', [1, 2]);
		feedController.stop();

		feedController.invalidateUnverified();
		expect(feedController.hasCachedTranscript('with-epoch')).toBe(true);
		expect(feedController.hasCachedTranscript('no-epoch')).toBe(false);
	});
});

describe('feedController transcript cache (S2)', () => {
	it('a switch back folds the cached transcript synchronously and resumes at lastSeq', () => {
		const deps = makeDeps();
		feedController.attach(deps);

		// Open s1, hydrate it, then leave (the switcher stops the feed).
		hydrate('s1', [1, 2, 3]);
		expect(hoisted.opened[0]).toEqual({ sessionId: 's1', fromSeq: 0 });
		feedController.stop();

		// Come back. The transcript must be on screen in the SAME synchronous
		// call — no spinner, no empty list, no fold scheduled for later.
		deps.update.mockClear();
		deps.update((s) => ({ ...s, messages: [], isLoadingSession: true, isRestoringSession: true }));
		const callsBeforeStart = deps.update.mock.calls.length;
		feedController.start('s1');

		expect(deps.update.mock.calls.length).toBe(callsBeforeStart + 1);
		expect((deps.state.messages as unknown[]).length).toBe(3);
		expect(deps.state.isLoadingSession).toBe(false);
		expect(deps.state.isRestoringSession).toBe(false);

		// …and the feed resumes at the cached cursor instead of replaying the log.
		expect(hoisted.fromSeq).toBe(3);
	});

	it('the resumed feed appends the delta without re-downloading history', () => {
		const deps = makeDeps();
		feedController.attach(deps);
		hydrate('s1', [1, 2, 3]);
		feedController.stop();

		feedController.start('s1');
		hoisted.onEvent!(userMessageAt(4, 'while-away'));
		hoisted.onEvent!(caughtUp(4));

		const msgs = deps.state.messages as Array<{ content: string }>;
		expect(msgs.map((m) => m.content)).toEqual(['m1', 'm2', 'm3', 'while-away']);
	});

	it('R1: a hydration completed by the FLOOR (no sentinel) is never cached', () => {
		const deps = makeDeps();
		feedController.attach(deps);
		feedController.start('s1');
		hoisted.onEvent!(userMessageAt(1, 'partial'));
		// Stall → the floor publishes what arrived, but the sentinel is still owed
		// so this state is NOT known to be complete.
		vi.advanceTimersByTime(CATCHUP_FALLBACK_MS);
		expect(deps.state.messages).toHaveLength(1);
		feedController.stop();

		expect(feedController.hasCachedTranscript('s1')).toBe(false);
		feedController.start('s1');
		expect(hoisted.fromSeq).toBe(0); // full replay
	});

	it('R2: restart() discards the cache and replays from seq 0', () => {
		const deps = makeDeps();
		feedController.attach(deps);
		hydrate('s1', [1, 2, 3]);
		expect(feedController.hasCachedTranscript('s1')).toBe(true);

		// The rewind / reload-session path: the state it was built from is no
		// longer trustworthy.
		feedController.restart('s1');
		expect(feedController.hasCachedTranscript('s1')).toBe(false);
		expect(hoisted.fromSeq).toBe(0);
		expect(deps.state.messages).toHaveLength(0);
		expect(deps.state.isLoadingSession).toBe(true);
	});

	it('R2: a rewind replayed on a RESUMED feed truncates, so it needs no invalidation', () => {
		const deps = makeDeps();
		feedController.attach(deps);
		hydrate('s1', [1, 2, 3]);
		feedController.stop();

		// The rewind happened while the user was away. It is an APPENDED event
		// (never a row deletion), so the resume replays it and the reducer drops
		// everything created at/after its from_seq — exactly what a full replay
		// would have produced.
		feedController.start('s1');
		hoisted.onEvent!({
			type: 'transcript.rewound',
			id: 'evt-rw',
			seq: 4,
			session_id: 's1',
			created_at: '2026-07-12T00:00:00.000Z',
			from_seq: 2,
		});
		hoisted.onEvent!(caughtUp(4));

		const msgs = deps.state.messages as Array<{ content: string }>;
		expect(msgs.map((m) => m.content)).toEqual(['m1']);
	});

	it('R3: invalidate() drops one session, or all of them', () => {
		const deps = makeDeps();
		feedController.attach(deps);
		hydrate('s1', [1]);
		feedController.stop();
		expect(feedController.hasCachedTranscript('s1')).toBe(true);

		feedController.invalidate('s1');
		expect(feedController.hasCachedTranscript('s1')).toBe(false);

		hydrate('s1', [1]);
		feedController.stop();
		feedController.invalidate();
		expect(cachedTranscriptCount()).toBe(0);
	});

	it('R4: an event at/below the resume cursor discards the cache and replays from 0', () => {
		const deps = makeDeps();
		feedController.attach(deps);
		hydrate('s1', [1, 2, 3]);
		feedController.stop();

		feedController.start('s1');
		expect(hoisted.fromSeq).toBe(3);

		// The server filter is `seq > from_seq`, so this cannot happen for the log
		// the cache was built from: the log was rewritten (restore, seq reset, an
		// id reused by a fork). Trusting the cache here would show a transcript
		// that never existed.
		hoisted.onEvent!(userMessageAt(2, 'not-our-log'));

		expect(feedController.hasCachedTranscript('s1')).toBe(false);
		expect(hoisted.fromSeq).toBe(0); // re-hydrating from scratch
		expect(deps.state.isLoadingSession).toBe(true);
		expect(deps.state.messages).toHaveLength(0);
	});

	it('R5: an entry older than the TTL is ignored and dropped', () => {
		const deps = makeDeps();
		feedController.attach(deps);
		hydrate('s1', [1, 2]);
		feedController.stop();

		vi.advanceTimersByTime(TRANSCRIPT_CACHE_TTL_MS + 1);
		expect(feedController.hasCachedTranscript('s1')).toBe(false);
		feedController.start('s1');
		expect(hoisted.fromSeq).toBe(0);
	});

	it('R6: the cache is LRU-capped', () => {
		const deps = makeDeps();
		feedController.attach(deps);
		// getState() reports sessionId 's1', and the fold only writes for the
		// session in view — irrelevant here, the cache is keyed by the FED id.
		for (let i = 0; i < TRANSCRIPT_CACHE_MAX + 3; i++) {
			hydrate(`sess-${i}`, [1, 2]);
			feedController.stop();
		}
		expect(cachedTranscriptCount()).toBe(TRANSCRIPT_CACHE_MAX);
		expect(feedController.hasCachedTranscript('sess-0')).toBe(false);
		expect(feedController.hasCachedTranscript(`sess-${TRANSCRIPT_CACHE_MAX + 2}`)).toBe(true);
	});

	it('cachedPatch() previews the same transcript the switcher primes with', () => {
		const deps = makeDeps();
		feedController.attach(deps);
		hydrate('s1', [1, 2]);
		feedController.stop();

		const patch = feedController.cachedPatch('s1');
		expect(patch).not.toBeNull();
		expect((patch!.messages as unknown[]).length).toBe(2);
		expect(patch!.isLoadingSession).toBe(false);
		expect(patch!.isRestoringSession).toBe(false);
		expect(feedController.cachedPatch('never-opened')).toBeNull();
	});
});

// ─── S6: isStreaming has ONE writer ──────────────────────────────────────────

describe('feedController isStreaming single-writer (S6)', () => {
	function turnStarted(seq: number): Record<string, unknown> {
		return {
			type: 'turn.started',
			id: `evt-turn-${seq}`,
			seq,
			session_id: 's1',
			created_at: '2026-07-12T00:00:00.000Z',
			turn_id: `turn-${seq}`,
		};
	}
	function turnCompleted(seq: number, turnId: string): Record<string, unknown> {
		return {
			type: 'turn.completed',
			id: `evt-done-${seq}`,
			seq,
			session_id: 's1',
			created_at: '2026-07-12T00:00:00.000Z',
			turn_id: turnId,
		};
	}

	it('a fold between the optimistic flip and turn.started never flips it back', () => {
		// This is the reported stutter: the send (or a WS `start`) set streaming
		// true, then an SSE frame folded BEFORE `turn.started` reset it from the
		// reducer, then `turn.started` set it true again.
		const deps = makeDeps({ isStreaming: true, optimisticStreaming: true });
		feedController.attach(deps);
		feedController.start('s1');
		hoisted.onEvent!(caughtUp(0));
		expect(deps.state.isStreaming).toBe(true);

		// A frame that is NOT turn.started folds — the indicator must hold.
		hoisted.onEvent!(userMessageAt(1, 'hi'));
		flushRaf();
		expect(deps.state.isStreaming).toBe(true);
		expect(deps.state.optimisticStreaming).toBe(true);

		// The server confirms: the reducer takes over and the flag is retired.
		hoisted.onEvent!(turnStarted(2));
		flushRaf();
		expect(deps.state.isStreaming).toBe(true);
		expect(deps.state.optimisticStreaming).toBe(false);

		// And the reducer alone ends it — no stale optimistic flag revives it.
		hoisted.onEvent!(turnCompleted(3, 'turn-2'));
		flushRaf();
		expect(deps.state.isStreaming).toBe(false);
		expect(deps.state.optimisticStreaming).toBe(false);
	});

	it('noteOptimisticSend/clearOptimisticSend drive the same single flag', () => {
		const deps = makeDeps();
		feedController.attach(deps);
		feedController.start('s1');
		hoisted.onEvent!(caughtUp(0));

		feedController.noteOptimisticSend('s1');
		hoisted.onEvent!(userMessageAt(1, 'hi'));
		flushRaf();
		expect(deps.state.isStreaming).toBe(true);

		feedController.clearOptimisticSend('s1');
		hoisted.onEvent!(userMessageAt(2, 'hi again'));
		flushRaf();
		expect(deps.state.isStreaming).toBe(false);
	});

	it('isReducerStreaming reports the authoritative value for the fed session', () => {
		const deps = makeDeps();
		feedController.attach(deps);
		feedController.start('s1');
		hoisted.onEvent!(caughtUp(0));
		expect(feedController.isReducerStreaming('s1')).toBe(false);

		hoisted.onEvent!(turnStarted(1));
		expect(feedController.isReducerStreaming('s1')).toBe(true);
		// Not the session being fed → never claims to know.
		expect(feedController.isReducerStreaming('other')).toBe(false);
		expect(feedController.isReducerStreaming(null)).toBe(false);
	});
});

// ─── W7: the feed owns usage once it has published any ───────────────────────

describe('feedController usage ownership (W7)', () => {
	function turnUsage(seq: number, inputTokens: number): Record<string, unknown> {
		return {
			type: 'turn.usage',
			id: `evt-usage-${seq}`,
			seq,
			session_id: 's1',
			created_at: '2026-07-12T00:00:00.000Z',
			usage: { input_tokens: inputTokens, output_tokens: 1 },
		};
	}

	it('keeps the output row a session total across per-call usage records', () => {
		const deps = makeDeps({ totalTokensOut: 900 }); // seeded from /live
		feedController.attach(deps);
		feedController.start('s1');
		hoisted.onEvent!(caughtUp(0));
		// Per-call records (partial while streaming) never replace the total.
		hoisted.onEvent!(turnUsage(1, 100));
		flushRaf();
		expect(deps.state.totalTokensOut).toBe(900);
		const completed = (seq: number, tokensOut: number) => ({
			type: 'turn.completed', id: `evt-done-${seq}`, seq, session_id: 's1',
			created_at: '2026-07-12T00:00:00.000Z', usage: { tokens_in: 10, tokens_out: tokensOut },
		});
		hoisted.onEvent!(completed(2, 400));
		hoisted.onEvent!(turnUsage(3, 120));
		hoisted.onEvent!(completed(4, 250));
		flushRaf();
		// The replayed log sums every completed turn (was: the last call's 1).
		expect(deps.state.totalTokensOut).toBe(650);
	});

	it('claims the usage fields only once a turn.usage has been folded', () => {
		const deps = makeDeps();
		feedController.attach(deps);
		feedController.start('s1');
		hoisted.onEvent!(caughtUp(0));
		// A slow /live snapshot landing now is the only source there is.
		expect(feedController.hasAppliedUsage('s1')).toBe(false);

		hoisted.onEvent!(turnUsage(1, 12345));
		flushRaf();
		expect(deps.state.totalTokensIn).toBe(12345);
		// From here a snapshot carrying older DB totals must not roll it back.
		expect(feedController.hasAppliedUsage('s1')).toBe(true);
		expect(feedController.hasAppliedUsage('other')).toBe(false);
	});

	it('ownership survives a switch away and back through the cache', () => {
		const deps = makeDeps();
		feedController.attach(deps);
		feedController.start('s1');
		hoisted.onEvent!(userMessageAt(1, 'hi'));
		hoisted.onEvent!(turnUsage(2, 999));
		hoisted.onEvent!(caughtUp(2));
		feedController.stop();

		expect(feedController.hasAppliedUsage('s1')).toBe(true);
		feedController.start('s1');
		expect(feedController.hasAppliedUsage('s1')).toBe(true);
	});
});
