/**
 * Store Index - Central export for all Svelte stores
 *
 * This file provides a single import point for all application stores.
 */

// =============================================================================
// Authentication Store
// =============================================================================

export {
	auth,
	isAuthenticated,
	isAdmin,
	setupRequired,
	claudeAuthenticated,
	githubAuthenticated,
	username,
	apiUser,
	authLoading,
	authError
} from './auth';

// =============================================================================
// Chat Store
// =============================================================================

export {
	chat,
	messages,
	isStreaming,
	chatError,
	profiles,
	selectedProfile,
	projects,
	selectedProject,
	sessions,
	currentSessionId,
	wsConnected,
	connectionState,
	deviceId,
	connectedDevices
} from './chat';

export type { ChatMessage, MessageType, Project } from './chat';

// =============================================================================
// Tabs Store
// =============================================================================

export { tabs } from './tabs';
export type { ChatTab, ApiUser, TodoItem, SubagentChildMessage } from './tabs';

// =============================================================================
// Theme Store
// =============================================================================

export { theme, themePreference, isDark, isLight } from './theme';
export type { Theme } from './theme';

// =============================================================================
// Device Store
// =============================================================================

export { deviceId as persistentDeviceId, getDeviceIdSync } from './device';

// =============================================================================
// PWA Store
// =============================================================================

export { pwa, isInstallable, isInstalled, isOnline, hasUpdate } from './pwa';

// =============================================================================
// Sync Store
// =============================================================================

export { sync, isConnected, isRemoteStreaming, connectedDevices as syncConnectedDevices, syncError } from './sync';
export type { SyncEvent, SyncState } from './sync';

// =============================================================================
// Groups Store
// =============================================================================

export { groups, projectGroups, profileGroups, subagentGroups, organizeByGroups } from './groups';
export type { EntityType, GroupDefinition, EntityGroups, GroupsState } from './groups';

// =============================================================================
// Git Store
// =============================================================================

export { git } from './git';

// =============================================================================
// GitHub Store
// =============================================================================

export { github } from './github';

// =============================================================================
// Deck Store (The Deck UI)
// =============================================================================

export {
	deck,
	// Derived stores
	activeCard,
	visibleCards,
	minimizedCards,
	pinnedCards,
	chatCards,
	agentCards,
	fileCards,
	generationCards,
	activeMode,
	cardLayout,
	contextPanelCollapsed,
	runningAgentsCount,
	activeGenerationsCount,
	totalCardsCount
} from './deck';

export type {
	ActivityMode,
	CardType,
	CardLayout,
	DeckCard,
	DeckAgent,
	DeckGeneration,
	DeckState,
	AgentStatus as DeckAgentStatus,
	GenerationStatus,
	GenerationType
} from './deck';

// =============================================================================
// Agents Store (Background Agents)
// =============================================================================

export {
	agents,
	// Derived stores
	allAgents,
	runningAgents,
	queuedAgents,
	pausedAgents,
	completedAgents,
	failedAgents,
	activeAgentsCount,
	agentsWsConnected,
	agentsWsError
} from './agents';

export type {
	AgentTask,
	BackgroundAgent,
	LaunchAgentOptions,
	TaskStatus,
	AgentStatus
} from './agents';

// =============================================================================
// Studio Store (Content Creation)
// =============================================================================

export {
	studio,
	// Derived stores
	activeGeneration,
	recentGenerations,
	savedAssets,
	imageAssets,
	videoAssets,
	imageProvider,
	videoProvider,
	defaultAspectRatio,
	studioLoading,
	studioError,
	activeGenerationsCount as studioActiveGenerationsCount,
	totalAssetsCount
} from './studio';

export type {
	ImageProvider,
	VideoProvider,
	AspectRatio,
	GenerationOptions,
	Asset,
	Generation
} from './studio';
