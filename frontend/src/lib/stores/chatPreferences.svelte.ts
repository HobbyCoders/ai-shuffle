/**
 * Chat Preferences Store
 *
 * Manages user preferences for chat behavior with two-tier persistence:
 * - localStorage: Instant load (no flash/delay)
 * - Server API: Cross-device sync (source of truth)
 *
 * Migrated to Svelte 5 runes. Provides subscribe() shim for backward
 * compatibility with $storeName auto-subscription in .svelte files.
 */

import { browser } from '$app/environment';
import { getPreference, setPreference } from '$lib/api/client';
import { createSubscribers } from './store-utils';

// ============================================
// Types
// ============================================

// Default values for all chat preferences
export interface ChatPreferences {
	// Thinking blocks
	showThinkingBlocks: boolean;        // Whether to show thinking blocks at all
	thinkingStartExpanded: boolean;     // Initial collapse state for thinking blocks
	showThinkingSummary: boolean;       // Show summary preview when thinking is collapsed

	// Tool results
	toolsStartExpanded: boolean;        // Initial collapse state for tool results

	// Todo list
	todosStartExpanded: boolean;        // Initial collapse state for todo list

	// Auto-scroll behavior
	autoScrollOnNewMessage: boolean;    // Auto-scroll when new messages arrive

	// Message display
	showMessageTimestamps: boolean;     // Show timestamps on messages
	compactMessageView: boolean;        // Use compact message spacing

	// Fast mode
	fastMode: boolean;                  // Enable fast mode (Opus 4.6 only — faster output via beta header)
}

const DEFAULT_PREFERENCES: ChatPreferences = {
	showThinkingBlocks: true,
	thinkingStartExpanded: false,       // Collapsed by default (current behavior)
	showThinkingSummary: true,          // Show summary when collapsed by default
	toolsStartExpanded: false,          // Collapsed by default (current behavior)
	todosStartExpanded: true,           // Expanded by default (current behavior)
	autoScrollOnNewMessage: true,
	showMessageTimestamps: false,       // Off by default - cleaner look
	compactMessageView: false,
	fastMode: false,                    // Off by default - only works with Opus 4.6
};

// ============================================
// Constants
// ============================================

const STORAGE_KEY = 'ai-shuffle-chat-preferences';
const SERVER_PREFERENCE_KEY = 'chat_behavior';
const SERVER_SYNC_DEBOUNCE_MS = 500;

// ============================================
// Server sync state (module-level mutable)
// ============================================

let serverSyncTimer: ReturnType<typeof setTimeout> | null = null;
let hasSyncedFromServer = false;
let pendingState: ChatPreferences | null = null;

// ============================================
// Persistence helpers
// ============================================

/**
 * Load preferences from localStorage (synchronous, instant)
 */
function getStoredPreferences(): ChatPreferences {
	if (!browser) return DEFAULT_PREFERENCES;

	try {
		const stored = localStorage.getItem(STORAGE_KEY);
		if (stored) {
			const parsed = JSON.parse(stored) as Partial<ChatPreferences>;
			// Merge with defaults to handle new preference keys
			return { ...DEFAULT_PREFERENCES, ...parsed };
		}
	} catch (e) {
		console.error('[ChatPreferences] Failed to parse localStorage:', e);
	}

	return DEFAULT_PREFERENCES;
}

/**
 * Save preferences to localStorage (synchronous)
 */
function saveToLocalStorage(prefs: ChatPreferences): void {
	if (!browser) return;

	try {
		localStorage.setItem(STORAGE_KEY, JSON.stringify(prefs));
	} catch (e) {
		console.error('[ChatPreferences] Failed to save to localStorage:', e);
	}
}

/**
 * Load preferences from server
 */
async function loadFromServer(): Promise<ChatPreferences | null> {
	try {
		const response = await getPreference<ChatPreferences>(SERVER_PREFERENCE_KEY);
		return response?.value as ChatPreferences || null;
	} catch (e) {
		console.error('[ChatPreferences] Failed to load from server:', e);
		return null;
	}
}

/**
 * Save preferences to server (debounced)
 */
function saveToServer(prefs: ChatPreferences): void {
	if (!browser) return;

	// Track pending state for flush on unload
	pendingState = prefs;

	// Don't write to server until initial sync is complete
	// This prevents localStorage data from overwriting newer server data
	if (!hasSyncedFromServer) {
		return;
	}

	if (serverSyncTimer) {
		clearTimeout(serverSyncTimer);
	}

	serverSyncTimer = setTimeout(async () => {
		try {
			await setPreference(SERVER_PREFERENCE_KEY, prefs);
			pendingState = null;
		} catch (e) {
			console.error('[ChatPreferences] Failed to save to server:', e);
		}
		serverSyncTimer = null;
	}, SERVER_SYNC_DEBOUNCE_MS);
}

/**
 * Flush pending save immediately (for beforeunload)
 */
