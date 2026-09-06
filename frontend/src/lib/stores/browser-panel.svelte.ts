/**
 * Browser Panel Store — state for the live browser screencast side panel.
 *
 * Mirrors the layout store pattern (Svelte 5 runes + the createSubscribers shim
 * for `$storeName` auto-subscription in .svelte files). Width, auto-open policy,
 * and viewport defaults sync through the browser_panel server preference.
 * Visibility, expansion, dismissal and page state live only in a session-keyed
 * runtime map. Background activity cannot change another session's panel.
 */
import { compatible } from '$lib/services/preference-values';
import type { BrowserViewport } from '$lib/browser-panel/workspace';
import { validViewport } from '$lib/browser-panel/workspace';
import { preferenceSync } from '$lib/services/preferences';
import { browser } from '$app/environment';
import { untrack } from 'svelte';
import { createSubscribers } from './store-utils';
import type { SelectedElement } from '$lib/browser-panel/inspect';

// ============================================
// Types
// ============================================

/** Server-reported stream state (see the browser WS protocol contract). */
export type BrowserStatus =
	'no_page' | 'connecting' | 'live' | 'disconnected' | 'browser_off';

interface BrowserPanelState {
	autoOpen: boolean;
	defaultViewport: BrowserViewport;
	/** Whether the panel is docked open. */
	open: boolean;
	expanded: boolean;
	/** Panel width in px (user-resizable, persisted, clamped). */
	width: number;
	/** Latest stream status. */
	status: BrowserStatus;
	/**
	 * Current page URL. Two sources feed it: the coarse `status` frames from the
	 * hub (registry metadata) and the finer `page` frames tracked from CDP page
	 * events — `page` wins once it has reported at least once.
	 */
	url: string | null;
	/** Whether the streamed page is mid-navigation (drives reload→stop). */
	pageLoading: boolean;
	/** True once a `page` frame has been seen this attachment. */
	pageTracked: boolean;
	/**
	 * The session id the user last CLOSED the panel for. While set, `autoOpenFor`
	 * that same session is suppressed so a later browser tool doesn't reopen a
	 * panel the user deliberately dismissed. Cleared whenever the user reopens.
	 */
	userClosedForSession: string | null;
	/** Point-and-prompt mode: clicks in the live view select elements instead
	 *  of being forwarded to the page. Runtime-only; off on open. */
	inspectMode: boolean;
	/** Element picked in inspect mode, feeding the panel's prompt composer. */
	selectedElement: SelectedElement | null;
}

// ============================================
// Constants
// ============================================

const STORAGE_KEY = 'ai-shuffle-browser-panel';
const DEFAULT_WIDTH = 480;
const MIN_WIDTH = 320;
const defaults = {
	width: DEFAULT_WIDTH,
	autoOpen: true,
	defaultViewport: { mode: 'fixed' as const, width: 1280, height: 800 },
};
let preference: Record<string, unknown> = { ...defaults };

// ============================================
// Persistence (width only)
// ============================================

function clampWidth(width: number): number {
	if (!Number.isFinite(width)) return DEFAULT_WIDTH;
	// No hardcoded max — the panel may fill the chat inner area up to the
	// sidebar. Layout (CSS max-width: 100% + drag clamp to the parent) is
	// the ceiling.
	return Math.max(MIN_WIDTH, Math.round(width));
}

