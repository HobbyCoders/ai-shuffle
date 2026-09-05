import { compatible } from '$lib/services/preference-values';
import { preferenceSync } from '$lib/services/preferences';
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
import { createSubscribers } from './store-utils';

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
interface HotkeyPreferences {
	// Global STT hotkey
	globalSttEnabled: boolean;
	globalSttHotkey: HotkeyConfig;
	/** Short tones when voice capture goes live, stops, or fails. On by default:
	 *  the go-cue is the whole point — it is what lets you start talking without
	 *  watching the screen. */
	voiceSoundCues: boolean;
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
	globalSttHotkey: DEFAULT_HOTKEY,
	voiceSoundCues: true
};

// ============================================
// Constants
// ============================================

const STORAGE_KEY = 'ai-shuffle-hotkey-preferences';
const SERVER_PREFERENCE_KEY = 'hotkey_settings';

// ============================================
// Server sync state (module-level mutable)
// ============================================


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


/**
 * Save preferences to server (debounced)
 */
function saveToServer(prefs: HotkeyPreferences): void {
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

let _state = $state<HotkeyPreferences>(initialPreferences);

// ============================================
// Subscribe shim (centralized via store-utils)
// ============================================

const { subscribe, notifySubscribers: _notifySubscribers, update: _update } =
	createSubscribers<HotkeyPreferences>(
		() => _state,
		(v) => { _state = v; }
	);

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

export const hotkeyPreferences = {
	get current() {
		return _state;
	},

	subscribe,

	load,
	syncFromServer,
	updatePreference,
	reset,
	getValue,

	// Convenience setters
	setGlobalSttEnabled: (value: boolean) => updatePreference('globalSttEnabled', value),
	setGlobalSttHotkey: (value: HotkeyConfig) => updatePreference('globalSttHotkey', value),
	setVoiceSoundCues: (value: boolean) => updatePreference('voiceSoundCues', value),
};
preferenceSync.register(SERVER_PREFERENCE_KEY, {
 read: () => _state,
 apply(value) {
  _state = compatible(DEFAULT_PREFERENCES, value);
  saveToLocalStorage(_state);
  _notifySubscribers();
 }
});
