import { compatible } from '$lib/services/preference-values';
import { preferenceSync } from '$lib/services/preferences';
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
import { createSubscribers } from './store-utils';

// ============================================
// Types
// ============================================

type CommitAction = 'commit' | 'push' | 'pr';
type PullStrategy = 'merge' | 'rebase';
type MergeMethod = 'squash' | 'merge' | 'rebase';

export interface GitActionPreferences {
	// Commit action defaults
	includeUnstaged: boolean;
	nextStep: CommitAction;

	// Pull action defaults
	pullStrategy: PullStrategy;
	autoStash: boolean;

	// Finish / Ship It defaults
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

/**
 * Merge a partial (localStorage or server) onto the defaults.
 *
 * Fills missing KNOWN keys from defaults, but PRESERVES any unknown keys the
 * blob carries. The `git_action_settings` preference is a single shared server
 * blob: an older app version must not wipe a newer version's keys just because
 * it doesn't recognize them (a whitelist rebuild would drop them on every save
 * and silently downgrade the shared blob). The only key we explicitly strip is
 * the known-dead `customMessages` from the removed canned-prompt system.
 */
function mergePreferences(parsed: Partial<GitActionPreferences> | null | undefined): GitActionPreferences {
	const p = (parsed ?? {}) as Partial<GitActionPreferences> & Record<string, unknown>;
	const merged: Record<string, unknown> = {
		includeUnstaged: p.includeUnstaged ?? DEFAULT_PREFERENCES.includeUnstaged,
		nextStep: p.nextStep ?? DEFAULT_PREFERENCES.nextStep,
		pullStrategy: p.pullStrategy ?? DEFAULT_PREFERENCES.pullStrategy,
		autoStash: p.autoStash ?? DEFAULT_PREFERENCES.autoStash,
		mergeMethod: p.mergeMethod ?? DEFAULT_PREFERENCES.mergeMethod,
		cleanupAfterMerge: p.cleanupAfterMerge ?? DEFAULT_PREFERENCES.cleanupAfterMerge,
	};
	// Preserve unknown / future keys (forward-compat with newer app versions
	// sharing the same server blob); drop only the known-dead customMessages.
	for (const [k, v] of Object.entries(p)) {
		if (k in merged) continue;
		if (k === 'customMessages') continue;
		merged[k] = v;
	}
	return merged as unknown as GitActionPreferences;
}

// ============================================
// Constants
// ============================================

const STORAGE_KEY = 'ai-shuffle-git-action-preferences';
const SERVER_PREFERENCE_KEY = 'git_action_settings';

// ============================================
// Server sync state (module-level mutable)
// ============================================


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
			return mergePreferences(parsed);
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


/**
 * Save preferences to server (debounced)
 */
function saveToServer(prefs: GitActionPreferences): void {
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

let _state = $state<GitActionPreferences>(initialPreferences);

// ============================================
// Subscribe shim (centralized via store-utils)
// ============================================

const { subscribe, notifySubscribers: _notifySubscribers, update: _update } =
	createSubscribers<GitActionPreferences>(
		() => _state,
		(v) => { _state = v; }
	);

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
	const fresh: GitActionPreferences = { ...DEFAULT_PREFERENCES };
	_state = fresh;
	_notifySubscribers();
	saveToLocalStorage(fresh);
	saveToServer(fresh);
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
 await preferenceSync.refresh(SERVER_PREFERENCE_KEY);
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

	subscribe,

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
preferenceSync.register(SERVER_PREFERENCE_KEY, {
 read: () => _state,
 apply(value) {
  _state = mergePreferences(compatible(DEFAULT_PREFERENCES, value));
  saveToLocalStorage(_state);
  _notifySubscribers();
 }
});
