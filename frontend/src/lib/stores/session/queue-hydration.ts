/**
 * queue-hydration — whether the pending bubbles on screen are confirmed by the
 * worker that executes the session (#524).
 *
 * Opening a session (or reconnecting) sends `watch_session`, and the worker
 * answers with one `sync/queue_updated` snapshot. Until it does, the bubbles
 * shown are the ones parked when the user left (pending-queue), which is the
 * optimistic view. The answer is one of:
 *
 * - `queue_status: "available"` (or a legacy frame with no status) — the queue
 *   as the executing worker holds it. It replaces the bubbles as before.
 * - `queue_status: "unavailable"` — the owner could not be read (offline peer,
 *   failed read, ownership moving). Both arrays are omitted. That is NOT an
 *   empty queue: the bubbles stay, marked stale, and `watch_session` is retried
 *   with backoff until an available snapshot arrives.
 *
 * A reply that never comes (dropped socket, older worker) is treated the same
 * way once HYDRATION_TIMEOUT_MS passes, but only while there are bubbles on
 * screen to vouch for. Reconnects re-send `watch_session` themselves, so a
 * retry is skipped while the socket is down.
 */

import type { ActiveSessionState } from '../active-session';

export interface QueueHydrationDeps {
	getState: () => ActiveSessionState;
	update: (fn: (s: ActiveSessionState) => ActiveSessionState) => void;
	wsSend: (data: Record<string, unknown>) => boolean;
	wsIsConnected: () => boolean;
}

/** How long a watch may go unanswered before its bubbles read as unconfirmed. */
export const HYDRATION_TIMEOUT_MS = 8_000;
/** Retry schedule for re-watching an unreadable queue; the last step repeats. */
export const RETRY_DELAYS_MS = [2_000, 4_000, 8_000, 16_000, 30_000] as const;

let deps: QueueHydrationDeps | null = null;
/** The session whose answer we are waiting for (or retrying). */
let sessionId: string | null = null;
let timeoutHandle: ReturnType<typeof setTimeout> | null = null;
let retryHandle: ReturnType<typeof setTimeout> | null = null;
let attempt = 0;

function clearTimers(): void {
	if (timeoutHandle !== null) clearTimeout(timeoutHandle);
	if (retryHandle !== null) clearTimeout(retryHandle);
	timeoutHandle = null;
	retryHandle = null;
}

function isCurrent(id: string): boolean {
	return !!deps && sessionId === id && deps.getState().sessionId === id;
}

function setStale(id: string, stale: boolean): void {
	if (!deps) return;
	if (deps.getState().sessionId !== id || !!deps.getState().queueStale === stale) return;
	deps.update(s => (s.sessionId === id ? { ...s, queueStale: stale } : s));
}

/** Nothing on screen depends on the answer: no stale chrome, no retries. */
function hasBubbles(): boolean {
	return (deps?.getState().queuedMessages.length ?? 0) > 0;
}

function scheduleRetry(id: string): void {
	if (retryHandle !== null) clearTimeout(retryHandle);
	const delay = RETRY_DELAYS_MS[Math.min(attempt, RETRY_DELAYS_MS.length - 1)];
	attempt++;
	retryHandle = setTimeout(() => {
		retryHandle = null;
		// Retried even with nothing on screen: an unreadable owner may be holding
		// rows this tab has never seen (a fresh device, a reload).
		if (!isCurrent(id)) return;
		// A reconnect re-watches on its own; it also re-arms this module then.
		if (!deps!.wsIsConnected()) return;
		if (deps!.wsSend({ type: 'watch_session', session_id: id })) armTimeout(id);
	}, delay);
}

function armTimeout(id: string): void {
	if (timeoutHandle !== null) clearTimeout(timeoutHandle);
	timeoutHandle = setTimeout(() => {
		timeoutHandle = null;
		if (!isCurrent(id) || !hasBubbles()) return;
		setStale(id, true);
		scheduleRetry(id);
	}, HYDRATION_TIMEOUT_MS);
}

export const queueHydration = {
	attach(d: QueueHydrationDeps): void {
		deps = d;
	},

	/** A `watch_session` for this session just went out; its snapshot is owed. */
	watching(id: string | null): void {
		if (id !== sessionId) {
			clearTimers();
			attempt = 0;
		}
		sessionId = id;
		if (!id) return;
		if (retryHandle !== null) {
			// A reconnect overtook a scheduled retry: this watch IS that retry.
			clearTimeout(retryHandle);
			retryHandle = null;
		}
		armTimeout(id);
	},

	/** A `queue_updated` for the active session arrived. */
	snapshot(id: string | null, status: 'available' | 'unavailable'): void {
		if (!id) return;
		if (sessionId !== id) {
			clearTimers();
			attempt = 0;
			sessionId = id;
		}
		if (timeoutHandle !== null) {
			clearTimeout(timeoutHandle);
			timeoutHandle = null;
		}
		if (status === 'available') {
			if (retryHandle !== null) clearTimeout(retryHandle);
			retryHandle = null;
			attempt = 0;
			setStale(id, false);
			return;
		}
		setStale(id, true);
		scheduleRetry(id);
	},

	/** Leaving the session (switch / new chat / teardown): drop owed answers. */
	cancel(): void {
		clearTimers();
		attempt = 0;
		sessionId = null;
	},

	/** Test hook. */
	reset(): void {
		clearTimers();
		attempt = 0;
		sessionId = null;
		deps = null;
	},
};
