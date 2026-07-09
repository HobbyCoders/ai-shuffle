/**
 * Usage-limits store — a per-window map of the user's Claude subscription usage,
 * fed by BOTH:
 *   1. A polled snapshot (`usage_snapshot` WS event + the REST `/usage/limits`
 *      endpoint on page load) — the authoritative full set of windows, from the
 *      backend usage poller. Utilization is already 0–100 percent.
 *   2. Live SDK `rate_limit` events (WS `case 'rate_limit'`) — refine a SINGLE
 *      window as its status changes. SDK utilization is a 0.0–1.0 fraction, so
 *      it is normalised to 0–100 on ingest.
 *
 * Both key on the window-type string (`five_hour`, `seven_day`,
 * `seven_day_opus`, `seven_day_sonnet`, `seven_day_overage_included`,
 * `overage`) so they update the same slot. The map is persisted to localStorage
 * so the header pill/popover stay populated across reloads.
 *
 * Plain `.ts` (no $state runes) — see KNOWN_ISSUES.md Root Cause 10.
 *
 * Consumers: `UsageLimitIndicator.svelte` (header pill + popover) reads
 * `$rateLimit.windows`; `ws-message-handlers.ts` calls `applyLiveEvent` /
 * `applySnapshot`.
 */

import { createStore } from './store-utils';

/** A single rate-limit window, utilization normalised to 0–100 percent. */
export interface UsageWindow {
	type: string;
	/** 0–100 percent. */
	utilization: number;
	resets_at: string | null;
	/** Wall-clock (ms since epoch) when we last updated this window. */
	received_at: number;
	source: 'poll' | 'live';
}

interface RateLimitState {
	/** Keyed by window type. */
	windows: Record<string, UsageWindow>;
	/** Whether the backend currently has a usable Claude credential. */
	available: boolean;
	/**
	 * True between an account switch and the arrival of the fresh snapshot for
	 * the new account. The indicator dims itself so we never present the OLD
	 * account's numbers as if they were current. Runtime-only (not persisted).
	 */
	stale: boolean;
}

/** Live SDK `rate_limit` event payload (utilization is a 0–1 fraction). */
export interface LiveRateLimitEvent {
	status?: string;
	rate_limit_type: string;
	resets_at?: string | null;
	utilization?: number | null;
	session_id?: string;
}

/** One window from a polled snapshot (utilization already 0–100). */
export interface SnapshotWindow {
	type: string;
	utilization: number;
	resets_at?: string | null;
}

export interface UsageSnapshot {
	windows: SnapshotWindow[];
	fetched_at?: number | null;
	available: boolean;
	/**
	 * Monotonic generation tag from the backend. Bumped on every account switch.
	 * Lets us drop a snapshot from an older generation that arrives out of order
	 * after a newer one (e.g. a slow in-flight poll completing post-switch).
	 */
	generation?: number | null;
}

const STORAGE_KEY = 'usage-limit-windows';

// Human labels per known window type. Unknown types get a humanized fallback.
export const WINDOW_LABELS: Record<string, string> = {
	five_hour: '5-hour limit',
	seven_day: 'Weekly (all models)',
	seven_day_opus: 'Weekly Opus',
	seven_day_sonnet: 'Weekly Sonnet',
	seven_day_overage_included: 'Fable 5 limit',
	overage: 'Overage',
};

// Display order for the popover / headline selection tie-breaks.
export const WINDOW_ORDER = [
	'five_hour',
	'seven_day',
	'seven_day_opus',
	'seven_day_sonnet',
	'seven_day_overage_included',
	'overage',
];

export function windowLabel(type: string): string {
	return WINDOW_LABELS[type] ?? type.replace(/_/g, ' ');
}

/** Clamp an already-percent (0–100) value into range. */
function _clampPct(u: number): number {
	if (typeof u !== 'number' || Number.isNaN(u) || !Number.isFinite(u)) return 0;
	return Math.min(100, Math.max(0, u));
}

/** Normalise an SDK fraction (0–1, defensively 0–100) to a 0–100 percent. */
function _fractionToPct(u: number): number {
	return _clampPct(u <= 1 ? u * 100 : u);
}

