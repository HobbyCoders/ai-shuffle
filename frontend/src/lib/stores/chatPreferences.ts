/**
 * Chat Preferences Store
 *
 * Manages user preferences for chat behavior, persisted via API.
 * Provides reactive stores for UI components to consume.
 */

import { writable, get } from 'svelte/store';
import { getPreference, setPreference } from '$lib/api/client';

// Preference key in the database
const CHAT_PREFS_KEY = 'chat_behavior';

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
};

// Internal state
const { subscribe, set, update } = writable<ChatPreferences>(DEFAULT_PREFERENCES);

// Track if we've loaded from API
let isLoaded = false;
let isLoading = false;

/**
 * Load preferences from API
 */
async function load(): Promise<void> {
	if (isLoaded || isLoading) return;

	isLoading = true;
	try {
		const response = await getPreference<ChatPreferences>(CHAT_PREFS_KEY);
		if (response && response.value) {
			// Merge with defaults to handle new preference keys
			const loaded = response.value as Partial<ChatPreferences>;
			set({ ...DEFAULT_PREFERENCES, ...loaded });
		}
		isLoaded = true;
	} catch (e) {
		console.error('[ChatPreferences] Failed to load preferences:', e);
		// Keep defaults on error
	} finally {
		isLoading = false;
	}
}

/**
 * Save current preferences to API (debounced internally)
 */
let saveTimer: ReturnType<typeof setTimeout> | null = null;
const SAVE_DEBOUNCE_MS = 500;

async function save(): Promise<void> {
	// Clear any pending save
	if (saveTimer) {
		clearTimeout(saveTimer);
	}

	// Debounce saves
	saveTimer = setTimeout(async () => {
		try {
			const current = get({ subscribe });
			await setPreference(CHAT_PREFS_KEY, current);
		} catch (e) {
			console.error('[ChatPreferences] Failed to save preferences:', e);
		}
	}, SAVE_DEBOUNCE_MS);
}

/**
 * Update a single preference
 */
function updatePreference<K extends keyof ChatPreferences>(
	key: K,
	value: ChatPreferences[K]
): void {
	update(prefs => {
		const updated = { ...prefs, [key]: value };
		return updated;
	});
	save();
}

/**
 * Reset all preferences to defaults
 */
function reset(): void {
	set(DEFAULT_PREFERENCES);
	save();
}

/**
 * Get current value of a preference (non-reactive)
 */
function getValue<K extends keyof ChatPreferences>(key: K): ChatPreferences[K] {
	return get({ subscribe })[key];
}

// Export the store and utilities
export const chatPreferences = {
	subscribe,
	load,
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
};

// Also export defaults for reference
export { DEFAULT_PREFERENCES };