function loadPersistedWidth(): number {
	if (!browser) return DEFAULT_WIDTH;
	try {
		const raw = localStorage.getItem(STORAGE_KEY);
		if (!raw) return DEFAULT_WIDTH;
		const parsed = JSON.parse(raw) as { width?: number };
		return typeof parsed.width === 'number'
			? clampWidth(parsed.width)
			: DEFAULT_WIDTH;
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
	preference = {
		...preference,
		width,
		autoOpen: _state.autoOpen,
		defaultViewport: _state.defaultViewport,
	};
	preferenceSync.save('browser_panel', preference);
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

preferenceSync.register('browser_panel', {
	read: () => ({
		...preference,
		width: _state.width,
		autoOpen: _state.autoOpen,
		defaultViewport: _state.defaultViewport,
	}),
	apply(value) {
		if (persistTimer !== null) clearTimeout(persistTimer);
		persistTimer = null;
		pendingWidth = null;
		const width =
			value &&
			typeof value === 'object' &&
			'width' in value &&
			typeof value.width === 'number'
				? clampWidth(value.width)
				: DEFAULT_WIDTH;
		const parsed = compatible(defaults, value);
		preference = { ...parsed };
		const defaultViewport = validViewport(parsed.defaultViewport)
			? parsed.defaultViewport
			: defaults.defaultViewport;
		_update((state) => ({
			...state,
			width,
			autoOpen: parsed.autoOpen,
			defaultViewport,
		}));
		writeWidth(width);
	},
});

// A resize finished right before a navigation must not be lost to the debounce.
if (browser && typeof window !== 'undefined') {
	window.addEventListener('pagehide', flushWidthPersist);
}

// ============================================
// Reactive State
// ============================================

let _state = $state<BrowserPanelState>({
	open: false,
	expanded: false,
	autoOpen: true,
	defaultViewport: { ...defaults.defaultViewport },
	width: loadPersistedWidth(),
	status: 'disconnected',
	url: null,
	pageLoading: false,
	pageTracked: false,
	userClosedForSession: null,
	inspectMode: false,
	selectedElement: null,
});

// ============================================
// Subscribe shim (centralized via store-utils)
// ============================================

const { subscribe, notifySubscribers } =
	createSubscribers<BrowserPanelState>(
		() => _state,
		(v) => {
			_state = v;
		},
	);

function _update(fn: (s: BrowserPanelState) => BrowserPanelState) {
	_state = fn(untrack(() => _state));
	notifySubscribers();
}

// Runtime state never enters preferenceSync or localStorage.
type RuntimeState = Pick<BrowserPanelState, 'open' | 'expanded' | 'status' | 'url' | 'pageLoading' | 'pageTracked' | 'userClosedForSession' | 'inspectMode' | 'selectedElement'>;
let runtime = $state<Record<string, RuntimeState>>({});
function updateSession(sessionId: string | null, fn: (s: BrowserPanelState) => BrowserPanelState) {
	if (!sessionId) return;
	const next = fn(untrack(() => browserPanel.forSession(sessionId)));
	const { open, expanded, status, url, pageLoading, pageTracked, userClosedForSession, inspectMode, selectedElement } = next;
	runtime[sessionId] = { open, expanded, status, url, pageLoading, pageTracked, userClosedForSession, inspectMode, selectedElement };
	notifySubscribers();
}

// ============================================
// Exported Store
// ============================================

export const browserPanel = {
	/** Shared preferences only; runtime state requires an explicit session. */
	get current(): Pick<BrowserPanelState, 'width' | 'autoOpen' | 'defaultViewport'> {
		return _state;
	},

	subscribe,
	setAutoOpen(autoOpen: boolean) {
		_update((s) => ({ ...s, autoOpen }));
		persistWidth(_state.width);
	},
	setDefaultViewport(viewport: BrowserViewport) {
		if (!validViewport(viewport)) return;
		_update((s) => ({
			...s,
			defaultViewport: { ...s.defaultViewport, ...viewport },
		}));
		persistWidth(_state.width);
	},

	forSession(sessionId: string | null): BrowserPanelState {
		return { ..._state, ...(sessionId ? runtime[sessionId] : undefined) };
	},
	openPanel(sessionId: string | null) {
		updateSession(sessionId, s => ({ ...s, open: true, userClosedForSession: null }));
	},
	closePanel(sessionId: string | null) {
		updateSession(sessionId, s => ({ ...s, open: false, expanded: false,
			userClosedForSession: sessionId, inspectMode: false, selectedElement: null }));
	},
	toggle(sessionId: string | null) {
		if (this.forSession(sessionId).open) this.closePanel(sessionId);
		else this.openPanel(sessionId);
	},
	setExpanded(sessionId: string, expanded: boolean) {
		updateSession(sessionId, s => ({ ...s, expanded }));
	},
	setInspectMode(on: boolean, sessionId: string) {
		updateSession(sessionId, s => ({ ...s, inspectMode: on, selectedElement: on ? s.selectedElement : null }));
	},
	setSelectedElement(element: SelectedElement | null, sessionId: string) {
		updateSession(sessionId, s => ({ ...s, selectedElement: element }));
	},

	/** Set the panel width (clamped to a 320px minimum) and persist it. */
	setWidth(width: number) {
		const clamped = clampWidth(width);
		_update((s) => ({ ...s, width: clamped }));
		persistWidth(clamped);
	},

	/** Update the stream status (and optionally the current URL). */
	setStatus(status: BrowserStatus, url: string | null | undefined, sessionId: string) {
		updateSession(sessionId, (s) => ({
			...s,
			status,
			// Registry-metadata URLs only fill the gap before CDP tracking kicks
			// in; once live page events have spoken they are authoritative.
			url: s.pageTracked ? s.url : url === undefined ? s.url : url,
		}));
	},

	/** Toolbar page state from a `page` frame (CDP-tracked url + loading). */
	setPage(url: string | null, loading: boolean, sessionId: string) {
		updateSession(sessionId, (s) => ({
			...s,
			url,
			pageLoading: loading,
			pageTracked: true,
		}));
	},

	/** Reset per-attachment page tracking (session switch / stream teardown).
	 *  A selection from the previous session's page is meaningless — drop it. */
	resetPageTracking(sessionId: string) {
		updateSession(sessionId, (s) => ({
			...s,
			url: null,
			inspectMode: false,
			pageLoading: false,
			pageTracked: false,
			selectedElement: null,
		}));
	},

	/**
	 * Auto-open for a session (called when the agent first drives a browser tool).
	 * No-op if the user closed the panel for this exact session, or if it's
	 * already open.
	 */
	autoOpenFor(sessionId: string | null) {
		updateSession(sessionId, (s) => {
			if (s.open || !s.autoOpen) return s;
			if (sessionId !== null && s.userClosedForSession === sessionId) return s;
			return { ...s, open: true };
		});
	},

	/** Current state snapshot. */
	getState(sessionId: string | null): BrowserPanelState {
		return this.forSession(sessionId);
	},
};
