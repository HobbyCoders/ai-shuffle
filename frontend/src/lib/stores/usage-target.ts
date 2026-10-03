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
 *      target = `row.owner_device_id ?? row.instance_id ?? null` (null →
 *      local/self). On a worker network each device keeps its own database and
 *      a mirrored or handed-off row's `instance_id` can be null or name another
 *      install, so the owning device id comes first; the usage store matches it
 *      against each bucket's `deviceId`.
 *   2. No sessionId (new chat) → self (null).
 *   3. Row not found → self (null).
 *
 * Plain `.ts` (no $state runes) so it can be imported before component init.
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
	active: Pick<ActiveSessionState, 'sessionId'>,
	sessionRows: Pick<Session, 'id' | 'instance_id' | 'owner_device_id'>[]
): string | null {
	if (active.sessionId) {
		const row = sessionRows.find((r) => r.id === active.sessionId);
		if (!row) return null; // Not in the list (yet) → assume local/self.
		return row.owner_device_id || row.instance_id || null;
	}
	return null;
}

/** The viewed session's owning-device name, for "nothing reported from X" lines. */
export function resolveTargetDeviceName(
	active: Pick<ActiveSessionState, 'sessionId'>,
	sessionRows: Pick<Session, 'id' | 'device_name'>[]
): string | null {
	if (!active.sessionId) return null;
	return sessionRows.find((r) => r.id === active.sessionId)?.device_name ?? null;
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

const _deviceName = createStore<string | null>(
	resolveTargetDeviceName(activeSession.current, sessions.current)
);
function _recomputeDeviceName() {
	const next = resolveTargetDeviceName(activeSession.current, sessions.current);
	if (next !== _deviceName.current) _deviceName.set(next);
}
activeSession.subscribe(() => _recomputeDeviceName());
sessions.subscribe(() => _recomputeDeviceName());

/** The owning device's name for the viewed session (null = unknown/new chat). */
export const usageTargetDeviceName = {
	get current(): string | null {
		return _deviceName.current;
	},
	subscribe: _deviceName.subscribe,
};
