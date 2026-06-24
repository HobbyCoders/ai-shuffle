/**
 * Tests for the session-poller INTERRUPT-transition lifecycle.
 *
 * Bug: on "interrupt and send" (interrupt_for_query), the old query drains and
 * `/live` reports `is_streaming=false` (no active tasks) in the window before the
 * replacement query starts streaming. The poll lifecycle used to STOP the timer
 * on that poll. If the new query's `start` then arrived while a stale poll was in
 * flight, `startStreaming` no-op'd (timer still present → generation not bumped)
 * and the late poll re-stopped the timer — leaving the poller dead so NO new
 * messages rendered until the user refreshed the page.
 *
 * Fix: while a session is mid-interrupt (activeSession's `interruptPending`,
 * surfaced to the poller via `setInterruptGuard`), an `is_streaming=false` poll
 * is treated as the drained OLD query — the poller keeps its streaming-rate timer
 * alive instead of stopping, so it catches the new stream the instant it begins.
 *
 * We drive the real poller with a scripted fetch mock + fake timers and toggle
 * the interrupt guard to assert the timer survives the drain window.
 */

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { sessionPoller } from './session-poller';
import { sessionStateManager } from './session-state.svelte';

const SESSION_ID = 'session-interrupt-poll';
const STREAMING_MS = 150;

type LiveOpts = {
	lastUpdateId: number;
	isStreaming: boolean;
	activeTasks?: Array<Record<string, unknown>>;
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
		}),
	} as unknown as Response;
}

describe('session-poller · interrupt transition', () => {
	let fetchMock: ReturnType<typeof vi.fn>;
	let interrupting: boolean;

	beforeEach(() => {
		vi.useFakeTimers();
		fetchMock = vi.fn();
		vi.stubGlobal('fetch', fetchMock);
		interrupting = false;
		// Honour the real contract: guard is per-session.
		sessionPoller.setInterruptGuard((sid) => interrupting && sid === SESSION_ID);
	});

	afterEach(() => {
		sessionPoller.setInterruptGuard(() => false); // reset singleton guard
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

	it('keeps polling across the drain window (is_streaming=false) while interrupting, then resumes normal stop after', async () => {
		// 1) STREAMING — startStreaming fires an immediate poll, then a fast timer.
		fetchMock.mockResolvedValue(makeLive({ lastUpdateId: 1, isStreaming: true }));
		sessionPoller.startStreaming(SESSION_ID);
		await flush();
		expect(sessionPoller.isPolling(SESSION_ID)).toBe(true);

		// 2) User hits "interrupt and send": the old query is draining. The next
		//    poll reports NOT streaming with NO tasks — but because we are mid-
		//    interrupt, the poller must KEEP its timer (this is the drained OLD
		//    query, not a real end-of-turn). Pre-fix this stopped the poller.
		interrupting = true;
		fetchMock.mockResolvedValue(makeLive({ lastUpdateId: 2, isStreaming: false, activeTasks: [] }));
		await vi.advanceTimersByTimeAsync(STREAMING_MS); // drain-window poll
		expect(sessionPoller.isPolling(SESSION_ID)).toBe(true); // timer survives — the fix

		// 3) Still at the FAST rate so we catch the new stream promptly: a single
		//    150ms tick fires exactly one poll (another drain-window poll).
		fetchMock.mockClear();
		await vi.advanceTimersByTimeAsync(STREAMING_MS);
		expect(fetchMock.mock.calls.length).toBe(1);
		expect(sessionPoller.isPolling(SESSION_ID)).toBe(true);

		// 4) Replacement query starts streaming. In the app the `start` handler
		//    clears interruptPending; mirror that, then a fresh poll reports
		//    streaming. The poller stays on the fast rate.
		interrupting = false;
		fetchMock.mockResolvedValue(makeLive({ lastUpdateId: 3, isStreaming: true }));
		await vi.advanceTimersByTimeAsync(STREAMING_MS);
		expect(sessionPoller.isPolling(SESSION_ID)).toBe(true);

		// 5) New turn genuinely ends (not interrupting, no tasks) → poller stops.
		fetchMock.mockResolvedValue(makeLive({ lastUpdateId: 4, isStreaming: false, activeTasks: [] }));
		await vi.advanceTimersByTimeAsync(STREAMING_MS);
		expect(sessionPoller.isPolling(SESSION_ID)).toBe(false);
	});

	it('still stops on a real end-of-turn when NOT interrupting (guard does not over-reach)', async () => {
		fetchMock.mockResolvedValue(makeLive({ lastUpdateId: 1, isStreaming: true }));
		sessionPoller.startStreaming(SESSION_ID);
		await flush();
		expect(sessionPoller.isPolling(SESSION_ID)).toBe(true);

		// interrupting stays false → an is_streaming=false / no-tasks poll must
		// stop the poller exactly as before (no regression to the normal path).
		fetchMock.mockResolvedValue(makeLive({ lastUpdateId: 2, isStreaming: false, activeTasks: [] }));
		await vi.advanceTimersByTimeAsync(STREAMING_MS);
		expect(sessionPoller.isPolling(SESSION_ID)).toBe(false);
	});
});
