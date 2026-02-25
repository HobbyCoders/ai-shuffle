/**
 * Git Action Preferences Store
 *
 * Persists user selections from git action modals (Commit, Pull, Finish)
 * with two-tier persistence:
 * - localStorage: Instant load (no flash/delay)
 * - Server API: Cross-device sync (source of truth)
 *
 * Follows the same pattern as chatPreferences.svelte.ts.
 */

import { browser } from '$app/environment';
import { getPreference, setPreference } from '$lib/api/client';
import { untrack } from 'svelte';

// ============================================
// Types
// ============================================

export type CommitAction = 'commit' | 'push' | 'pr';
export type PullStrategy = 'merge' | 'rebase';
export type MergeMethod = 'squash' | 'merge' | 'rebase';

export interface GitActionPreferences {
	// Commit modal
	includeUnstaged: boolean;
	nextStep: CommitAction;

	// Pull modal
	pullStrategy: PullStrategy;
	autoStash: boolean;

	// Finish modal
	mergeMethod: MergeMethod;
	cleanupAfterMerge: boolean;
}

const DEFAULT_PREFERENCES: GitActionPreferences = {
	includeUnstaged: true,
	nextStep: 'push',
	pullStrategy: 'rebase',
	autoStash: false,
	mergeMethod: 'squash',
	cleanupAfterMerge: true,
};

// ============================================
// Constants
// ============================================

const STORAGE_KEY = 'ai-shuffle-git-action-preferences';
const SERVER_PREFERENCE_KEY = 'git_action_settings';
const SERVER_SYNC_DEBOUNCE_MS = 500;

// ============================================
// Server sync state (module-level mutable)
// ============================================

let serverSyncTimer: ReturnType<typeof setTimeout> | null = null;
let hasSyncedFromServer = false;
let pendingState: GitActionPreferences | null = null;

// ============================================
// Persistence helpers
// ============================================

/**
 * Load preferences from localStorage (synchronous, instant)
 */
function getStoredPreferences(): GitActionPreferences {
	if (!browser) return DEFAULT_PREFERENCES;

	try {
		const stored = localStorage.getItem(STORAGE_KEY);
		if (stored) {
			const parsed = JSON.parse(stored) as Partial<GitActionPreferences>;
			// Merge with defaults to handle new preference keys
			return { ...DEFAULT_PREFERENCES, ...parsed };
		}
	} catch (e) {
		console.error('[GitActionPreferences] Failed to parse localStorage:', e);
	}

	return DEFAULT_PREFERENCES;
}

/**
 * Save preferences to localStorage (synchronous)
 */
function saveToLocalStorage(prefs: GitActionPreferences): void {
	if (!browser) return;

	try {
		localStorage.setItem(STORAGE_KEY, JSON.stringify(prefs));
	} catch (e) {
		console.error('[GitActionPreferences] Failed to save to localStorage:', e);
	}
}

/**
 * Load preferences from server
 */
async function loadFromServer(): Promise<GitActionPreferences | null> {
	try {
		const response = await getPreference<GitActionPreferences>(SERVER_PREFERENCE_KEY);
		return response?.value as GitActionPreferences || null;
	} catch (e) {
		console.error('[GitActionPreferences] Failed to load from server:', e);
		return null;
	}
}

/**
 * Save preferences to server (debounced)
 */
function saveToServer(prefs: GitActionPreferences): void {
	if (!browser) return;

	// Track pending state for flush on unload
	pendingState = prefs;

	// Don't write to server until initial sync is complete
	if (!hasSyncedFromServer) return;

	if (serverSyncTimer) {
		clearTimeout(serverSyncTimer);
	}

	serverSyncTimer = setTimeout(async () => {
		try {
			await setPreference(SERVER_PREFERENCE_KEY, prefs);
			pendingState = null;
		} catch (e) {
			console.error('[GitActionPreferences] Failed to save to server:', e);
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

let _state = $state<GitActionPreferences>(initialPreferences);

// ============================================
// Subscribe shim
// ============================================

type Subscriber<T> = (value: T) => void;

const _subscribers = new Set<Subscriber<GitActionPreferences>>();

function _notifySubscribers() {
	untrack(() => { for (const fn of _subscribers) fn(_state); });
}

function _update(fn: (s: GitActionPreferences) => GitActionPreferences) {
	_state = fn(untrack(() => _state));
	_notifySubscribers();
}

// ============================================
// Actions
// ============================================

/**
 * Update a single preference
 */
function updatePreference<K extends keyof GitActionPreferences>(
	key: K,
	value: GitActionPreferences[K]
): void {
	_update(prefs => {
		const updated = { ...prefs, [key]: value };
		saveToLocalStorage(updated);
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
function getValue<K extends keyof GitActionPreferences>(key: K): GitActionPreferences[K] {
	return _state[key];
}

/**
 * Sync preferences from server (call after authentication)
 */
async function syncFromServer(): Promise<void> {
	if (!browser) return;

	const serverState = await loadFromServer();
	const localState = _state;

	if (serverState) {
		const merged = { ...DEFAULT_PREFERENCES, ...serverState };

		const isDifferent = Object.keys(merged).some(
			key => merged[key as keyof GitActionPreferences] !== localState[key as keyof GitActionPreferences]
		);

		if (isDifferent) {
			saveToLocalStorage(merged);
			_state = merged;
			_notifySubscribers();
		}
	}

	hasSyncedFromServer = true;

	// If no server data and we have non-default local data, push to server
	if (!serverState) {
		const hasNonDefaults = Object.keys(localState).some(
			key => localState[key as keyof GitActionPreferences] !== DEFAULT_PREFERENCES[key as keyof GitActionPreferences]
		);
		if (hasNonDefaults) {
			saveToServer(localState);
		}
	}
}

/**
 * Legacy load function — calls syncFromServer
 */
async function load(): Promise<void> {
	return syncFromServer();
}

// ============================================
// Exported Store
// ============================================

export const gitActionPreferences = {
	get current() {
		return _state;
	},

	subscribe(fn: Subscriber<GitActionPreferences>) {
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
	setIncludeUnstaged: (value: boolean) => updatePreference('includeUnstaged', value),
	setNextStep: (value: CommitAction) => updatePreference('nextStep', value),
	setPullStrategy: (value: PullStrategy) => updatePreference('pullStrategy', value),
	setAutoStash: (value: boolean) => updatePreference('autoStash', value),
	setMergeMethod: (value: MergeMethod) => updatePreference('mergeMethod', value),
	setCleanupAfterMerge: (value: boolean) => updatePreference('cleanupAfterMerge', value),
};

export { DEFAULT_PREFERENCES };
