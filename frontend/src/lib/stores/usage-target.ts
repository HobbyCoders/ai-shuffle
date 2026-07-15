/**
 * Usage target — resolves WHICH install's usage bucket the widget should show
 * for the currently viewed/composed session.
 *
 * The 5-hour usage window is per Claude account, and in a clustered deployment
 * each install runs a different account. A session can be offloaded to another
 * install, so the usage a viewer cares about is the OWNING install's — not
 * necessarily the local one. This store derives that owning `instance_id`
 * (null = local/self) and updates whenever the active session or the sessions
 * list changes, so the pill follows session switches.
 *
 * Resolution order:
 *   1. `activeSession.sessionId` set → look the row up in the sessions store;
 *      target = `row.instance_id ?? null` (null → local/self).
 *   2. No sessionId (new chat) → self (null).
 *   3. Row not found → self (null).
 *
 * Plain `.ts` (no $state runes) — see KNOWN_ISSUES.md Root Cause 10.
 */

import { createStore } from './store-utils';
import { activeSession, type ActiveSessionState } from './active-session';
import { sessions } from './app-data.svelte';
import type { Session } from '$lib/api/sessions';

/**
 * Pure resolver — exported for tests. Takes just the fields it needs so it can
 * be exercised without standing up the whole store graph.
 */
export function resolveTargetInstance(
	active: Pick<ActiveSessionState, 'sessionId' | 'executionMode'>,
	sessionRows: Pick<Session, 'id' | 'instance_id'>[]
): string | null {
	if (active.sessionId) {
		const row = sessionRows.find((r) => r.id === active.sessionId);
		if (!row) return null; // Not in the list (yet) → assume local/self.
		return row.instance_id ?? null;
	}
	return null;
}

function _compute(): string | null {
	return resolveTargetInstance(activeSession.current, sessions.current);
}

const _store = createStore<string | null>(_compute());

function _recompute() {
	const next = _compute();
	if (next !== _store.current) _store.set(next);
}

// React to both source stores. These are module-level singletons; the
// subscriptions live for the app's lifetime (no teardown needed).
activeSession.subscribe(() => _recompute());
sessions.subscribe(() => _recompute());

/** The owning install id for the viewed/composed session (null = local/self). */
export const usageTargetInstance = {
	get current(): string | null {
		return _store.current;
	},
	subscribe: _store.subscribe,
};