/** Parse `resets_at` (ISO string, epoch seconds, or epoch ms) into epoch ms. */
export function parseResetMs(resetsAt: string | number | null | undefined): number | null {
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

/** Effective utilization for a window, accounting for a rolled-over window. */
export function effectivePct(w: UsageWindow, nowMs: number): number {
	const resetMs = parseResetMs(w.resets_at);
	if (resetMs != null && resetMs <= nowMs) return 0; // window rolled over
	return Math.round(w.utilization);
}

/** Format a countdown like "3h 12m" / "12m 4s" / "now". */
export function formatCountdown(resetsAt: string | null | undefined, nowMs: number): string {
	const target = parseResetMs(resetsAt);
	if (target == null) return '';
	const diffMs = target - nowMs;
	if (diffMs <= 0) return 'now';
	const totalSec = Math.ceil(diffMs / 1000);
	const d = Math.floor(totalSec / 86400);
	const h = Math.floor((totalSec % 86400) / 3600);
	const m = Math.floor((totalSec % 3600) / 60);
	const s = totalSec % 60;
	if (d > 0) return `${d}d ${h}h`;
	if (h > 0) return `${h}h ${m}m`;
	if (m > 0) return `${m}m ${s}s`;
	return `${s}s`;
}

/** Windows as an array, ordered by WINDOW_ORDER then any extras alphabetically. */
export function orderedWindows(windows: Record<string, UsageWindow>): UsageWindow[] {
	const known = WINDOW_ORDER.filter((t) => t in windows).map((t) => windows[t]);
	const extra = Object.keys(windows)
		.filter((t) => !WINDOW_ORDER.includes(t))
		.sort()
		.map((t) => windows[t]);
	return [...known, ...extra];
}

function _loadWindows(): Record<string, UsageWindow> {
	try {
		const raw = localStorage.getItem(STORAGE_KEY);
		if (!raw) return {};
		const parsed = JSON.parse(raw);
		if (parsed && typeof parsed === 'object') {
			return parsed as Record<string, UsageWindow>;
		}
		return {};
	} catch {
		return {}; // localStorage unavailable (SSR/prerender) or malformed JSON.
	}
}

function _saveWindows(windows: Record<string, UsageWindow>) {
	try {
		localStorage.setItem(STORAGE_KEY, JSON.stringify(windows));
	} catch {
		// best-effort persistence
	}
}

const _store = createStore<RateLimitState>({
	windows: _loadWindows(),
	available: false,
	stale: false,
});

/**
 * Highest snapshot generation seen this session. In-memory (NOT persisted) so a
 * page reload — or a backend restart, which resets its counter to 0 — starts
 * fresh and never permanently rejects post-restart snapshots. A snapshot with a
 * strictly-lower generation is a stale straggler and is discarded.
 */
let _lastGeneration = -1;

export const rateLimit = {
	get current(): RateLimitState {
		return _store.current;
	},

	subscribe: _store.subscribe,

	createDerived: _store.createDerived,

	/** Apply a live SDK `rate_limit` event — refines a single window. */
	applyLiveEvent(event: LiveRateLimitEvent): void {
		const { rate_limit_type, utilization } = event;
		// A data-less event (utilization null) must NOT create/wipe a window —
		// the CLI emits these before any usage is recorded.
		if (!rate_limit_type || utilization == null || typeof utilization !== 'number') return;
		const win: UsageWindow = {
			type: rate_limit_type,
			utilization: _fractionToPct(utilization),
			resets_at: event.resets_at ?? null,
			received_at: Date.now(),
			source: 'live',
		};
		_store.update((s) => {
			const windows = { ...s.windows, [rate_limit_type]: win };
			_saveWindows(windows);
			return { ...s, windows, available: true };
		});
	},

	/**
	 * Mark the indicator stale — call at the start of an account switch so it
	 * dims until the fresh snapshot for the new account arrives.
	 */
	markStale(): void {
		_store.update((s) => ({ ...s, stale: true }));
	},

	/** Apply a polled snapshot (WS `usage_snapshot` or REST `/usage/limits`). */
	applySnapshot(snapshot: UsageSnapshot): void {
		const gen = snapshot.generation;
		if (gen == null) {
			// No generation tag (legacy backend / tests). Treat as authoritative
			// and reset the guard so it can't wedge on a stale high-water mark.
			_lastGeneration = -1;
		} else {
			// Discard a straggler from an older generation (out-of-order arrival).
			if (gen < _lastGeneration) return;
			_lastGeneration = gen;
		}

		// When the backend has no usable credential it reports available=false
		// with no windows — keep the last-known windows rather than flashing the
		// pill away, but reflect availability. A fresh snapshot resolves any
		// pending switch, so clear the stale flag either way.
		if (!snapshot.available) {
			_store.update((s) => ({ ...s, available: false, stale: false }));
			return;
		}
		const now = Date.now();
		const windows: Record<string, UsageWindow> = {};
		for (const w of snapshot.windows ?? []) {
			if (!w?.type || typeof w.utilization !== 'number') continue;
			windows[w.type] = {
				type: w.type,
				utilization: _clampPct(w.utilization),
				resets_at: w.resets_at ?? null,
				received_at: now,
				source: 'poll',
			};
		}
		_saveWindows(windows);
		_store.update(() => ({ windows, available: true, stale: false }));
	},
};
