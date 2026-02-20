/**
 * Hotkey Preferences Store
 *
 * Manages user preferences for keyboard shortcuts with two-tier persistence:
 * - localStorage: Instant load (no flash/delay)
 * - Server API: Cross-device sync (source of truth)
 *
 * Migrated to Svelte 5 runes. Provides subscribe() shim for backward
 * compatibility with $storeName auto-subscription in .svelte files.
 */

import { browser } from '$app/environment';
import { getPreference, setPreference } from '$lib/api/client';
import { untrack } from 'svelte';

// ============================================
// Types
// ============================================

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

// ============================================
// Constants
// ============================================

const STORAGE_KEY = 'ai-shuffle-hotkey-preferences';
const SERVER_PREFERENCE_KEY = 'hotkey_settings';
const SERVER_SYNC_DEBOUNCE_MS = 500;

// ============================================
// Server sync state (module-level mutable)
// ============================================

let serverSyncTimer: ReturnType<typeof setTimeout> | null = null;
let hasSyncedFromServer = false;
let pendingState: HotkeyPreferences | null = null;

// ============================================
// Persistence helpers
// ============================================

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

// ============================================
// Reactive State
// ============================================

const initialPreferences = getStoredPreferences();

let _state = $state<HotkeyPreferences>(initialPreferences);

// ============================================
// Subscribe shim
// ============================================

type Subscriber<T> = (value: T) => void;

const _subscribers = new Set<Subscriber<HotkeyPreferences>>();

function _notifySubscribers() {
	untrack(() => { for (const fn of _subscribers) fn(_state); });
}

function _update(fn: (s: HotkeyPreferences) => HotkeyPreferences) {
	_state = fn(untrack(() => _state));
	_notifySubscribers();
}

// ============================================
// Actions
// ============================================

/**
 * Update a single preference
 */
function updatePreference<K extends keyof HotkeyPreferences>(
	key: K,
	value: HotkeyPreferences[K]
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
function getValue<K extends keyof HotkeyPreferences>(key: K): HotkeyPreferences[K] {
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

// ============================================
// Exported Store
// ============================================

export const hotkeyPreferences = {
	get current() {
		return _state;
	},

	subscribe(fn: Subscriber<HotkeyPreferences>) {
		_subscribers.add(fn);
		untrack(() => fn(_state));
		return () => {
			_subscribers.delete(fn);
		};
	},

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
