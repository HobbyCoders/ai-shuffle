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
import { createSubscribers } from './store-utils';
import { GIT_ACTION_KEYS, type GitActionKey } from '$lib/utils/git-action-prompts';

// ============================================
// Types
// ============================================

type CommitAction = 'commit' | 'push' | 'pr';
type PullStrategy = 'merge' | 'rebase';
type MergeMethod = 'squash' | 'merge' | 'rebase';

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

	// Custom message overrides per git action. When set (non-empty) a custom
	// message replaces the structured default entirely (see git-action-prompts).
	// Absent keys fall back to the built-in default.
	customMessages: Partial<Record<GitActionKey, string>>;
}

const DEFAULT_PREFERENCES: GitActionPreferences = {
	includeUnstaged: true,
	nextStep: 'push',
	pullStrategy: 'rebase',
	autoStash: false,
	mergeMethod: 'squash',
	cleanupAfterMerge: true,
	customMessages: {},
};

/**
 * Sanitize a raw `customMessages` blob from localStorage / server: keep only
 * string values for known action keys, drop everything else.
 */
function sanitizeCustomMessages(raw: unknown): Partial<Record<GitActionKey, string>> {
	const out: Partial<Record<GitActionKey, string>> = {};
	if (raw && typeof raw === 'object') {
		const obj = raw as Record<string, unknown>;
		for (const key of GIT_ACTION_KEYS) {
			const value = obj[key];
			if (typeof value === 'string') out[key] = value;
		}
	}
	return out;
}

/** Merge a partial (localStorage or server) onto the defaults, sanitizing nested fields. */
function mergePreferences(parsed: Partial<GitActionPreferences> | null | undefined): GitActionPreferences {
	return {
		...DEFAULT_PREFERENCES,
		...(parsed ?? {}),
		customMessages: sanitizeCustomMessages(parsed?.customMessages),
	};
}

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

	// Track pending state for flush on unload / debounced localStorage write.
	pendingState = prefs;

	if (serverSyncTimer) {
		clearTimeout(serverSyncTimer);
	}

	serverSyncTimer = setTimeout(async () => {
		serverSyncTimer = null;
		// Persist the local copy on the debounced tick (the typing hot path relies
		// on this instead of a synchronous per-keystroke write).
		saveToLocalStorage(prefs);

		// Don't write to server until initial sync is complete.
		if (!hasSyncedFromServer) return;

		try {
			await setPreference(SERVER_PREFERENCE_KEY, prefs);
			pendingState = null;
		} catch (e) {
			console.error('[GitActionPreferences] Failed to save to server:', e);
		}
	}, SERVER_SYNC_DEBOUNCE_MS);
}

/**
 * Flush pending save immediately (for beforeunload)
 */
function flushPendingSave(): void {
	if (!pendingState || !browser) return;

	if (serverSyncTimer) {
		clearTimeout(serverSyncTimer);
		serverSyncTimer = null;
	}

	// Always persist the local copy first so a quick tab close can't lose the
	// debounced-but-not-yet-written localStorage update from the typing path.
	saveToLocalStorage(pendingState);

	// Only push to the server once the initial sync has completed (mirrors
	// saveToServer) so we never clobber unseen server data on a fast close.
	if (hasSyncedFromServer) {
		const data = JSON.stringify({
			key: SERVER_PREFERENCE_KEY,
			value: pendingState
		});
		navigator.sendBeacon(
			'/api/v1/preferences/' + SERVER_PREFERENCE_KEY,
			new Blob([data], { type: 'application/json' })
		);
	}
	pendingState = null;
}

// Register beforeunload handler (guarded against HMR duplicate registration)
if (browser && !(window as any).__gitActionPrefs_listeners) {
	(window as any).__gitActionPrefs_listeners = true;
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
 * Set (or clear) a custom message override for a git action.
 * A trimmed-empty value deletes the override so the built-in default is used.
 */
function setCustomMessage(action: GitActionKey, value: string): void {
	_update(prefs => {
		const nextMessages = { ...prefs.customMessages };
		if (value.trim().length === 0) {
			delete nextMessages[action];
		} else {
			nextMessages[action] = value;
		}
		// Canonicalize key order so identical data always serializes identically
		// (sanitize orders by GIT_ACTION_KEYS; equality checks use JSON.stringify).
		const updated = { ...prefs, customMessages: sanitizeCustomMessages(nextMessages) };
		// In-memory state updates synchronously (UI unaffected); the localStorage
		// write is debounced alongside the server write on this typing hot path.
		saveToServer(updated);
		return updated;
	});
}

/**
 * Reset all preferences to defaults
 */
function reset(): void {
	// Clone so the shared DEFAULT_PREFERENCES.customMessages object is never mutated.
	const fresh: GitActionPreferences = { ...DEFAULT_PREFERENCES, customMessages: {} };
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
	if (!browser) return;

	const serverState = await loadFromServer();
	const localState = _state;

	// Invariant: never clobber local state while an un-flushed local edit exists.
	// A pending debounced save means local state is strictly newer than the
	// server's and will be pushed by that save, so skip the overwrite entirely
	// (this also races out a resolution that lands mid-typing).
	const hasPendingEdit = serverSyncTimer !== null || pendingState !== null;

	if (serverState && !hasPendingEdit) {
		const merged = mergePreferences(serverState);

		// Both sides are built via the same spread + sanitize (canonical key
		// order) path, so a single whole-object compare is exact.
		if (JSON.stringify(merged) !== JSON.stringify(localState)) {
			saveToLocalStorage(merged);
			_state = merged;
			_notifySubscribers();
		}
	}

	hasSyncedFromServer = true;

	// If no server data and we have non-default local data, push to server.
	if (!serverState) {
		if (JSON.stringify(localState) !== JSON.stringify(DEFAULT_PREFERENCES)) {
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
	setCustomMessage,
};
