/**
 * Hotkey Preferences Store
 *
 * Manages user preferences for keyboard shortcuts with two-tier persistence:
 * - localStorage: Instant load (no flash/delay)
 * - Server API: Cross-device sync (source of truth)
 *
 * This mirrors the chatPreferences store pattern for reliable settings persistence.
 */

import { writable, get } from 'svelte/store';
import { browser } from '$app/environment';
import { getPreference, setPreference } from '$lib/api/client';

// Storage keys
const STORAGE_KEY = 'ai-shuffle-hotkey-preferences';
const SERVER_PREFERENCE_KEY = 'hotkey_settings';
const SERVER_SYNC_DEBOUNCE_MS = 500;

// Server sync state
let serverSyncTimer: ReturnType<typeof setTimeout> | null = null;
let hasSyncedFromServer = false;
let pendingState: HotkeyPreferences | null = null;

/**
 * Hotkey configuration for a single shortcut
 */
export interface HotkeyConfig {
	key: string;           // The key code (e.g., 's', 'Enter', 'Space')
	cmdOrCtrl: boolean;    // Require Cmd (Mac) or Ctrl (Windows)
	shift: boolean;        // Require Shift key
	alt: boolean;          // Require Alt/Option key
}

/**
 * All hotkey-related preferences
 */
export interface HotkeyPreferences {
	// Global STT hotkey
	globalSttEnabled: boolean;
	globalSttHotkey: HotkeyConfig;
}

/**
 * Default hotkey: Ctrl/Cmd + Shift + S
 */
const DEFAULT_HOTKEY: HotkeyConfig = {
	key: 's',
	cmdOrCtrl: true,
	shift: true,
	alt: false
};

const DEFAULT_PREFERENCES: HotkeyPreferences = {
	globalSttEnabled: false, // Disabled by default - user must opt-in for privacy
	globalSttHotkey: DEFAULT_HOTKEY
};

/**
 * Load preferences from localStorage (synchronous, instant)
 */
function getStoredPreferences(): HotkeyPreferences {
	if (!browser) return DEFAULT_PREFERENCES;

	try {
		const stored = localStorage.getItem(STORAGE_KEY);
		if (stored) {
			const parsed = JSON.parse(stored) as Partial<HotkeyPreferences>;
			// Merge with defaults to handle new preference keys
			return {
				...DEFAULT_PREFERENCES,
				...parsed,
				// Ensure nested objects are properly merged
				globalSttHotkey: {
					...DEFAULT_HOTKEY,
					...(parsed.globalSttHotkey || {})
				}
			};
		}
	} catch (e) {
		console.error('[HotkeyPreferences] Failed to parse localStorage:', e);
	}

	return DEFAULT_PREFERENCES;
}

/**
 * Save preferences to localStorage (synchronous)
 */
function saveToLocalStorage(prefs: HotkeyPreferences): void {
	if (!browser) return;

	try {
		localStorage.setItem(STORAGE_KEY, JSON.stringify(prefs));
	} catch (e) {
		console.error('[HotkeyPreferences] Failed to save to localStorage:', e);
	}
}

/**
 * Load preferences from server
 */
async function loadFromServer(): Promise<HotkeyPreferences | null> {
	try {
		const response = await getPreference<HotkeyPreferences>(SERVER_PREFERENCE_KEY);
		return response?.value as HotkeyPreferences || null;
	} catch (e) {
		console.error('[HotkeyPreferences] Failed to load from server:', e);
		return null;
	}
}

/**
 * Save preferences to server (debounced)
 */
