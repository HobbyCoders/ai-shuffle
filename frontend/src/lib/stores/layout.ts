/**
 * Layout Store - Manages the sidebar + main panel layout state
 *
 * Replaces the deck store's layout modes with a simpler
 * sidebar + single active view model (Codex-style).
 */
import { writable, derived, get } from 'svelte/store';
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
	| 'notes'
	| 'stats';

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
// Store
// ============================================

function createLayoutStore() {
	const persisted = loadPersistedState();

	const store = writable<LayoutState>({
		sidebarCollapsed: persisted?.sidebarCollapsed ?? false,
		activeView: persisted?.activeView ?? null,
		sidebarOverlay: false,
		sidebarWidth: persisted?.sidebarWidth ?? DEFAULT_SIDEBAR_WIDTH,
	});

	const { subscribe, set, update } = store;

	// Persist on changes (module-level singleton - lives for app lifetime)
	if (browser) {
		store.subscribe((state) => {
			try {
				localStorage.setItem(
					STORAGE_KEY,
					JSON.stringify({
						sidebarCollapsed: state.sidebarCollapsed,
						activeView: state.activeView,
						sidebarWidth: state.sidebarWidth,
					})
				);
			} catch {
				// Ignore storage errors
			}
		});
	}

	return {
		subscribe,

		/** Toggle sidebar collapsed state */
		toggleSidebar() {
			update((s) => ({ ...s, sidebarCollapsed: !s.sidebarCollapsed }));
		},

		/** Set sidebar collapsed state */
		setSidebarCollapsed(collapsed: boolean) {
			update((s) => ({ ...s, sidebarCollapsed: collapsed }));
		},

		/** Set the active view (what's shown in the main panel) */
		setActiveView(view: ActiveView | null) {
			update((s) => ({ ...s, activeView: view }));
		},

		/** Navigate to a chat session */
		openChat(sessionId: string | null) {
			update((s) => ({
				...s,
				activeView: { type: 'chat', sessionId },
			}));
		},

		/** Navigate to a feature page */
		openFeaturePage(type: Exclude<ViewType, 'chat'>) {
			update((s) => ({
				...s,
				activeView: { type, sessionId: null },
			}));
		},

		/** Set sidebar width (clamped to min/max) */
		setSidebarWidth(width: number) {
			const clamped = Math.max(MIN_SIDEBAR_WIDTH, Math.min(MAX_SIDEBAR_WIDTH, width));
			update((s) => ({ ...s, sidebarWidth: clamped }));
		},

		/** Set overlay mode (for responsive behavior) */
		setSidebarOverlay(overlay: boolean) {
			update((s) => ({ ...s, sidebarOverlay: overlay }));
		},

		/** Get current state snapshot */
		getState(): LayoutState {
			return get(store);
		},

		/** Reset to defaults */
		reset() {
			set({
				sidebarCollapsed: false,
				activeView: null,
				sidebarOverlay: false,
				sidebarWidth: DEFAULT_SIDEBAR_WIDTH,
			});
		},
	};
}

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

// ============================================
// Exports
// ============================================

export const layout = createLayoutStore();

export const sidebarCollapsed = derived(
	{ subscribe: layout.subscribe },
	($layout) => $layout.sidebarCollapsed
);

export const activeView = derived(
	{ subscribe: layout.subscribe },
	($layout) => $layout.activeView
);

export const sidebarOverlay = derived(
	{ subscribe: layout.subscribe },
	($layout) => $layout.sidebarOverlay
);

export const sidebarWidth = derived(
	{ subscribe: layout.subscribe },
	($layout) => $layout.sidebarWidth
);

export const isInPanelMode = derived(
	{ subscribe: layout.subscribe },
	($layout) => $layout.activeView != null && $layout.activeView.type !== 'chat'
);
