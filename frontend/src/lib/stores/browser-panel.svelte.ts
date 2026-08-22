/**
 * Browser Panel Store — state for the live browser screencast side panel.
 *
 * Mirrors the layout store pattern (Svelte 5 runes + the createSubscribers shim
 * for `$storeName` auto-subscription in .svelte files). Only the panel WIDTH is
 * persisted to localStorage; `open`/`status`/`url` are runtime-only, and
 * `userClosedForSession` is a per-runtime suppression flag so an auto-open never
 * fights a user who deliberately closed the panel for the session they're in.
 */
import { browser } from '$app/environment';
import { untrack } from 'svelte';
import { createSubscribers } from './store-utils';

// ============================================
// Types
// ============================================

/** Server-reported stream state (see the browser WS protocol contract). */
export type BrowserStatus =
	| 'no_page'
	| 'connecting'
	| 'live'
	| 'disconnected'
	| 'browser_off';

interface BrowserPanelState {
	/** Whether the panel is docked open. */
	open: boolean;
	/** Panel width in px (user-resizable, persisted, clamped). */
	width: number;
	/** Latest stream status. */
	status: BrowserStatus;
	/** Current page URL, or null when unknown. */
	url: string | null;
	/**
	 * The session id the user last CLOSED the panel for. While set, `autoOpenFor`
	 * that same session is suppressed so a later browser tool doesn't reopen a
	 * panel the user deliberately dismissed. Cleared whenever the user reopens.
	 */
	userClosedForSession: string | null;
}

// ============================================
// Constants
// ============================================

const STORAGE_KEY = 'ai-shuffle-browser-panel';
const DEFAULT_WIDTH = 480;
const MIN_WIDTH = 320;
const MAX_WIDTH = 960;

// ============================================
// Persistence (width only)
// ============================================

function clampWidth(width: number): number {
	if (!Number.isFinite(width)) return DEFAULT_WIDTH;
	return Math.max(MIN_WIDTH, Math.min(MAX_WIDTH, Math.round(width)));
}

function loadPersistedWidth(): number {
	if (!browser) return DEFAULT_WIDTH;
	try {
		const raw = localStorage.getItem(STORAGE_KEY);
		if (!raw) return DEFAULT_WIDTH;
		const parsed = JSON.parse(raw) as { width?: number };
		return typeof parsed.width === 'number' ? clampWidth(parsed.width) : DEFAULT_WIDTH;
	} catch {
		return DEFAULT_WIDTH;
	}
}

function writeWidth(width: number) {
	if (!browser) return;
	try {
		localStorage.setItem(STORAGE_KEY, JSON.stringify({ width }));
	} catch {
		// Ignore storage errors
	}
}

/**
 * Debounced width persistence. A drag-resize fires `setWidth` on every
 * pointermove; a synchronous localStorage write per move stalls the main thread
 * and makes the live canvas stutter. The in-memory width still updates on every
 * move — only the disk write waits for the drag to settle.
 */
const PERSIST_DEBOUNCE_MS = 150;
let persistTimer: ReturnType<typeof setTimeout> | null = null;
let pendingWidth: number | null = null;

function persistWidth(width: number) {
	if (!browser) return;
	pendingWidth = width;
	if (persistTimer !== null) return;
	persistTimer = setTimeout(() => {
		persistTimer = null;
		if (pendingWidth !== null) {
			writeWidth(pendingWidth);
			pendingWidth = null;
		}
	}, PERSIST_DEBOUNCE_MS);
}

/** Flush any pending width write immediately (tests / teardown). */
export function flushWidthPersist() {
	if (persistTimer !== null) {
		clearTimeout(persistTimer);
		persistTimer = null;
	}
	if (pendingWidth !== null) {
		writeWidth(pendingWidth);
		pendingWidth = null;
	}
}

// A resize finished right before a navigation must not be lost to the debounce.
if (browser && typeof window !== 'undefined') {
	window.addEventListener('pagehide', flushWidthPersist);
}

// ============================================
// Reactive State
// ============================================

let _state = $state<BrowserPanelState>({
	open: false,
	width: loadPersistedWidth(),
	status: 'disconnected',
	url: null,
	userClosedForSession: null,
});

// ============================================
// Subscribe shim (centralized via store-utils)
// ============================================

const { subscribe, notifySubscribers, createDerived } = createSubscribers<BrowserPanelState>(
	() => _state,
	(v) => {
		_state = v;
	},
);

function _update(fn: (s: BrowserPanelState) => BrowserPanelState) {
	_state = fn(untrack(() => _state));
	notifySubscribers();
}

// ============================================
// Exported Store
// ============================================

export const browserPanel = {
	get current() {
		return _state;
	},

	subscribe,

	/** Open the panel and lift any close-suppression (the user wants it visible). */
	openPanel() {
		_update((s) => ({ ...s, open: true, userClosedForSession: null }));
	},

	/**
	 * Close the panel and remember which session it was closed for, so a later
	 * browser tool in that same session won't auto-reopen it.
	 */
	closePanel(sessionId: string | null) {
		_update((s) => ({ ...s, open: false, userClosedForSession: sessionId }));
	},

	/** Toggle open/closed. Closing records the suppression for `sessionId`. */
	toggle(sessionId: string | null) {
		_update((s) =>
			s.open
				? { ...s, open: false, userClosedForSession: sessionId }
				: { ...s, open: true, userClosedForSession: null },
		);
	},

	/** Set the panel width (clamped 320–960) and persist it. */
	setWidth(width: number) {
		const clamped = clampWidth(width);
		_update((s) => ({ ...s, width: clamped }));
		persistWidth(clamped);
	},

	/** Update the stream status (and optionally the current URL). */
	setStatus(status: BrowserStatus, url?: string | null) {
		_update((s) => ({
			...s,
			status,
			url: url === undefined ? s.url : url,
		}));
	},

	/**
	 * Auto-open for a session (called when the agent first drives a browser tool).
	 * No-op if the user closed the panel for this exact session, or if it's
	 * already open.
	 */
	autoOpenFor(sessionId: string | null) {
		_update((s) => {
			if (s.open) return s;
			if (sessionId !== null && s.userClosedForSession === sessionId) return s;
			return { ...s, open: true };
		});
	},

	/** Current state snapshot. */
	getState(): BrowserPanelState {
		return _state;
	},
};

// ============================================
// Derived stores (via store-utils createDerived)
// ============================================

export const browserPanelOpen = createDerived(() => _state.open);
export const browserPanelStatus = createDerived(() => _state.status);
