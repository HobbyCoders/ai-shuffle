import { preferenceSync } from '$lib/services/preferences';
/**
 * Timezone Preference Store
 *
 * Manages the user's preferred timezone with two-tier persistence:
 * - localStorage: Instant load (no flash/delay)
 * - Server API: Cross-device sync (source of truth)
 *
 * Migrated to Svelte 5 runes.
 */

import { browser } from '$app/environment';
import { createSubscribers } from './store-utils';

const STORAGE_KEY = 'ai-shuffle-timezone';
const SERVER_PREFERENCE_KEY = 'timezone';


/** "local" means use the browser's default timezone */
const DEFAULT_TIMEZONE = 'local';

function getStoredTimezone(): string {
	if (!browser) return DEFAULT_TIMEZONE;
	try {
		return localStorage.getItem(STORAGE_KEY) || DEFAULT_TIMEZONE;
	} catch {
		return DEFAULT_TIMEZONE;
	}
}

function saveToLocalStorage(tz: string): void {
	if (!browser) return;
	try {
		localStorage.setItem(STORAGE_KEY, tz);
	} catch {
		// ignore
	}
}

function saveToServer(tz: string): void {
 preferenceSync.save(SERVER_PREFERENCE_KEY, tz);
}

// Guard against HMR duplicate registration
// Persistence lifecycle is owned by live-sync, not module-level unload listeners.

// ============================================
// Reactive State
// ============================================

let _state = $state<string>(getStoredTimezone());

// ============================================
// Subscribe shim (centralized via store-utils)
// ============================================

const { subscribe, notifySubscribers: _notifySubscribers } =
	createSubscribers<string>(
		() => _state,
		(v) => { _state = v; }
	);

// ============================================
// Exported Store
// ============================================

export const timezonePreference = {
	get current() {
		return _state;
	},

	subscribe,

	load(): Promise<void> {
		return syncFromServer();
	},

	async syncFromServer(): Promise<void> {
 await preferenceSync.refresh(SERVER_PREFERENCE_KEY);
},

	setTimezone(tz: string): void {
		_state = tz;
		_notifySubscribers();
		saveToLocalStorage(tz);
		saveToServer(tz);
	},

	/**
	 * Resolve the effective IANA timezone string.
	 * "local" -> browser default; anything else is used as-is.
	 */
	resolveTimezone(): string {
		const tz = _state;
		if (tz === 'local') {
			return Intl.DateTimeFormat().resolvedOptions().timeZone;
		}
		return tz;
	}
};

// Also make syncFromServer available at module level for the load() reference
async function syncFromServer(): Promise<void> {
	return timezonePreference.syncFromServer();
}
preferenceSync.register(SERVER_PREFERENCE_KEY, {
 read: () => _state,
 apply(value) {
  let next = typeof value === 'string' ? value : DEFAULT_TIMEZONE;
  if (next !== 'local') { try { new Intl.DateTimeFormat('en', { timeZone: next }); } catch { next = DEFAULT_TIMEZONE; } }
  _state = next; saveToLocalStorage(next); _notifySubscribers();
 }
});
