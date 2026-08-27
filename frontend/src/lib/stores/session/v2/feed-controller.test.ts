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
	creates: 0,
	stops: 0,
	revives: 0,
}));

vi.mock('./sse-feed', () => ({
	createSseFeed: (
		_sessionId: string,
		opts: { onEvent: (e: Record<string, unknown>) => void; onOpen?: () => void },
	) => {
		hoisted.creates++;
		hoisted.onEvent = opts.onEvent;
		hoisted.onOpen = opts.onOpen ?? null;
		return {
			get running() {
				return true;
			},
			get lastSeq() {
				return 0;
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

import { feedController, CATCHUP_FALLBACK_MS } from './feed-controller';
import { pendingQueue } from '../pending-queue';
import { browserPanel } from '../../browser-panel.svelte';
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
	return {
		update,
		getState,
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
	hoisted.creates = 0;
	hoisted.stops = 0;
	hoisted.revives = 0;
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

	it('(a-atomic) a flowing backfill never publishes partially — caught_up is the boundary', () => {
		const deps = makeDeps({ isLoadingSession: true, isRestoringSession: true });
		feedController.attach(deps);
		feedController.start('s1');
		const callsAfterStart = deps.update.mock.calls.length;

		// Gaps SHORTER than the idle window are an alive stream, not a stall: each
		// frame re-arms the fallback, so a paging backfill stays atomic no matter
		// how many pages it takes. caught_up remains the hydration boundary.
		for (let i = 0; i < 20; i++) {
			hoisted.onEvent!(textDelta(`e${i}`, 'x'));
			vi.advanceTimersByTime(CATCHUP_FALLBACK_MS - 100);
		}

		expect(deps.update.mock.calls.length).toBe(callsAfterStart);
		expect(deps.state.messages).toHaveLength(0);
		expect(deps.state.isLoadingSession).toBe(true);
		expect(deps.state.isRestoringSession).toBe(true);

		hoisted.onEvent!(caughtUp(20));
		expect(deps.update.mock.calls.length).toBe(callsAfterStart + 1);
		expect((deps.state.messages as Array<{ content: string }>)[0].content).toBe('x'.repeat(20));
		expect(deps.state.isLoadingSession).toBe(false);
		expect(deps.state.isRestoringSession).toBe(false);
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

	it('(a-fallback) a connect that never delivers a frame still clears the spinner', () => {
		const deps = makeDeps({ isLoadingSession: true, isRestoringSession: true });
		feedController.attach(deps);
		feedController.start('s1');

		// Armed at start(), before the first connect resolves — so a feed that
		// opens and then says nothing at all is bounded too.
		vi.advanceTimersByTime(CATCHUP_FALLBACK_MS);
		expect(deps.state.isLoadingSession).toBe(false);
		expect(deps.state.isRestoringSession).toBe(false);
	});

	it('(a-fallback) events after the fallback fired fold at the live cadence', () => {
		const deps = makeDeps();
		feedController.attach(deps);
		feedController.start('s1');
		vi.advanceTimersByTime(CATCHUP_FALLBACK_MS);
		const callsAfterFallback = deps.update.mock.calls.length;

		hoisted.onEvent!(textDelta('e1', 'late'));
		flushRaf();
		expect(deps.update.mock.calls.length).toBe(callsAfterFallback + 1);
		expect((deps.state.messages as Array<{ content: string }>)[0].content).toBe('late');
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
		// the normal rAF/250ms cadence — it must not wait for another sentinel.
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

	it('live system.note with permissionMode updates the composer chip', () => {
		const setPermissionMode = vi.fn();
		const deps = makeDeps();
		feedController.attach({ ...deps, setPermissionMode });
		feedController.start('s1');
		hoisted.onEvent!(caughtUp(0));

		hoisted.onEvent!({
			type: 'system.note',
			id: 'n1',
			seq: 1,
			subtype: 'status',
			data: { permissionMode: 'plan', content: 'Switched to plan mode.' },
		});

		expect(setPermissionMode).toHaveBeenCalledWith('plan');
	});

	it('backfilled system.note with permissionMode does not rewind the chip', () => {
		const setPermissionMode = vi.fn();
		const deps = makeDeps();
		feedController.attach({ ...deps, setPermissionMode });
		feedController.start('s1');

		hoisted.onEvent!({
			type: 'system.note',
			id: 'n1',
			seq: 1,
			subtype: 'status',
			data: { permissionMode: 'plan' },
		});

		expect(setPermissionMode).not.toHaveBeenCalled();
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

	it('other event types never trigger a settle', () => {
		const deps = makeDeps({
			pendingPermissions: [{ request_id: 'perm-1' }],
		} as unknown as Partial<ActiveSessionState>);
		feedController.attach(deps);
		feedController.start('s1');
		hoisted.onEvent!(caughtUp(0));

		// A requested event (card creation stays on the legacy relay) and a text
		// delta must NOT settle anything.
		hoisted.onEvent!({
			type: 'interaction.requested',
			id: 'evt-req',
			seq: 0,
			session_id: 's1',
			request_id: 'perm-1',
		});
		hoisted.onEvent!(textDelta('e1', 'foo'));
		flushRaf();

		expect(deps.resolveInteractionByRequestId).not.toHaveBeenCalled();
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
});

// ─── Browser-panel auto-open on a live browser tool.use ──────────────────────

describe('feedController browser-panel auto-open', () => {
	beforeEach(() => {
		// Clean, closed baseline with no close-suppression for the fed session.
		browserPanel.closePanel(null);
	});

	it('auto-opens on a LIVE mcp__browser__* tool.use', () => {
		const deps = makeDeps();
		feedController.attach(deps);
		feedController.start('s1');
		hoisted.onEvent!(caughtUp(0)); // enter live mode

		expect(browserPanel.current.open).toBe(false);
		hoisted.onEvent!(toolUse('t1', 'mcp__browser__browser_navigate'));
		expect(browserPanel.current.open).toBe(true);
	});

	it('does NOT open for a non-browser tool.use', () => {
		const deps = makeDeps();
		feedController.attach(deps);
		feedController.start('s1');
		hoisted.onEvent!(caughtUp(0));

		hoisted.onEvent!(toolUse('t1', 'Bash'));
		expect(browserPanel.current.open).toBe(false);
	});

	it('does NOT open during history backfill (before caught_up)', () => {
		const deps = makeDeps();
		feedController.attach(deps);
		feedController.start('s1');

		// A backfilled browser tool.use (no caught_up yet) must not pop the panel.
		hoisted.onEvent!(toolUse('t1', 'mcp__browser__browser_navigate'));
		expect(browserPanel.current.open).toBe(false);

		// Once caught up, the same backfilled event is not replayed, so still closed.
		hoisted.onEvent!(caughtUp(1));
		expect(browserPanel.current.open).toBe(false);
	});

	it('does NOT open after the user closed the panel for that session', () => {
		const deps = makeDeps();
		feedController.attach(deps);
		feedController.start('s1');
		hoisted.onEvent!(caughtUp(0));

		// User dismissed the panel for s1 → a later browser tool must not reopen it.
		browserPanel.closePanel('s1');
		hoisted.onEvent!(toolUse('t1', 'mcp__browser__browser_navigate'));
		expect(browserPanel.current.open).toBe(false);
	});
});