function flushPendingSave(): void {
	if (!pendingState || !hasSyncedFromServer || !browser) return;

	if (serverSyncTimer) {
		clearTimeout(serverSyncTimer);
		serverSyncTimer = null;
	}

	// Use sendBeacon for reliable delivery during page unload
	const data = JSON.stringify({
		key: SERVER_PREFERENCE_KEY,
		value: pendingState
	});
	navigator.sendBeacon(
		'/api/v1/preferences/' + SERVER_PREFERENCE_KEY,
		new Blob([data], { type: 'application/json' })
	);
	pendingState = null;
}

// Register beforeunload handler (guarded against HMR duplicate registration)
if (browser && !(window as any).__chatPrefs_listeners) {
	(window as any).__chatPrefs_listeners = true;
	window.addEventListener('beforeunload', flushPendingSave);
	document.addEventListener('visibilitychange', () => {
		if (document.visibilityState === 'hidden') {
			flushPendingSave();
		}
	});
}

// ============================================
// Reactive State
// ============================================

const initialPreferences = getStoredPreferences();

let _state = $state<ChatPreferences>(initialPreferences);

// ============================================
// Subscribe shim (centralized via store-utils)
// ============================================

const { subscribe, notifySubscribers: _notifySubscribers, update: _update } =
	createSubscribers<ChatPreferences>(
		() => _state,
		(v) => { _state = v; }
	);

// ============================================
// Actions
// ============================================

/**
 * Update a single preference
 */
function updatePreference<K extends keyof ChatPreferences>(
	key: K,
	value: ChatPreferences[K]
): void {
	_update(prefs => {
		const updated = { ...prefs, [key]: value };
		// Save to localStorage immediately (instant persistence)
		saveToLocalStorage(updated);
		// Save to server (debounced)
		saveToServer(updated);
		return updated;
	});
}

/**
 * Reset all preferences to defaults
 */
function reset(): void {
	_state = DEFAULT_PREFERENCES;
	_notifySubscribers();
	saveToLocalStorage(DEFAULT_PREFERENCES);
	saveToServer(DEFAULT_PREFERENCES);
}

/**
 * Get current value of a preference (non-reactive)
 */
function getValue<K extends keyof ChatPreferences>(key: K): ChatPreferences[K] {
	return _state[key];
}

/**
 * Sync preferences from server (call after authentication)
 * Server is the source of truth - if server has different data, update local
 * If server is empty but local has data, push local to server (migration)
 */
async function syncFromServer(): Promise<void> {
	if (!browser) return;

	const serverState = await loadFromServer();
	const localState = _state;

	if (serverState) {
		// Server has data - it's the source of truth
		// Merge with defaults to handle new preference keys
		const merged = { ...DEFAULT_PREFERENCES, ...serverState };

		// Check if server differs from local
		const isDifferent = Object.keys(merged).some(
			key => merged[key as keyof ChatPreferences] !== localState[key as keyof ChatPreferences]
		);

		if (isDifferent) {
			// Update localStorage
			saveToLocalStorage(merged);
			// Update store
			_state = merged;
			_notifySubscribers();
		}
	} else {
		// No server data - push local data to server (migration)
	}

	// Mark sync as complete - now server writes are allowed
	hasSyncedFromServer = true;

	// If there was no server data and we have non-default local data, save it
	if (!serverState) {
		const hasNonDefaults = Object.keys(localState).some(
			key => localState[key as keyof ChatPreferences] !== DEFAULT_PREFERENCES[key as keyof ChatPreferences]
		);
		if (hasNonDefaults) {
			saveToServer(localState);
		}
	}
}

/**
 * Legacy load function - now just calls syncFromServer
 * Kept for backward compatibility with existing code
 */
async function load(): Promise<void> {
	return syncFromServer();
}

// ============================================
// Exported Store
// ============================================

export const chatPreferences = {
	get current() {
		return _state;
	},

	subscribe,

	load,
	syncFromServer,
	updatePreference,
	reset,
	getValue,

	// Convenience setters for each preference
	setShowThinkingBlocks: (value: boolean) => updatePreference('showThinkingBlocks', value),
	setThinkingStartExpanded: (value: boolean) => updatePreference('thinkingStartExpanded', value),
	setShowThinkingSummary: (value: boolean) => updatePreference('showThinkingSummary', value),
	setToolsStartExpanded: (value: boolean) => updatePreference('toolsStartExpanded', value),
	setTodosStartExpanded: (value: boolean) => updatePreference('todosStartExpanded', value),
	setAutoScrollOnNewMessage: (value: boolean) => updatePreference('autoScrollOnNewMessage', value),
	setShowMessageTimestamps: (value: boolean) => updatePreference('showMessageTimestamps', value),
	setCompactMessageView: (value: boolean) => updatePreference('compactMessageView', value),
	setFastMode: (value: boolean) => updatePreference('fastMode', value),
};

// Also export defaults for reference
export { DEFAULT_PREFERENCES };
