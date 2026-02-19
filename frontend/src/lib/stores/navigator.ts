/**
 * Navigator Store — Re-export shim.
 * Implementation lives in navigator.svelte.ts (Svelte 5 runes).
 */
export {
	navigator,
	DECK_COLORS,
	DECK_ICONS,
	editMode,
	decks,
	cardOrder,
	navigatorInitialized,
	recentSessionsView,
} from './navigator.svelte';

export type {
	CustomDeck,
	RecentSessionsViewMode,
	NavigatorPersistedState,
	NavigatorState,
} from './navigator.svelte';
