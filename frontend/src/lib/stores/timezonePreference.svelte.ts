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
import { getPreference, setPreference } from '$lib/api/client';
import { createSubscribers } from './store-utils';

const STORAGE_KEY = 'ai-shuffle-timezone';
const SERVER_PREFERENCE_KEY = 'timezone';
const SERVER_SYNC_DEBOUNCE_MS = 500;

let serverSyncTimer: ReturnType<typeof setTimeout> | null = null;
let hasSyncedFromServer = false;
let pendingState: string | null = null;

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

async function loadFromServer(): Promise<string | null> {
	try {
		const response = await getPreference<string>(SERVER_PREFERENCE_KEY);
		return (response?.value as string) || null;
	} catch {
		return null;
	}
}

function saveToServer(tz: string): void {
	if (!browser || !hasSyncedFromServer) {
		pendingState = tz;
		return;
	}
	pendingState = tz;

	if (serverSyncTimer) clearTimeout(serverSyncTimer);
	serverSyncTimer = setTimeout(async () => {
		try {
			await setPreference(SERVER_PREFERENCE_KEY, tz);
			pendingState = null;
		} catch {
			// ignore
		}
		serverSyncTimer = null;
	}, SERVER_SYNC_DEBOUNCE_MS);
}

function flushPendingSave(): void {
	if (!pendingState || !hasSyncedFromServer || !browser) return;
	if (serverSyncTimer) {
		clearTimeout(serverSyncTimer);
		serverSyncTimer = null;
	}
	const data = JSON.stringify({ key: SERVER_PREFERENCE_KEY, value: pendingState });
	navigator.sendBeacon(
		'/api/v1/preferences/' + SERVER_PREFERENCE_KEY,
		new Blob([data], { type: 'application/json' })
	);
	pendingState = null;
}

// Guard against HMR duplicate registration
if (browser && !(window as any).__tzPrefs_listeners) {
	(window as any).__tzPrefs_listeners = true;
	window.addEventListener('beforeunload', flushPendingSave);
	document.addEventListener('visibilitychange', () => {
		if (document.visibilityState === 'hidden') flushPendingSave();
	});
}

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
		if (!browser) return;
		const serverState = await loadFromServer();
		if (serverState) {
			saveToLocalStorage(serverState);
			_state = serverState;
			_notifySubscribers();
		}
		hasSyncedFromServer = true;
		if (!serverState) {
			const local = _state;
			if (local !== DEFAULT_TIMEZONE) saveToServer(local);
		}
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
