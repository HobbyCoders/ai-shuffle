/**
 * End-to-end poller convergence for a SERVER-INITIATED WAKE, driven the way the
 * SessionNotifier now drives it: `startStreaming()` on any update event for a
 * stopped poller (previously a one-shot `pollNow`).
 *
 * Reproduces the reported symptom's core: an unpark whose content lands while
 * the live store is momentarily in the between-turns gap
 * (is_streaming=false, unpark_pending=true). The restarted poller must NOT stop
 * on that first idle-looking poll — it must park-poll and then upshift +
 * publish `isStreaming=true` (which drives the streaming indicator + stop
 * button) once the wake turn actually flips streaming. Before the notifier fix
 * a stopped poller would never have been armed at all, so this convergence
 * could not happen from a lost `stream_start`.
 */

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { sessionPoller } from './session-poller';
import { sessionStateManager } from './session-state.svelte';

const SESSION_ID = 'session-wake-restart';
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
				{ id: 'a1', role: 'assistant', type: 'text', content: 'waking', streaming: opts.isStreaming },
			],
			initial_message_count: 0,
			usage: {},
			activeTasks: opts.activeTasks ?? [],
			unpark_pending: opts.unparkPending ?? false,
		}),
	} as unknown as Response;
}

describe('session-poller · wake restart convergence', () => {
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

	it('restarted poller survives the between-turns gap and converges to streaming', async () => {
		// The wake update event arrives with the store in the unpark hand-off gap:
		// is_streaming=false but unpark_pending=true. startStreaming's immediate
		// poll must land here and keep the poller alive (park), NOT stop it.
		fetchMock.mockResolvedValueOnce(
			makeLive({ lastUpdateId: 1, isStreaming: false, unparkPending: true })
		);

		// This is exactly what the notifier now calls on an update event for a
		// stopped poller (was pollNow — a timerless one-shot that would render the
		// message and then never observe the streaming flip).
		sessionPoller.startStreaming(SESSION_ID);
		await vi.advanceTimersByTimeAsync(0); // flush the immediate poll

		expect(sessionPoller.isPolling(SESSION_ID)).toBe(true);
		expect(sessionStateManager.getSession(SESSION_ID)?.isStreaming).toBe(false);

		// The wake turn now actually streams. The next tick observes it and the
		// poller publishes isStreaming=true — the indicator + stop button engage.
		fetchMock.mockResolvedValue(makeLive({ lastUpdateId: 2, isStreaming: true }));
		await vi.advanceTimersByTimeAsync(PARK_MS);

		expect(sessionPoller.isPolling(SESSION_ID)).toBe(true);
		expect(sessionStateManager.getSession(SESSION_ID)?.isStreaming).toBe(true);

		// Confirm it upshifted to the fast streaming rate.
		fetchMock.mockClear();
		await vi.advanceTimersByTimeAsync(STREAMING_MS);
		expect(fetchMock.mock.calls.length).toBe(1);
	});

	it('restarted poller stops itself when the poll turns out fully idle', async () => {
		// If the update event was spurious / the wake already finished, the restart
		// must self-limit: a truthfully-idle poll (no streaming, no tasks, no
		// unpark) stops the poller rather than park-polling forever.
		fetchMock.mockResolvedValue(
			makeLive({ lastUpdateId: 1, isStreaming: false, activeTasks: [], unparkPending: false })
		);

		sessionPoller.startStreaming(SESSION_ID);
		await vi.advanceTimersByTimeAsync(0);

		// One streaming tick lands the idle poll → the idle branch stops the poller.
		await vi.advanceTimersByTimeAsync(STREAMING_MS);
		expect(sessionPoller.isPolling(SESSION_ID)).toBe(false);
	});
});
