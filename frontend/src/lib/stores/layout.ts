/**
 * Layout store — Re-export shim.
 * Implementation lives in layout.svelte.ts (Svelte 5 runes).
 */
export {
	DEFAULT_SIDEBAR_WIDTH,
	MIN_SIDEBAR_WIDTH,
	MAX_SIDEBAR_WIDTH,
	layout,
	sidebarCollapsed,
	activeView,
	sidebarOverlay,
	sidebarWidth,
	isInPanelMode,
} from './layout.svelte';

export type {
	ViewType,
	ActiveView,
} from './layout.svelte';
