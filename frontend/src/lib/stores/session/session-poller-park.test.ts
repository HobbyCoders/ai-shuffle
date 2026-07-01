/**
 * Tests for the session-poller PARK-POLL lifecycle (Bug 2 fix).
 *
 * The poller used to STOP entirely the moment `is_streaming` flipped false, so a
 * turn parked waiting on a long-running background task went stale and never
 * auto-refreshed. The new lifecycle keeps polling at a slow "park" rate while
 * `activeTasks` is non-empty, upshifts back to the fast streaming rate when the
 * turn wakes, and only fully stops once it's both not streaming AND has no
 * active tasks.
 *
 * We drive the real poller with a scripted fetch mock and fake timers, then
 * assert WHEN it polls (fast vs slow) and WHEN it stops, by counting fetches as
 * we advance the clock past the streaming (150ms) and park (1500ms) intervals.
 */

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { sessionPoller } from './session-poller';
import { sessionStateManager } from './session-state.svelte';

const SESSION_ID = 'session-park-poll';
const STREAMING_MS = 150;
const PARK_MS = 1500;

type LiveOpts = {
	lastUpdateId: number;
	isStreaming: boolean;
	activeTasks?: Array<Record<string, unknown>>;
	unparkPending?: boolean;
};

function makeLive(opts: LiveOpts): Response {
	return {
		ok: true,
		status: 200,
		json: async () => ({
			last_update_id: opts.lastUpdateId,
			is_streaming: opts.isStreaming,
			messages: [
				{ id: 'u1', role: 'user', type: 'text', content: 'hi', streaming: false },
				{ id: 'a1', role: 'assistant', type: 'text', content: 'working', streaming: opts.isStreaming },
			],
			initial_message_count: 0,
			usage: {},
			activeTasks: opts.activeTasks ?? [],
			unpark_pending: opts.unparkPending ?? false,
		}),
	} as unknown as Response;
}

const TASK = {
	id: 'task-1',
	subject: 'Run the suite',
	status: 'running',
	kind: 'shell',
	summary: null,
	startedAt: new Date().toISOString(),
	updatedAt: new Date().toISOString(),
};

