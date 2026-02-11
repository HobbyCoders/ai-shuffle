/**
 * Tabs module barrel export.
 *
 * Re-exports types, utilities, and persistence functions
 * extracted from the main tabs store.
 */

// Types
export type {
	MessageType,
	SubagentChildMessage,
	TodoItem,
	ChatMessage,
	Project,
	ExecutionMode,
	ChatTab,
	TabsState,
	PersistedTab,
	PersistedTabsState
} from './types';

// Utilities
export {
	formatWorktreeError,
	getOrCreateDeviceId,
	generateTabId
} from './utils';

// Persistence
export {
	setInitializing,
	getIsInitializing,
	flushPendingTabsSave,
	broadcastTabsUpdate,
	saveTabsToServer,
	loadTabsFromServer,
	getPersistedProfile,
	getPersistedProject,
	loadSelectionFromBackend,
	flushPendingSelectionSave,
	saveSelectionToBackend
} from './tabs-persistence';
