/**
 * Tests for the session-poller SWITCH-BACK SAFETY park poll.
 *
 * Forensic incident 2026-07-09 (ses-6ed0038f173f froze 19:13-19:17 EDT while
 * the server streamed continuously): switching back to a session that is
 * MID-TURN can land the fresh loadSession() in the micro-gap BETWEEN a
 * queue/unpark-driven session's follow-up turns. `/live` then TRUTHFULLY reports
 * (is_streaming=false, activeTasks=[], unpark_pending=false) because nothing is
 * streaming that instant. A fresh load has NO timer, so the plain idle branch
 * would stop() a poller that never started — and the WS parked-watcher can't
 * rescue it (server-initiated follow-ups don't reliably re-broadcast
 * stream_start to a re-attached socket). The UI freezes until a manual refresh.
 *
 * The fix: a fresh load landing on a truthful-idle poll arms a BOUNDED park poll
 * (SWITCHBACK_PARK_POLL_MAX_CYCLES cycles at the park rate) instead of
 * concluding permanent idle. If any cycle sees streaming / tasks / unpark, the
 * normal lifecycle takes over; if all come back idle, it stops for real.
 *
 * We drive the real poller with a scripted fetch mock + fake timers.
 */

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { sessionPoller } from './session-poller';
import { sessionStateManager } from './session-state.svelte';

const SESSION_ID = 'session-switchback';
const STREAMING_MS = 150;
const PARK_MS = 1500;
const MAX_CYCLES = 8; // must match SWITCHBACK_PARK_POLL_MAX_CYCLES

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

describe('session-poller · switch-back safety park poll', () => {
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

	it('arms a bounded park poll on a fresh idle load, then upshifts to streaming when a later poll wakes', async () => {
		// Fresh load lands in the between-turns micro-gap: TRUTHFUL idle.
		fetchMock.mockResolvedValueOnce(
			makeLive({ lastUpdateId: 1, isStreaming: false, activeTasks: [], unparkPending: false })
		);
		await sessionPoller.loadSession(SESSION_ID);

		// FAILS on current code: the poller never starts (idle stop on a load with
		// no timer). With the fix it arms the bounded switch-back park poll.
		expect(sessionPoller.isPolling(SESSION_ID)).toBe(true);

		// It polls at the SLOW park rate: 9 × 150ms (1350ms < 1500ms) fires no poll.
		fetchMock.mockClear();
		fetchMock.mockResolvedValue(
			makeLive({ lastUpdateId: 1, isStreaming: false, activeTasks: [], unparkPending: false })
		);
		await vi.advanceTimersByTimeAsync(9 * STREAMING_MS);
		expect(fetchMock.mock.calls.length).toBe(0);
		expect(sessionPoller.isPolling(SESSION_ID)).toBe(true);

		// The follow-up turn finally starts streaming server-side. The next park
		// tick sees is_streaming=true → the poller upshifts to the streaming rate.
		fetchMock.mockClear();
		fetchMock.mockResolvedValue(makeLive({ lastUpdateId: 2, isStreaming: true }));
		await vi.advanceTimersByTimeAsync(PARK_MS); // park tick sees streaming → upshift
		expect(sessionPoller.isPolling(SESSION_ID)).toBe(true);

		// Confirm the FAST rate now: a single 150ms tick fires a poll.
		fetchMock.mockClear();
		await vi.advanceTimersByTimeAsync(STREAMING_MS);
		expect(fetchMock.mock.calls.length).toBe(1);
	});

	it('promotes to the park lifecycle when a bounded poll sees active tasks', async () => {
		fetchMock.mockResolvedValueOnce(
			makeLive({ lastUpdateId: 1, isStreaming: false, activeTasks: [], unparkPending: false })
		);
		await sessionPoller.loadSession(SESSION_ID);
		expect(sessionPoller.isPolling(SESSION_ID)).toBe(true);

		// A later poll surfaces a background task → normal park lifecycle takes over
		// and the poller keeps running well beyond the bounded budget.
		fetchMock.mockResolvedValue(
			makeLive({
				lastUpdateId: 2,
				isStreaming: false,
				activeTasks: [{ id: 't1', status: 'running', kind: 'shell', subject: 'x' }],
			})
		);
		await vi.advanceTimersByTimeAsync((MAX_CYCLES + 4) * PARK_MS);
		expect(sessionPoller.isPolling(SESSION_ID)).toBe(true);
	});

	it('stops for real when every bounded poll comes back idle (no timer leak)', async () => {
		fetchMock.mockResolvedValue(
			makeLive({ lastUpdateId: 1, isStreaming: false, activeTasks: [], unparkPending: false })
		);
		await sessionPoller.loadSession(SESSION_ID);
		expect(sessionPoller.isPolling(SESSION_ID)).toBe(true);

		// Spend the whole bounded budget with only-idle polls: it must fully stop.
		await vi.advanceTimersByTimeAsync(MAX_CYCLES * PARK_MS);
		expect(sessionPoller.isPolling(SESSION_ID)).toBe(false);

		// And it stays dead — no leaked timer keeps firing.
		fetchMock.mockClear();
		await vi.advanceTimersByTimeAsync(4 * PARK_MS);
		expect(fetchMock.mock.calls.length).toBe(0);
		expect(sessionPoller.isPolling(SESSION_ID)).toBe(false);
	});

	it('does not stack timers on repeated idle loads (budget resets, single timer)', async () => {
		fetchMock.mockResolvedValue(
			makeLive({ lastUpdateId: 1, isStreaming: false, activeTasks: [], unparkPending: false })
		);
		await sessionPoller.loadSession(SESSION_ID);
		await sessionPoller.loadSession(SESSION_ID);
		await sessionPoller.loadSession(SESSION_ID);
		expect(sessionPoller.isPolling(SESSION_ID)).toBe(true);

		// One park interval fires EXACTLY one poll — not one per stacked load.
		fetchMock.mockClear();
		await vi.advanceTimersByTimeAsync(PARK_MS);
		expect(fetchMock.mock.calls.length).toBe(1);
	});
});
