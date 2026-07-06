/**
 * Rate-limit store — holds the most recent `rate_limit` WebSocket event plus a
 * persisted "last-known five-hour reading" so `UsageLimitIndicator.svelte` can
 * render a 5-hour usage pill (hidden until the first reading arrives).
 *
 * Slice C (SDK 0.1.65→0.1.81 upgrade). Plain `.ts` (no $state runes) — see
 * KNOWN_ISSUES.md Root Cause 10.
 *
 * Data-source reality: the CLI emits `rate_limit` events only when the
 * rate-limit *status transitions* (e.g. toward `allowed_warning`) — there is no
 * proactive/queryable "current 5h usage" feed (no SDK control request for it,
 * and the 5h window belongs to the CLI's OAuth subscription, not the backend's
 * API key). So we keep the MOST RECENT reported five-hour reading
 * ({@link RateLimitState.lastFiveHour}) and persist it to localStorage. The pill
 * stays hidden until the first reading ever arrives, then shows that last-known
 * value at all times, resetting to 0% once the window rolls over (`resets_at`
 * passes).
 *
 * Lifecycle:
 *   1. WS handler `case 'rate_limit'` calls {@link rateLimit.set}.
 *   2. A five-hour event updates `lastFiveHour` and is persisted.
 *   3. UsageLimitIndicator subscribes via `$rateLimit` and renders `lastFiveHour`.
 */

import { createStore } from './store-utils';

interface RateLimitEvent {
	status: string;
	rate_limit_type: string;
	resets_at?: string;
	utilization?: number;
	session_id?: string;
	/** Wall-clock when we received the event (ms since epoch). */
	received_at: number;
}

interface RateLimitState {
	event: RateLimitEvent | null;
	dismissedEventKey: string | null;
	/** Most recent five-hour-window reading, persisted across reloads so the
	 *  usage pill can stay visible between (sparse) rate-limit events. */
	lastFiveHour: RateLimitEvent | null;
}

/** Canonical SDK value for the 5-hour ("session") rate-limit window. */
const FIVE_HOUR_TYPE = 'five_hour';
const FIVE_HOUR_STORAGE_KEY = 'rate-limit-five-hour';

/** Parse `resets_at` (ISO string, epoch seconds, or epoch ms) into epoch ms. */
function _parseResetMs(resetsAt: string | number | undefined): number | null {
	if (resetsAt == null) return null;
	if (typeof resetsAt === 'number') {
		return resetsAt < 1e12 ? resetsAt * 1000 : resetsAt;
	}
	const trimmed = resetsAt.trim();
	if (trimmed === '') return null;
	if (/^\d+$/.test(trimmed)) {
		const n = Number(trimmed);
		return n < 1e12 ? n * 1000 : n;
	}
	const parsed = Date.parse(trimmed);
	return Number.isNaN(parsed) ? null : parsed;
}

function _loadLastFiveHour(): RateLimitEvent | null {
	try {
		const raw = localStorage.getItem(FIVE_HOUR_STORAGE_KEY);
		if (!raw) return null;
		const parsed = JSON.parse(raw);
		if (parsed && typeof parsed === 'object' && parsed.rate_limit_type === FIVE_HOUR_TYPE) {
			return parsed as RateLimitEvent;
		}
		return null;
	} catch {
		// localStorage unavailable (SSR/prerender) or malformed JSON.
		return null;
	}
}

function _saveLastFiveHour(e: RateLimitEvent | null) {
	try {
		if (e) localStorage.setItem(FIVE_HOUR_STORAGE_KEY, JSON.stringify(e));
		else localStorage.removeItem(FIVE_HOUR_STORAGE_KEY);
	} catch {
		// localStorage unavailable — persistence is best-effort.
	}
}

const _store = createStore<RateLimitState>({
	event: null,
	dismissedEventKey: null,
	lastFiveHour: _loadLastFiveHour(),
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

	createDerived: _store.createDerived,

	set(event: Omit<RateLimitEvent, 'received_at'>): void {
		const next: RateLimitEvent = { ...event, received_at: Date.now() };
		const isFiveHour = next.rate_limit_type === FIVE_HOUR_TYPE;
		// Persist the latest five-hour reading so the pill survives reloads and
		// stays populated between events.
		if (isFiveHour) _saveLastFiveHour(next);
		_store.update(s => ({
			...s,
			event: next,
			// Re-arm: a new event always re-shows even if a previous one was dismissed.
			dismissedEventKey: null,
			lastFiveHour: isFiveHour ? next : s.lastFiveHour,
		}));
	},

	clear(): void {
		// Clears only the transient event; the persisted five-hour reading stays.
		_store.update(s => ({ ...s, event: null, dismissedEventKey: null }));
	},

	dismiss(): void {
		const cur = _store.current.event;
		if (!cur) return;
		_store.update(s => ({ ...s, dismissedEventKey: eventKey(cur) }));
	},

	/** Tick helper — clears the transient `event` once its window resets.
	 *  Does NOT touch `lastFiveHour` (which intentionally persists). */
	gcExpired(now: number = Date.now()): boolean {
		const cur = _store.current.event;
		if (!cur?.resets_at) return false;
		const resetMs = _parseResetMs(cur.resets_at);
		if (resetMs == null) return false;
		if (resetMs <= now) {
			_store.update(s => ({ ...s, event: null, dismissedEventKey: null }));
			return true;
		}
		return false;
	},
};