describe('session-poller · park-poll lifecycle', () => {
	let fetchMock: ReturnType<typeof vi.fn>;

	beforeEach(() => {
		vi.useFakeTimers();
		fetchMock = vi.fn();
		vi.stubGlobal('fetch', fetchMock);
	});

	afterEach(() => {
		sessionPoller.stopAll();
		sessionStateManager.removeSession(SESSION_ID);
		vi.unstubAllGlobals();
		vi.useRealTimers();
		vi.restoreAllMocks();
	});

	/** Resolve the immediate startStreaming poll and let its .then() chain run. */
	async function flush() {
		await vi.runOnlyPendingTimersAsync();
	}

	it('keeps polling (does NOT stop) when parked with active tasks, then upshifts on wake and stops when idle', async () => {
		// 1) STREAMING — startStreaming fires an immediate poll, then a fast timer.
		fetchMock.mockResolvedValue(makeLive({ lastUpdateId: 1, isStreaming: true }));
		sessionPoller.startStreaming(SESSION_ID);
		await flush();
		expect(sessionPoller.isPolling(SESSION_ID)).toBe(true);

		// Confirm the FAST rate: one 150ms tick fires exactly one poll.
		fetchMock.mockClear();
		await vi.advanceTimersByTimeAsync(STREAMING_MS);
		expect(fetchMock.mock.calls.length).toBe(1); // streaming-rate tick

		// 2) PARKED + tasks — the next poll reports not-streaming but with an
		//    active task. The poller must DOWNSHIFT (keep polling slowly), NOT stop.
		fetchMock.mockClear();
		fetchMock.mockResolvedValue(
			makeLive({ lastUpdateId: 2, isStreaming: false, activeTasks: [TASK] })
		);
		await vi.advanceTimersByTimeAsync(STREAMING_MS); // the poll that parks us
		expect(sessionPoller.isPolling(SESSION_ID)).toBe(true); // still polling — Bug 2 fix

		// Confirm the SLOW (park) rate: 9 further 150ms ticks (1350ms < 1500ms park
		// interval) fire NO poll. At the streaming rate these would have been 9.
		fetchMock.mockClear();
		await vi.advanceTimersByTimeAsync(9 * STREAMING_MS);
		expect(fetchMock.mock.calls.length).toBe(0); // downshifted — no fast polls
		expect(sessionPoller.isPolling(SESSION_ID)).toBe(true);

		// Crossing the full 1500ms park interval fires exactly one park poll.
		fetchMock.mockClear();
		await vi.advanceTimersByTimeAsync(PARK_MS);
		expect(fetchMock.mock.calls.length).toBe(1); // park-rate tick fired
		expect(sessionPoller.isPolling(SESSION_ID)).toBe(true);

		// 3) WAKE → STREAMING again (upshift). Next park poll reports streaming.
		fetchMock.mockClear();
		fetchMock.mockResolvedValue(makeLive({ lastUpdateId: 3, isStreaming: true }));
		await vi.advanceTimersByTimeAsync(PARK_MS); // park tick → sees streaming → upshift
		expect(sessionPoller.isPolling(SESSION_ID)).toBe(true);

		// Confirm we're back at the FAST rate: a single 150ms tick fires a poll,
		// and that poll reports PARKED with NO tasks → the poller must FULLY STOP.
		fetchMock.mockClear();
		fetchMock.mockResolvedValue(
			makeLive({ lastUpdateId: 4, isStreaming: false, activeTasks: [] })
		);
		await vi.advanceTimersByTimeAsync(STREAMING_MS);
		expect(fetchMock.mock.calls.length).toBe(1); // fast-rate tick confirmed
		expect(sessionPoller.isPolling(SESSION_ID)).toBe(false); // fully stopped — idle
	});

	it('keeps park-polling across the unpark hand-off gap (no tasks, unpark_pending) instead of stopping', async () => {
		// 1) STREAMING — startStreaming fires an immediate poll, then a fast timer.
		fetchMock.mockResolvedValue(makeLive({ lastUpdateId: 1, isStreaming: true }));
		sessionPoller.startStreaming(SESSION_ID);
		await flush();
		expect(sessionPoller.isPolling(SESSION_ID)).toBe(true);

		// 2) PARKED + a background task → downshift to the park rate.
		fetchMock.mockResolvedValue(
			makeLive({ lastUpdateId: 2, isStreaming: false, activeTasks: [TASK] })
		);
		await vi.advanceTimersByTimeAsync(STREAMING_MS);
		expect(sessionPoller.isPolling(SESSION_ID)).toBe(true);

		// 3) THE GAP — the sub-agent completed (dropped from activeTasks) but the
		//    unpark follow-up hasn't flipped is_streaming yet. Backend latches
		//    unpark_pending. Without honoring it, this (false, []) poll would STOP
		//    the poller and the follow-up would stream unseen. It must NOT stop.
		fetchMock.mockResolvedValue(
			makeLive({ lastUpdateId: 3, isStreaming: false, activeTasks: [], unparkPending: true })
		);
		await vi.advanceTimersByTimeAsync(PARK_MS);
		expect(sessionPoller.isPolling(SESSION_ID)).toBe(true); // bridged the gap

		// Still at the SLOW park rate: 9 × 150ms (1350ms < 1500ms) fires no poll.
		fetchMock.mockClear();
		await vi.advanceTimersByTimeAsync(9 * STREAMING_MS);
		expect(fetchMock.mock.calls.length).toBe(0);
		expect(sessionPoller.isPolling(SESSION_ID)).toBe(true);

		// 4) WAKE — the follow-up turn starts streaming (latch cleared). Upshift.
		fetchMock.mockClear();
		fetchMock.mockResolvedValue(makeLive({ lastUpdateId: 4, isStreaming: true }));
		await vi.advanceTimersByTimeAsync(PARK_MS);
		expect(sessionPoller.isPolling(SESSION_ID)).toBe(true);

		// Back at the FAST rate, and the follow-up finally goes fully idle → STOP.
		fetchMock.mockClear();
		fetchMock.mockResolvedValue(
			makeLive({ lastUpdateId: 5, isStreaming: false, activeTasks: [], unparkPending: false })
		);
		await vi.advanceTimersByTimeAsync(STREAMING_MS);
		expect(fetchMock.mock.calls.length).toBe(1); // fast-rate tick confirmed (we upshifted)
		expect(sessionPoller.isPolling(SESSION_ID)).toBe(false); // now genuinely idle
	});

	it('does NOT resurrect a stopped session when an in-flight poll resolves with parked tasks after stop()', async () => {
		// RESURRECTION GUARD: a park-rate interval poll can be in flight when
		// stop(sessionId) runs (e.g. the session was switched away / departed). If
		// that poll then resolves with (is_streaming=false, activeTasks>0), the park
		// branch used to re-arm the timer UNCONDITIONALLY — re-creating a park poller
		// for the just-departed session, which then polls it forever. The guard now
		// only re-arms when a timer still exists (or on loadSession), so the stray
		// post-stop poll no longer resurrects the session.

		// 1) Start streaming, settle to a park timer with an active task.
		fetchMock.mockResolvedValue(makeLive({ lastUpdateId: 1, isStreaming: true }));
		sessionPoller.startStreaming(SESSION_ID);
		await flush();
		fetchMock.mockResolvedValue(
			makeLive({ lastUpdateId: 2, isStreaming: false, activeTasks: [TASK] })
		);
		await vi.advanceTimersByTimeAsync(STREAMING_MS); // parks → park timer running
		expect(sessionPoller.isPolling(SESSION_ID)).toBe(true);

		// 2) Arm the NEXT park poll to hang (deferred), so it's in flight when we stop.
		let releasePoll!: (r: Response) => void;
		fetchMock.mockImplementationOnce(
			() => new Promise<Response>((resolve) => { releasePoll = (r) => resolve(r); })
		);
		await vi.advanceTimersByTimeAsync(PARK_MS); // fires the park tick → hangs in flight
		expect(releasePoll).toBeDefined();

		// 3) The session departs: stop() clears its timer while the poll is in flight.
		sessionPoller.stop(SESSION_ID);
		expect(sessionPoller.isPolling(SESSION_ID)).toBe(false);

		// 4) The stray in-flight poll resolves — still parked with an active task.
		releasePoll(makeLive({ lastUpdateId: 3, isStreaming: false, activeTasks: [TASK] }));
		await vi.advanceTimersByTimeAsync(0); // let the .then() chain run

		// The departed session must stay stopped — no resurrected park timer.
		expect(sessionPoller.isPolling(SESSION_ID)).toBe(false);

		// And it stays dead: advancing well past a park interval fires no new poll.
		fetchMock.mockClear();
		await vi.advanceTimersByTimeAsync(2 * PARK_MS);
		expect(fetchMock.mock.calls.length).toBe(0);
		expect(sessionPoller.isPolling(SESSION_ID)).toBe(false);
	});

	it('still starts a park timer from loadSession for a freshly-loaded parked session', async () => {
		// The guard must NOT break the loadSession park-start: a session loaded (via
		// onConnected) that is parked with active tasks has no timer yet but must
		// begin park-polling to detect the wake. fromLoadSession=true allows it.
		fetchMock.mockResolvedValueOnce(
			makeLive({ lastUpdateId: 1, isStreaming: false, activeTasks: [TASK] })
		);
		await sessionPoller.loadSession(SESSION_ID);
		expect(sessionPoller.isPolling(SESSION_ID)).toBe(true);
	});

	it('stops immediately on streaming→ended when there are no active tasks', async () => {
		fetchMock.mockResolvedValue(makeLive({ lastUpdateId: 1, isStreaming: true }));
		sessionPoller.startStreaming(SESSION_ID);
		await flush();
		expect(sessionPoller.isPolling(SESSION_ID)).toBe(true);

		// Next poll: not streaming, no tasks → must stop (classic end-of-turn path).
		fetchMock.mockResolvedValue(
			makeLive({ lastUpdateId: 2, isStreaming: false, activeTasks: [] })
		);
		await vi.advanceTimersByTimeAsync(STREAMING_MS);
		expect(sessionPoller.isPolling(SESSION_ID)).toBe(false);
	});

	it('tolerates a missing activeTasks field (treats as none → stops)', async () => {
		fetchMock.mockResolvedValue(makeLive({ lastUpdateId: 1, isStreaming: true }));
		sessionPoller.startStreaming(SESSION_ID);
		await flush();

		// Response omits activeTasks entirely (older backend). Must behave like [].
		fetchMock.mockResolvedValue({
			ok: true,
			status: 200,
			json: async () => ({
				last_update_id: 2,
				is_streaming: false,
				messages: [],
				initial_message_count: 0,
				usage: {},
				// no activeTasks field
			}),
		} as unknown as Response);
		await vi.advanceTimersByTimeAsync(STREAMING_MS);
		expect(sessionPoller.isPolling(SESSION_ID)).toBe(false);
	});
});
