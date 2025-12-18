// The Deck - Core Layout Components
export { default as DeckLayout } from './DeckLayout.svelte';
export { default as ActivityRail } from './ActivityRail.svelte';
export { default as ContextPanel } from './ContextPanel.svelte';
export { default as Dock } from './Dock.svelte';

// Types
export type {
	ActivityMode,
	ActivityBadges,
	DeckSession,
	DeckAgent,
	DeckGeneration,
	SessionInfo,
	CardGeometry,
	CardState,
	MinimizedCard,
	RunningProcess,
	ActivityButtonConfig
} from './types';
