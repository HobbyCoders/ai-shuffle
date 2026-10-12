/**
 * Settling a session dismisses whatever it waits on (#2324): the server denies
 * the pending permission and cancels the pending question, so this client
 * drops its local copies of those waits as well. Without this the card keeps
 * an amber status from the `backgroundPermissions` map and, if the session is
 * open, its permission/question card keeps blocking the composer.
 *
 * Called on a confirmed settle only (the sidebar/context-menu action once the
 * PATCH succeeds, and a `user_session_updated` / `session_updated` frame that
 * reports `is_active: false`). Unsettling clears nothing: the session starts
 * fresh and any new prompt arrives through the usual frames.
 */
import type { ActiveSessionState } from '../active-session';
import type { SessionAttention } from './ws-message-handlers';

export interface SettlePromptDeps {
	backgroundPermissions: {
		update: (fn: (m: Map<string, SessionAttention>) => Map<string, SessionAttention>) => void;
	};
	getState: () => ActiveSessionState;
	update: (fn: (s: ActiveSessionState) => ActiveSessionState) => void;
}

/**
 * What a settle may dismiss once its PATCH resolves: the attention entry and
 * prompt request ids that existed when the settle was sent. The settle frame
 * may land first, and by the time the PATCH resolves another device may have
 * unsettled the session and started a turn with a NEW prompt; that prompt must
 * survive the late clear.
 */
export interface SettlePromptSnapshot {
	attention: SessionAttention | undefined;
	requestIds: ReadonlySet<string>;
}

export function snapshotSessionPrompts(
	sessionId: string,
	attention: ReadonlyMap<string, SessionAttention>,
	state: ActiveSessionState
): SettlePromptSnapshot {
	const requestIds = new Set<string>();
	if (state.sessionId === sessionId) {
		for (const p of state.pendingPermissions) if (p.request_id) requestIds.add(p.request_id);
		for (const p of state.deferredPermissions) if (p.request_id) requestIds.add(p.request_id);
		for (const q of state.pendingQuestions) if (q.request_id) requestIds.add(q.request_id);
	}
	return { attention: attention.get(sessionId), requestIds };
}

/**
 * Drop the session's local waits. Without `only`, everything the session
 * holds goes (a settle frame is current by definition). With `only`, just
 * what the snapshot saw: the attention entry if it is still the same object
 * (every change writes a new one), and prompts whose request id it recorded.
 */
export function dismissSettledSessionPrompts(
	sessionId: string | null | undefined,
	deps: SettlePromptDeps,
	only?: SettlePromptSnapshot
): void {
	if (!sessionId) return;
	deps.backgroundPermissions.update((map) => {
		if (!map.has(sessionId)) return map;
		if (only && map.get(sessionId) !== only.attention) return map;
		const next = new Map(map);
		next.delete(sessionId);
		return next;
	});
	if (deps.getState().sessionId !== sessionId) return;
	const keep = <T extends { request_id?: string }>(items: T[]): T[] =>
		only ? items.filter((item) => !item.request_id || !only.requestIds.has(item.request_id)) : [];
	deps.update((s) => {
		if (s.sessionId !== sessionId) return s;
		const pendingPermissions = keep(s.pendingPermissions);
		const deferredPermissions = keep(s.deferredPermissions);
		const pendingQuestions = keep(s.pendingQuestions);
		if (
			pendingPermissions.length === s.pendingPermissions.length &&
			deferredPermissions.length === s.deferredPermissions.length &&
			pendingQuestions.length === s.pendingQuestions.length
		) {
			return s;
		}
		return { ...s, pendingPermissions, deferredPermissions, pendingQuestions };
	});
}
