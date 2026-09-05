import { preferenceSync } from '$lib/services/preferences';
/**
 * Session mode — which lane of the sidepanel the user is looking at.
 *
 * Three surfaces share one app shell:
 * - 'code'  — the full agentic workspace (projects, agents, branches, worktrees)
 * - 'chat'  — a lightweight web-only assistant: model + effort, attachments,
 *             transcription, usage. No repository, no tools beyond web search/fetch.
 * - 'voice' — realtime calls only; new sessions are started from a dedicated hero.
 *
 * This store answers two questions at once: which sessions the sidepanel lists,
 * and which surface a *new* session is created into. Both need the same value,
 * so there is exactly one of it.
 *
 * It mirrors the backend `sessions.mode` column, with one storage wrinkle worth
 * remembering: a code-mode session stores NULL server-side, not the string
 * 'code'. Every row that existed before modes did is therefore already a
 * code-mode row, and no migration is needed. Read `session_modes.py` for the
 * server half of that contract.
 *
 * Svelte 5 runes with the usual subscribe() shim, so `$sessionMode` works in
 * components (see store-utils.ts and theme.svelte.ts for the pattern).
 */

import { browser } from '$app/environment';
import { untrack } from 'svelte';
import { createSubscribers } from './store-utils';

export type SessionMode = 'code' | 'chat' | 'voice';

export const SESSION_MODES: readonly SessionMode[] = ['code', 'chat', 'voice'];

export const DEFAULT_SESSION_MODE: SessionMode = 'code';

const STORAGE_KEY = 'ai-shuffle.sessionMode';

/**
 * Coerce anything — a stored string, a `sessions.mode` value off the wire, a
 * URL param — into a mode. NULL is the code lane's storage representation, so
 * an absent value must resolve to 'code' rather than to an error state.
 */
export function normalizeSessionMode(value: unknown): SessionMode {
	if (typeof value !== 'string') return DEFAULT_SESSION_MODE;
	const trimmed = value.trim().toLowerCase();
	return (SESSION_MODES as readonly string[]).includes(trimmed)
		? (trimmed as SessionMode)
		: DEFAULT_SESSION_MODE;
}

function getStoredMode(): SessionMode {
	if (!browser) return DEFAULT_SESSION_MODE;
	try {
		return normalizeSessionMode(localStorage.getItem(STORAGE_KEY));
	} catch {
		// Private-mode / blocked storage: the mode is a preference, not state we
		// can't run without.
		return DEFAULT_SESSION_MODE;
	}
}

let _mode = $state<SessionMode>(getStoredMode());
let _defaultMode = untrack(() => _mode);
let followingDefault = true;

const { subscribe, notifySubscribers: _notifySubscribers, createDerived } =
	createSubscribers<SessionMode>(
		() => _mode,
		(v) => { _mode = v; }
	);

export const appMode = {
	get current(): SessionMode {
		return _mode;
	},

	subscribe,

	/** Switch the sidepanel to `mode`. Unknown values fall back to 'code'. */
	set(mode: SessionMode, persist = true) {
		const next = normalizeSessionMode(mode);
 followingDefault = persist;
 if (persist) { _defaultMode = next; preferenceSync.save('session_mode', next); }
		if (next === _mode) return;

		if (browser && persist) {
			try {
				localStorage.setItem(STORAGE_KEY, next);
			} catch {
				// Non-fatal — the switch still applies for this page load.
			}
		}

		_mode = next;
		_notifySubscribers();
	},

	/** Re-read the persisted mode. Only useful after storage is cleared. */
	reload() {
		const next = getStoredMode();
		if (next === _mode) return;
		_mode = next;
		_notifySubscribers();
	}
};

/** `$sessionMode` in a component yields the current SessionMode. */
export const sessionMode = createDerived(() => _mode);
preferenceSync.register('session_mode', {
 read: () => _defaultMode,
 apply(value) { _defaultMode = normalizeSessionMode(value); if (followingDefault || value === undefined) _mode = _defaultMode; if (browser) localStorage.setItem(STORAGE_KEY, _defaultMode); _notifySubscribers(); }
});