function saveToServer(prefs: HotkeyPreferences): void {
	if (!browser) return;

	// Track pending state for flush on unload
	pendingState = prefs;

	// Don't write to server until initial sync is complete
	// This prevents localStorage data from overwriting newer server data
	if (!hasSyncedFromServer) {
		console.log('[HotkeyPreferences] Skipping server save - initial sync not complete');
		return;
	}

	if (serverSyncTimer) {
		clearTimeout(serverSyncTimer);
	}

	serverSyncTimer = setTimeout(async () => {
		try {
			await setPreference(SERVER_PREFERENCE_KEY, prefs);
			console.log('[HotkeyPreferences] Synced to server');
			pendingState = null;
		} catch (e) {
			console.error('[HotkeyPreferences] Failed to save to server:', e);
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
	console.log('[HotkeyPreferences] Flushed pending state on unload');
	pendingState = null;
}

// Register beforeunload handler
if (browser) {
	window.addEventListener('beforeunload', flushPendingSave);
	document.addEventListener('visibilitychange', () => {
		if (document.visibilityState === 'hidden') {
			flushPendingSave();
		}
	});
}

// Initialize store with localStorage values (instant, no flash)
const initialPreferences = getStoredPreferences();
const { subscribe, set, update } = writable<HotkeyPreferences>(initialPreferences);

/**
 * Update a single preference
 */
function updatePreference<K extends keyof HotkeyPreferences>(
	key: K,
	value: HotkeyPreferences[K]
): void {
	update(prefs => {
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
	set(DEFAULT_PREFERENCES);
	saveToLocalStorage(DEFAULT_PREFERENCES);
	saveToServer(DEFAULT_PREFERENCES);
}

/**
 * Get current value of a preference (non-reactive)
 */
function getValue<K extends keyof HotkeyPreferences>(key: K): HotkeyPreferences[K] {
	return get({ subscribe })[key];
}

/**
 * Sync preferences from server (call after authentication)
 * Server is the source of truth - if server has different data, update local
 * If server is empty but local has data, push local to server (migration)
 */
async function syncFromServer(): Promise<void> {
	if (!browser) return;

	const serverState = await loadFromServer();
	const localState = get({ subscribe });

	if (serverState) {
		// Server has data - it's the source of truth
		// Merge with defaults to handle new preference keys
		const merged: HotkeyPreferences = {
			...DEFAULT_PREFERENCES,
			...serverState,
			globalSttHotkey: {
				...DEFAULT_HOTKEY,
				...(serverState.globalSttHotkey || {})
			}
		};

		// Check if server differs from local
		const isDifferent =
			merged.globalSttEnabled !== localState.globalSttEnabled ||
			merged.globalSttHotkey.key !== localState.globalSttHotkey.key ||
			merged.globalSttHotkey.cmdOrCtrl !== localState.globalSttHotkey.cmdOrCtrl ||
			merged.globalSttHotkey.shift !== localState.globalSttHotkey.shift ||
			merged.globalSttHotkey.alt !== localState.globalSttHotkey.alt;

		if (isDifferent) {
			console.log('[HotkeyPreferences] Server state differs, updating local');
			// Update localStorage
			saveToLocalStorage(merged);
			// Update store
			set(merged);
		} else {
			console.log('[HotkeyPreferences] Server and local in sync');
		}
	} else {
		// No server data - push local data to server (migration)
		console.log('[HotkeyPreferences] No server data, will push local to server');
	}

	// Mark sync as complete - now server writes are allowed
	hasSyncedFromServer = true;

	// If there was no server data and we have non-default local data, save it
	if (!serverState) {
		const hasNonDefaults =
			localState.globalSttEnabled !== DEFAULT_PREFERENCES.globalSttEnabled ||
			localState.globalSttHotkey.key !== DEFAULT_HOTKEY.key ||
			localState.globalSttHotkey.cmdOrCtrl !== DEFAULT_HOTKEY.cmdOrCtrl ||
			localState.globalSttHotkey.shift !== DEFAULT_HOTKEY.shift ||
			localState.globalSttHotkey.alt !== DEFAULT_HOTKEY.alt;

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

// Export the store and utilities
export const hotkeyPreferences = {
	subscribe,
	load,
	syncFromServer,
	updatePreference,
	reset,
	getValue,

	// Convenience setters
	setGlobalSttEnabled: (value: boolean) => updatePreference('globalSttEnabled', value),
	setGlobalSttHotkey: (value: HotkeyConfig) => updatePreference('globalSttHotkey', value),
};

// Also export defaults for reference
export { DEFAULT_PREFERENCES, DEFAULT_HOTKEY };
