import { preferenceSync } from '$lib/services/preferences';
/**
 * Layout Store - Manages the sidebar + main panel layout state
 *
 * Migrated to Svelte 5 runes. Provides subscribe() shim for backward
 * compatibility with $storeName auto-subscription in .svelte files.
 */
import { browser } from '$app/environment';
import { untrack } from 'svelte';
import { createSubscribers } from './store-utils';

// ============================================
// Types
// ============================================

export type ViewType =
	| 'session'
	| 'board'
	| 'build'
	| 'settings'
	| 'profile'
	| 'project'
	| 'file-browser'
	| 'plugins'
	| 'mcp'
	| 'hooks'
	| 'automations'
	| 'notes'
	| 'stats'
	| 'docs';

interface ActiveView {
	type: ViewType;
	/** Session ID for the conversation panel, null for a new session */
	sessionId?: string | null;
	/**
	 * Deep-link target within a feature page (e.g. an agent id to open in its
	 * editor). One-shot: never persisted to localStorage and cleared after
	 * the target page consumes it, so a reload doesn't re-open the editor.
	 */
	itemId?: string | null;
}

interface LayoutState {
	sidebarCollapsed: boolean;
	activeView: ActiveView | null;
	/** Track if sidebar is in overlay mode (mobile/tablet) */
	sidebarOverlay: boolean;
	/** Sidebar width in pixels (user-resizable) */
	sidebarWidth: number;
}

// ============================================
// Constants
// ============================================

const STORAGE_KEY = 'ai-shuffle-layout';
const DEFAULT_SIDEBAR_WIDTH = 256;
const MIN_SIDEBAR_WIDTH = 220;
const MAX_SIDEBAR_WIDTH = 400;

// ============================================
// Persistence
// ============================================

function loadPersistedState(): Partial<LayoutState> | null {
	if (!browser) return null;
	try {
		const raw = localStorage.getItem(STORAGE_KEY);
		if (!raw) return null;
		const parsed = JSON.parse(raw);
		// Pre-rename ViewType 'chat' meant this conversation panel, not SessionMode.
		if (parsed?.activeView?.type === 'chat') parsed.activeView.type = 'session';
		return parsed;
	} catch {
		return null;
	}
}

function persistState(state: LayoutState) {
	if (!browser) return;
	try {
		// Strip the one-shot deep-link itemId so a reload never re-opens an editor.
		const persistedView = state.activeView
			? (() => {
					const { itemId: _itemId, ...rest } = state.activeView;
					return rest;
				})()
			: null;
		localStorage.setItem(
			STORAGE_KEY,
			JSON.stringify({
				sidebarCollapsed: savedSidebarCollapsed,
				activeView: persistedView,
				sidebarWidth: state.sidebarWidth
			})
		);
	} catch {
		// Ignore storage errors
	}
}

// ============================================
// Reactive State
// ============================================

const persisted = loadPersistedState();

let _state = $state<LayoutState>({
	sidebarCollapsed: persisted?.sidebarCollapsed ?? false,
	activeView: persisted?.activeView ?? null,
	sidebarOverlay: false,
	sidebarWidth: (browser && Number(localStorage.getItem('sidebar-width'))) || persisted?.sidebarWidth || DEFAULT_SIDEBAR_WIDTH
});

// ============================================
// Subscribe shim (centralized via store-utils)
// ============================================

const { subscribe, notifySubscribers, createDerived } =
	createSubscribers<LayoutState>(
		() => _state,
		(v) => { _state = v; }
	);

function _notifyAndPersist() {
	persistState(_state);
 if (browser) localStorage.removeItem('sidebar-width');
	notifySubscribers();
}

let savedSidebarCollapsed = untrack(() => _state.sidebarCollapsed);
function _update(fn: (s: LayoutState) => LayoutState) {
 const before = untrack(() => _state);
 _state = fn(before);
 if (!_state.sidebarOverlay && !before.sidebarOverlay && _state.sidebarCollapsed !== before.sidebarCollapsed) savedSidebarCollapsed = _state.sidebarCollapsed;
 if ((!_state.sidebarOverlay && !before.sidebarOverlay && _state.sidebarCollapsed !== before.sidebarCollapsed) || _state.sidebarWidth !== before.sidebarWidth)
  preferenceSync.save('layout', { sidebarCollapsed: savedSidebarCollapsed, sidebarWidth: _state.sidebarWidth });
	_notifyAndPersist();
}

