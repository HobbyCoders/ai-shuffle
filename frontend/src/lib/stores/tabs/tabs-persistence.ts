/**
 * Tab Persistence
 *
 * Handles saving/loading tab state to/from the backend for
 * cross-device persistence and session restore.
 *
 * Extracted from tabs.ts to reduce file size and separate concerns.
 */

import { api } from '$lib/api/client';
import type { TabsState, PersistedTabsState } from './types';

// ─── Tabs Persistence ────────────────────────────────────────────────────────

// Debounce timer for saving tabs
let saveTabsTimer: ReturnType<typeof setTimeout> | null = null;
const SAVE_DEBOUNCE_MS = 1000;

// Flag to prevent saving during initial load
let isInitializing = false;

// Track pending tabs state for flush on unload
let pendingTabsState: TabsState | null = null;

/** Set the initializing flag (called from tabs store during init/load) */
export function setInitializing(value: boolean): void {
	isInitializing = value;
}

/** Get the initializing flag */
export function getIsInitializing(): boolean {
	return isInitializing;
}

/**
 * Immediately flush any pending tabs save to backend (sync, for beforeunload)
 */
export function flushPendingTabsSave(): void {
	if (!pendingTabsState || isInitializing) return;

	// Cancel the debounce timer
	if (saveTabsTimer) {
		clearTimeout(saveTabsTimer);
		saveTabsTimer = null;
	}

	// Build the persisted state
	const persistedState: PersistedTabsState = {
		tabs: pendingTabsState.tabs.map(tab => ({
			id: tab.id,
			title: tab.title,
			sessionId: tab.sessionId,
			profile: tab.profile,
			project: tab.project,
			modelOverride: tab.modelOverride,
			permissionModeOverride: tab.permissionModeOverride,
			thinkingModeOverride: tab.thinkingModeOverride,
			// Worktree fields for restoring worktree mode on app resume
			executionMode: tab.executionMode,
			worktreeId: tab.worktreeId,
			worktreeBranch: tab.worktreeBranch,
			selectedWorktreeBranch: tab.selectedWorktreeBranch,
			worktreeMode: tab.worktreeMode,
			worktreeDeleted: tab.worktreeDeleted
		})),
		activeTabId: pendingTabsState.activeTabId
	};

	// Use sendBeacon for reliable delivery during page unload
	const data = JSON.stringify({ key: 'open_tabs', value: persistedState });
	navigator.sendBeacon('/api/v1/preferences/open_tabs', new Blob([data], { type: 'application/json' }));

	pendingTabsState = null;
}

/**
 * Broadcast tabs update via WebSocket for real-time cross-device sync.
 * Requires a map of WebSocket connections (injected from the tabs store).
 */
export function broadcastTabsUpdate(
	persistedState: PersistedTabsState,
	tabConnections: Map<string, WebSocket>
): void {
	// Find any open WebSocket connection to send through
	for (const [, ws] of tabConnections) {
		if (ws.readyState === WebSocket.OPEN) {
			ws.send(JSON.stringify({
				type: 'tabs_update',
				tabs_data: persistedState
			}));
			return; // Only need to send once, server will broadcast to all devices
		}
	}
}

/**
 * Save tabs state to backend (debounced).
 * Also broadcasts via WebSocket for real-time cross-device sync.
 */
export async function saveTabsToServer(
	state: TabsState,
	tabConnections: Map<string, WebSocket>
): Promise<void> {
	// Don't save during initialization
	if (isInitializing) return;

	// Store pending state for flush on unload
	pendingTabsState = state;

	// Clear existing timer
	if (saveTabsTimer) {
		clearTimeout(saveTabsTimer);
	}

	// Debounce the save
	saveTabsTimer = setTimeout(async () => {
		if (!pendingTabsState) return;

		try {
			const persistedState: PersistedTabsState = {
				tabs: pendingTabsState.tabs.map(tab => ({
					id: tab.id,
					title: tab.title,
					sessionId: tab.sessionId,
					profile: tab.profile,
					project: tab.project,
					modelOverride: tab.modelOverride,
					permissionModeOverride: tab.permissionModeOverride,
					thinkingModeOverride: tab.thinkingModeOverride,
					// Worktree fields for restoring worktree mode on app resume
					executionMode: tab.executionMode,
					worktreeId: tab.worktreeId,
					worktreeBranch: tab.worktreeBranch,
					selectedWorktreeBranch: tab.selectedWorktreeBranch,
					worktreeMode: tab.worktreeMode,
					worktreeDeleted: tab.worktreeDeleted
				})),
				activeTabId: pendingTabsState.activeTabId
			};

			// Save to backend (REST API for persistence)
			await api.put('/preferences/open_tabs', {
				key: 'open_tabs',
				value: persistedState
			});
			// Also broadcast via WebSocket for real-time sync to other devices
			broadcastTabsUpdate(persistedState, tabConnections);
		} catch (error) {
			console.error('[Tabs] Failed to save tabs state:', error);
		}

		pendingTabsState = null;
		saveTabsTimer = null;
	}, SAVE_DEBOUNCE_MS);
}

