/**
 * Session stores barrel export.
 *
 * Usage:
 *   import { sessionState, sessionPoller, sessionNotifier } from '$lib/stores/session';
 */

// New runes-based state (use this for new code)
export { sessionStateManager, type SessionData, type SessionUsage } from './session-state.svelte';

// Compat shim (writable store interface for existing bridge/derived stores)
export { sessionState, type SessionStateMap } from './session-state';

export { sessionPoller } from './session-poller';
export { sessionNotifier } from './session-notifier';
