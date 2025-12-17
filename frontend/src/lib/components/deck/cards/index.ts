/**
 * Card System for The Deck
 *
 * This module exports all card components and types for the deck UI system.
 */

// Components
export { default as BaseCard } from './BaseCard.svelte';
export { default as CardContainer } from './CardContainer.svelte';
export { default as ChatCard } from './ChatCard.svelte';
export { default as AgentCard } from './AgentCard.svelte';
export { default as CanvasCard } from './CanvasCard.svelte';
export { default as TerminalCard } from './TerminalCard.svelte';

// Types
export type {
	// Card types
	CardType,
	LayoutMode,
	DeckCard,

	// Chat types
	ChatMessage,
	ChatCardData,

	// Agent types
	TaskStatus,
	AgentStatus,
	AgentTask,
	DeckAgent,
	AgentCardData,

	// Canvas types
	CanvasProvider,
	CanvasContentType,
	CanvasSettings,
	CanvasVariation,
	DeckGeneration,
	CanvasCardData,

	// Terminal types
	TerminalCardData,

	// Union types
	AnyCardData,

	// Event types
	CardEvents,
	ContainerEvents
} from './types';