// ============================================
// Exported Store
// ============================================

export const layout = {
	get current() {
		return _state;
	},

	subscribe,

	/** Toggle sidebar collapsed state */
	toggleSidebar() {
		_update((s) => ({ ...s, sidebarCollapsed: !s.sidebarCollapsed }));
	},

	/** Set sidebar collapsed state */
	setSidebarCollapsed(collapsed: boolean) {
		_update((s) => ({ ...s, sidebarCollapsed: collapsed }));
	},

	/** Set the active view (what's shown in the main panel) */
	setActiveView(view: ActiveView | null) {
		_update((s) => ({ ...s, activeView: view }));
	},

	/** Navigate to a chat session */
	openChat(sessionId: string | null) {
		_update((s) => ({
			...s,
			activeView: { type: 'session', sessionId }
		}));
	},

	/** Navigate home. The Sessions board is retired — home is a fresh chat;
	 *  sessions are navigated from the sidebar. ('board' stays in the ViewType
	 *  union so persisted views from older builds still type-check.) */
	openBoard() {
		_update((s) => ({
			...s,
			activeView: { type: 'session', sessionId: null }
		}));
	},

	/** Navigate to a feature page, optionally deep-linking to an item id. */
	openFeaturePage(type: Exclude<ViewType, 'session'>, itemId?: string | null) {
		_update((s) => ({
			...s,
			activeView: { type, sessionId: null, itemId: itemId ?? null }
		}));
	},

	/**
	 * Clear the one-shot deep-link itemId once a feature page has consumed it,
	 * so remounts (e.g. switching Build sub-tabs) don't re-open the editor.
	 */
	clearFeatureItemId() {
		_update((s) =>
			s.activeView?.itemId == null
				? s
				: { ...s, activeView: { ...s.activeView, itemId: null } }
		);
	},

	/** Set sidebar width (clamped to min/max) */
	setSidebarWidth(width: number) {
		const clamped = Math.max(MIN_SIDEBAR_WIDTH, Math.min(MAX_SIDEBAR_WIDTH, width));
		_update((s) => ({ ...s, sidebarWidth: clamped }));
	},

	/** Set overlay mode (for responsive behavior) */
	setSidebarOverlay(overlay: boolean) {
		_update((s) => ({ ...s, sidebarOverlay: overlay, sidebarCollapsed: overlay ? true : savedSidebarCollapsed }));
	},

	/** Get current state snapshot */
	getState(): LayoutState {
		return _state;
	},

	/** Reset to defaults */
	reset() {
		_state = {
			sidebarCollapsed: false,
			activeView: null,
			sidebarOverlay: false,
			sidebarWidth: DEFAULT_SIDEBAR_WIDTH
		};
		_notifyAndPersist();
	}
};

// ============================================
// Derived stores (via store-utils createDerived)
// ============================================

export const sidebarCollapsed = createDerived(() => _state.sidebarCollapsed);
export const activeView = createDerived(() => _state.activeView);
export const sidebarOverlay = createDerived(() => _state.sidebarOverlay);
preferenceSync.register('layout', {
 read: () => ({ sidebarCollapsed: savedSidebarCollapsed, sidebarWidth: _state.sidebarWidth }),
 apply(value) {
  const raw = (value ?? {}) as Partial<LayoutState>;
  savedSidebarCollapsed = typeof raw.sidebarCollapsed === 'boolean' ? raw.sidebarCollapsed : false;
  const sidebarWidth = typeof raw.sidebarWidth === 'number' && Number.isFinite(raw.sidebarWidth)
   ? Math.max(MIN_SIDEBAR_WIDTH, Math.min(MAX_SIDEBAR_WIDTH, raw.sidebarWidth)) : DEFAULT_SIDEBAR_WIDTH;
  _state = { ..._state, sidebarWidth, sidebarCollapsed: _state.sidebarOverlay ? _state.sidebarCollapsed : savedSidebarCollapsed };
  _notifyAndPersist();
 }
});
