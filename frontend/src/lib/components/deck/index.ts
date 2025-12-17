/**
 * The Deck - AI Hub's card-based layout system
 *
 * This module exports all components and types for the deck layout system.
 */

// Components
export { default as DeckLayout } from './DeckLayout.svelte';
export { default as ActivityRail } from './ActivityRail.svelte';
export { default as ContextPanel } from './ContextPanel.svelte';
export { default as Dock } from './Dock.svelte';

// Types
export type {
	ActivityMode,
	AgentStatus,
	GenerationStatus,
	CardType,
	DeckAgent,
	DeckGeneration,
	DeckCard,
	DeckSession,
	ActivityBadge,
	ActivityBadges,
	SessionInfo,
	ContextSection
} from './types';
