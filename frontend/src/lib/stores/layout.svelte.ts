/**
 * Layout Store - Manages the sidebar + main panel layout state
 *
 * Migrated to Svelte 5 runes. Provides subscribe() shim for backward
 * compatibility with $storeName auto-subscription in .svelte files.
 */
import { untrack } from 'svelte';
import { browser } from '$app/environment';

// ============================================
// Types
// ============================================

export type ViewType =
	| 'chat'
	| 'settings'
	| 'profile'
	| 'subagent'
	| 'project'
	| 'file-browser'
	| 'plugins'
	| 'mcp'
	| 'hooks'
	| 'automations'
	| 'notes'
	| 'stats'
	| 'docs';

export interface ActiveView {
	type: ViewType;
	/** Session ID for chat views, null for new chat */
	sessionId?: string | null;
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
export const DEFAULT_SIDEBAR_WIDTH = 390;
export const MIN_SIDEBAR_WIDTH = 240;
export const MAX_SIDEBAR_WIDTH = 600;

// ============================================
// Persistence
// ============================================

function loadPersistedState(): Partial<LayoutState> | null {
	if (!browser) return null;
	try {
		const raw = localStorage.getItem(STORAGE_KEY);
		if (!raw) return null;
		return JSON.parse(raw);
	} catch {
		return null;
	}
}

function persistState(state: LayoutState) {
	if (!browser) return;
	try {
		localStorage.setItem(
			STORAGE_KEY,
			JSON.stringify({
				sidebarCollapsed: state.sidebarCollapsed,
				activeView: state.activeView,
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
	sidebarWidth: persisted?.sidebarWidth ?? DEFAULT_SIDEBAR_WIDTH
});

// ============================================
// Subscribe shim
// ============================================

type Subscriber<T> = (value: T) => void;

const _subscribers = new Set<Subscriber<LayoutState>>();

function _notifyAndPersist() {
	persistState(_state);
	for (const fn of _subscribers) {
		fn(_state);
	}
}

function _update(fn: (s: LayoutState) => LayoutState) {
	_state = fn(_state);
	_notifyAndPersist();
}

// ============================================
// Exported Store
// ============================================

export const layout = {
	get current() {
		return _state;
	},

	subscribe(fn: Subscriber<LayoutState>) {
		_subscribers.add(fn);
		untrack(() => fn(_state));
		return () => {
			_subscribers.delete(fn);
		};
	},

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
			activeView: { type: 'chat', sessionId }
		}));
	},

	/** Navigate to a feature page */
	openFeaturePage(type: Exclude<ViewType, 'chat'>) {
		_update((s) => ({
			...s,
			activeView: { type, sessionId: null }
		}));
	},

	/** Set sidebar width (clamped to min/max) */
	setSidebarWidth(width: number) {
		const clamped = Math.max(MIN_SIDEBAR_WIDTH, Math.min(MAX_SIDEBAR_WIDTH, width));
		_update((s) => ({ ...s, sidebarWidth: clamped }));
	},

	/** Set overlay mode (for responsive behavior) */
	setSidebarOverlay(overlay: boolean) {
		_update((s) => ({ ...s, sidebarOverlay: overlay }));
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
// Derived stores with subscribe shim
// ============================================

function createDerived<T>(getter: () => T) {
	return {
		get current() {
			return getter();
		},
		subscribe(fn: Subscriber<T>) {
			const handler: Subscriber<LayoutState> = () => fn(getter());
			_subscribers.add(handler);
			untrack(() => fn(getter()));
			return () => {
				_subscribers.delete(handler);
			};
		}
	};
}

export const sidebarCollapsed = createDerived(() => _state.sidebarCollapsed);
export const activeView = createDerived(() => _state.activeView);
export const sidebarOverlay = createDerived(() => _state.sidebarOverlay);
export const sidebarWidth = createDerived(() => _state.sidebarWidth);
export const isInPanelMode = createDerived(
	() => _state.activeView != null && _state.activeView.type !== 'chat'
);
