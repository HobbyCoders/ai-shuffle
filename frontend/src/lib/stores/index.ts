/**
 * Store Exports
 *
 * Central export point for all Svelte stores in the application.
 *
 * Note: Some stores have overlapping export names. When importing, prefer
 * importing directly from the specific store module if you need to avoid
 * ambiguity, e.g.: import { deck } from '$lib/stores/deck';
 */

// Auth and User
export * from './auth';

// Chat and Sessions
export * from './chat';

// Git integration
export * from './git';

// Chat groups
export * from './groups';

// PWA features
export * from './pwa';

// Theme
export * from './theme';

// Canvas (media generation)
export * from './canvas';

// =============================================================================
// The Deck - Phase 3 Stores
// =============================================================================

// Deck - Main card-based workspace
// Note: Uses explicit exports to avoid conflicts with workspace store
export {
	deck,
	allCards,
	focusedCard,
	contextPanelCollapsed,
	workspaceBounds,
	isMobile,
	mobileActiveCardIndex,
	snapEnabled,
	gridSnapEnabled,
	// Types
	type DeckMode,
	type DeckCardType,
	type DeckCard,
	type DeckCardPosition,
	type DeckCardSize,
	type DeckState,
	type SnapZone
} from './deck';

// Background Agents
export * from './agents';

// Studio - Media generation
export * from './studio';

// =============================================================================
// Stores with overlapping exports - import directly from module if needed:
// - ./device (deviceId also in chat)
// - ./github (githubAuthenticated also in auth)
// - ./sync (connectedDevices also in chat)
// - ./tabs (ChatMessage, MessageType, Project, profiles, projects, sessions also in chat)
// =============================================================================