/**
 * Load tabs state from backend
 */
export async function loadTabsFromServer(): Promise<PersistedTabsState | null> {
	try {
		const response = await api.get<{ key: string; value: PersistedTabsState } | null>('/preferences/open_tabs');
		if (response && response.value) {
			return response.value;
		}
	} catch (error) {
		console.error('[Tabs] Failed to load tabs state:', error);
	}
	return null;
}

// ─── Selection Persistence ───────────────────────────────────────────────────

// Debounce timer for selection sync
let selectionSyncTimeout: ReturnType<typeof setTimeout> | null = null;
let pendingSelection: { profile: string; project: string } | null = null;
const SELECTION_SYNC_DEBOUNCE_MS = 500;

// Load persisted values - empty string means no selection (user must choose)
export function getPersistedProfile(): string {
	if (typeof window !== 'undefined') {
		return localStorage.getItem('aihub_selectedProfile') || '';
	}
	return '';
}

export function getPersistedProject(): string {
	if (typeof window !== 'undefined') {
		return localStorage.getItem('aihub_selectedProject') || '';
	}
	return '';
}

/**
 * Load selection from backend (used during init to sync with backend)
 */
export async function loadSelectionFromBackend(): Promise<{ selectedProfile: string; selectedProject: string } | null> {
	if (typeof window === 'undefined') return null;

	try {
		const response = await fetch('/api/v1/preferences/selection', {
			credentials: 'include'
		});

		if (response.ok) {
			const data = await response.json();
			if (data && data.value) {
				// Update localStorage with backend values
				if (data.value.selectedProfile) {
					localStorage.setItem('aihub_selectedProfile', data.value.selectedProfile);
				}
				if (data.value.selectedProject) {
					localStorage.setItem('aihub_selectedProject', data.value.selectedProject);
				}
				return {
					selectedProfile: data.value.selectedProfile || '',
					selectedProject: data.value.selectedProject || ''
				};
			}
		}
	} catch (e) {
		console.error('[Tabs] Failed to load selection from backend:', e);
	}

	return null;
}

/**
 * Immediately flush any pending selection save to backend (sync, for beforeunload)
 */
export function flushPendingSelectionSave(): void {
	if (!pendingSelection) return;

	// Cancel the debounce timer
	if (selectionSyncTimeout) {
		clearTimeout(selectionSyncTimeout);
		selectionSyncTimeout = null;
	}

	// Use sendBeacon for reliable delivery during page unload
	const data = JSON.stringify({
		key: 'selection',
		value: { selectedProfile: pendingSelection.profile, selectedProject: pendingSelection.project }
	});
	navigator.sendBeacon('/api/v1/preferences/selection', new Blob([data], { type: 'application/json' }));

	pendingSelection = null;
}

/**
 * Save selection to backend (debounced) for cross-device persistence.
 *
 * IMPORTANT: If profile or project is empty, we preserve the existing localStorage value
 * to avoid accidentally clearing the user's selection when only one field changes.
 */
export async function saveSelectionToBackend(profile: string, project: string): Promise<void> {
	// If either value is empty, preserve the existing localStorage value
	const effectiveProfile = profile || getPersistedProfile();
	const effectiveProject = project || getPersistedProject();

	// Update localStorage immediately (always update both to keep in sync)
	if (typeof window !== 'undefined') {
		if (effectiveProfile) {
			localStorage.setItem('aihub_selectedProfile', effectiveProfile);
		}
		if (effectiveProject) {
			localStorage.setItem('aihub_selectedProject', effectiveProject);
		}
	}

	// Store pending selection for flush on unload (use effective values)
	pendingSelection = { profile: effectiveProfile, project: effectiveProject };

	// Clear existing timer
	if (selectionSyncTimeout) {
		clearTimeout(selectionSyncTimeout);
	}

	// Debounce the sync to avoid excessive API calls
	selectionSyncTimeout = setTimeout(async () => {
		if (!pendingSelection) return;

		try {
			await fetch('/api/v1/preferences/selection', {
				method: 'PUT',
				headers: { 'Content-Type': 'application/json' },
				credentials: 'include',
				body: JSON.stringify({
					key: 'selection',
					value: { selectedProfile: pendingSelection.profile, selectedProject: pendingSelection.project }
				})
			});
		} catch (e) {
			console.error('[Tabs] Failed to sync selection to backend:', e);
		}

		pendingSelection = null;
		selectionSyncTimeout = null;
	}, SELECTION_SYNC_DEBOUNCE_MS);
}

// NOTE: beforeunload/visibilitychange listeners are NOT registered here.
// The tabs store (tabs.ts) registers its own listeners that operate on its
// module-scoped pending state variables. Registering listeners here would
// be redundant — the pendingTabsState and pendingSelection in this module
// are only populated when the exported functions are called directly, which
// the tabs store does not do (it uses its own local copies).
