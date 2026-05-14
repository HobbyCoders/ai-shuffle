/**
 * Rate-limit store — holds the most recent `rate_limit` WebSocket event so
 * `RateLimitBanner.svelte` can render reactively.
 *
 * Slice C (SDK 0.1.65→0.1.81 upgrade). Plain `.ts` (no $state runes) — see
 * KNOWN_ISSUES.md Root Cause 10.
 *
 * Lifecycle:
 *   1. WS handler `case 'rate_limit'` calls {@link rateLimit.set}.
 *   2. The banner subscribes via `$rateLimit` and renders when `event` is set.
 *   3. The banner ticks a countdown each second; once `resets_at` is in the
 *      past, the store auto-clears (via {@link rateLimit.gcExpired}).
 *   4. Dismiss is per-event — re-arms automatically on the next event.
 */

import { createStore } from './store-utils';

export interface RateLimitEvent {
	status: string;
	rate_limit_type: string;
	resets_at?: string;
	utilization?: number;
	session_id?: string;
	/** Wall-clock when we received the event (ms since epoch). */
	received_at: number;
}

export interface RateLimitState {
	event: RateLimitEvent | null;
	dismissedEventKey: string | null;
}

const _store = createStore<RateLimitState>({
	event: null,
	dismissedEventKey: null,
});

/** Build a stable key per distinct event so dismiss survives countdown ticks. */
function eventKey(e: RateLimitEvent): string {
	return `${e.rate_limit_type}|${e.resets_at ?? ''}|${e.received_at}`;
}

export const rateLimit = {
	get current(): RateLimitState {
		return _store.current;
	},

	subscribe: _store.subscribe,

	/** Latest event the user has NOT dismissed and that hasn't expired. */
	createDerived: _store.createDerived,

	set(event: Omit<RateLimitEvent, 'received_at'>): void {
		const next: RateLimitEvent = { ...event, received_at: Date.now() };
		_store.update(s => ({
			event: next,
			// Re-arm: a new event always re-shows even if a previous one was dismissed.
			dismissedEventKey: null,
		}));
	},

	clear(): void {
		_store.update(() => ({ event: null, dismissedEventKey: null }));
	},

	dismiss(): void {
		const cur = _store.current.event;
		if (!cur) return;
		_store.update(s => ({ ...s, dismissedEventKey: eventKey(cur) }));
	},

	/** Called by the banner's tick loop. Returns true if the event has expired. */
	gcExpired(now: number = Date.now()): boolean {
		const cur = _store.current.event;
		if (!cur?.resets_at) return false;
		const resetMs = Date.parse(cur.resets_at);
		if (Number.isNaN(resetMs)) return false;
		if (resetMs <= now) {
			_store.update(() => ({ event: null, dismissedEventKey: null }));
			return true;
		}
		return false;
	},
};

/** Reactive helper — true when there's an active, non-dismissed event. */
export const rateLimitVisible = _store.createDerived(s =>
	Boolean(s.event && (!s.dismissedEventKey || s.dismissedEventKey !== eventKey(s.event)))
);
