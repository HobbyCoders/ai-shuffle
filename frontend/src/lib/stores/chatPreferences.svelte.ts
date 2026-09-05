import { compatible } from '$lib/services/preference-values';
import { preferenceSync } from '$lib/services/preferences';
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
import { createSubscribers } from './store-utils';

// ============================================
// Types
// ============================================

// Default values for all chat preferences
export interface ChatPreferences {
	suggestNextResponse: boolean;       // Offer an ephemeral next-reply draft (extra model call)
	// Thinking blocks
	showThinkingBlocks: boolean;        // Whether to show thinking blocks at all
	thinkingStartExpanded: boolean;     // Initial collapse state for thinking blocks
	showThinkingSummary: boolean;       // Show summary preview when thinking is collapsed

	// Tool results
	toolsStartExpanded: boolean;        // Initial collapse state for tool results
	groupToolCalls: boolean;            // Collapse consecutive tool calls into one group card

	// Background tasks strip
	activeTasksCollapsed: boolean;      // Whether the active-tasks strip is minimized

	// Todo list
	todosStartExpanded: boolean;        // Initial collapse state for todo list

	// Auto-scroll behavior
	autoScrollOnNewMessage: boolean;    // Auto-scroll when new messages arrive

	// Message display
	showMessageTimestamps: boolean;     // Show timestamps on messages
	compactMessageView: boolean;        // Use compact message spacing
}

const DEFAULT_PREFERENCES: ChatPreferences = {
	suggestNextResponse: false,
	showThinkingBlocks: true,
	thinkingStartExpanded: false,       // Collapsed by default (current behavior)
	showThinkingSummary: true,          // Show summary when collapsed by default
	toolsStartExpanded: false,          // Collapsed by default (current behavior)
	groupToolCalls: true,               // Group consecutive tools by default (current behavior)
	activeTasksCollapsed: false,        // Expanded by default
	todosStartExpanded: true,           // Expanded by default (current behavior)
	autoScrollOnNewMessage: true,
	showMessageTimestamps: false,       // Off by default - cleaner look
	compactMessageView: false,
};

// ============================================
// Constants
// ============================================

const STORAGE_KEY = 'ai-shuffle-chat-preferences';
const SERVER_PREFERENCE_KEY = 'chat_behavior';

// ============================================
// Server sync state (module-level mutable)
// ============================================


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


/**
 * Save preferences to server (debounced)
 */
function saveToServer(prefs: ChatPreferences): void {
 preferenceSync.save(SERVER_PREFERENCE_KEY, prefs);
}

/**
 * Flush pending save immediately (for beforeunload)
 */


// Register beforeunload handler (guarded against HMR duplicate registration)
// Persistence lifecycle is owned by live-sync, not module-level unload listeners.

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
 await preferenceSync.refresh(SERVER_PREFERENCE_KEY);
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
	setGroupToolCalls: (value: boolean) => updatePreference('groupToolCalls', value),
	setActiveTasksCollapsed: (value: boolean) => updatePreference('activeTasksCollapsed', value),
	setTodosStartExpanded: (value: boolean) => updatePreference('todosStartExpanded', value),
	setAutoScrollOnNewMessage: (value: boolean) => updatePreference('autoScrollOnNewMessage', value),
	setShowMessageTimestamps: (value: boolean) => updatePreference('showMessageTimestamps', value),
	setCompactMessageView: (value: boolean) => updatePreference('compactMessageView', value),
};
preferenceSync.register(SERVER_PREFERENCE_KEY, {
 read: () => _state,
 apply(value) {
  _state = compatible(DEFAULT_PREFERENCES, value);
  saveToLocalStorage(_state);
  _notifySubscribers();
 }
});
