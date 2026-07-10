/**
 * Tests for the SessionNotifier's poller-restart policy.
 *
 * Regression: a server-initiated WAKE turn (unpark) whose `stream_start`
 * broadcast is lost — or which surfaces via a `message_added` / `system` /
 * `subagent_*` event rather than a `stream_chunk` — used to fall to a one-shot
 * `pollNow` when the poller was STOPPED. `pollNow` renders the delivered
 * messages but never ARMS a running poller, so the wake's `is_streaming=true`
 * flip was never observed: the streaming indicator + stop button never
 * appeared and the (actually streaming) session could not be interrupted.
 *
 * The notifier now restarts the poller (`startStreaming`) for ANY update event
 * when the session is not already polling, so it converges on `/live`'s
 * `is_streaming` regardless of the missed WS edge.
 */

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

// Mock the poller the notifier drives.
vi.mock('./session-poller', () => ({
	sessionPoller: {
		startStreaming: vi.fn(),
		pollNow: vi.fn(),
		isPolling: vi.fn(),
	},
}));

import { sessionNotifier } from './session-notifier';
import { sessionPoller } from './session-poller';

const SID = 'sess-wake';

describe('SessionNotifier · poller restart on server-initiated wake', () => {
	beforeEach(() => {
		vi.clearAllMocks();
	});
	afterEach(() => {
		vi.clearAllMocks();
	});

	// Every event a wake turn can surface with while the parked/idle poller is
	// stopped. NONE of these are the `stream_chunk` family the old code special-
	// cased, yet each must promote a stopped poller to a running one.
	const wakeUpdateEvents: Array<Record<string, unknown>> = [
		{ event_type: 'message_added', data: { message_id: 'm1' } },
		{ event_type: 'messages_updated' },
		{ event_type: 'streaming_update' },
		{ type: 'system' },
		{ event_type: 'subagent_text', data: { text: 'hi' } },
		{ event_type: 'subagent_done' },
		{ event_type: 'tool_use' },
	];

	it.each(wakeUpdateEvents)(
		'starts the poller (not just a one-shot pollNow) for %o when the poller is stopped',
		(evt) => {
			(sessionPoller.isPolling as ReturnType<typeof vi.fn>).mockReturnValue(false);

			sessionNotifier.handleMessage(SID, evt);

			expect(sessionPoller.startStreaming).toHaveBeenCalledWith(SID);
			// It must NOT settle for a timerless one-shot poll on a stopped poller.
			expect(sessionPoller.pollNow).not.toHaveBeenCalled();
		}
	);

	it('uses a one-shot pollNow when the poller is ALREADY running (no churn/reset)', () => {
		(sessionPoller.isPolling as ReturnType<typeof vi.fn>).mockReturnValue(true);

		sessionNotifier.handleMessage(SID, { event_type: 'message_added' });

		expect(sessionPoller.pollNow).toHaveBeenCalledWith(SID);
		expect(sessionPoller.startStreaming).not.toHaveBeenCalled();
	});

	it('still restarts on the stream_chunk family (unchanged behavior)', () => {
		(sessionPoller.isPolling as ReturnType<typeof vi.fn>).mockReturnValue(false);

		sessionNotifier.handleMessage(SID, { event_type: 'stream_chunk' });

		expect(sessionPoller.startStreaming).toHaveBeenCalledWith(SID);
		expect(sessionPoller.pollNow).not.toHaveBeenCalled();
	});

	it('start / stream_start still go straight to startStreaming', () => {
		(sessionPoller.isPolling as ReturnType<typeof vi.fn>).mockReturnValue(false);

		sessionNotifier.handleMessage(SID, { event_type: 'stream_start' });
		expect(sessionPoller.startStreaming).toHaveBeenCalledWith(SID);

		vi.clearAllMocks();
		(sessionPoller.isPolling as ReturnType<typeof vi.fn>).mockReturnValue(false);
		sessionNotifier.handleMessage(SID, { type: 'start' });
		expect(sessionPoller.startStreaming).toHaveBeenCalledWith(SID);
	});

	it('stream_end triggers a final poll, not a restart', () => {
		(sessionPoller.isPolling as ReturnType<typeof vi.fn>).mockReturnValue(false);

		sessionNotifier.handleMessage(SID, { event_type: 'stream_end' });

		expect(sessionPoller.pollNow).toHaveBeenCalledWith(SID);
		expect(sessionPoller.startStreaming).not.toHaveBeenCalled();
	});

	it('does not restart the poller for a non-update event (e.g. permission request)', () => {
		(sessionPoller.isPolling as ReturnType<typeof vi.fn>).mockReturnValue(false);

		sessionNotifier.handleMessage(SID, { type: 'permission_request', data: {} });

		expect(sessionPoller.startStreaming).not.toHaveBeenCalled();
		expect(sessionPoller.pollNow).not.toHaveBeenCalled();
	});
});
