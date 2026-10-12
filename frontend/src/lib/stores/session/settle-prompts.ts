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

export function dismissSettledSessionPrompts(sessionId: string | null | undefined, deps: SettlePromptDeps): void {
	if (!sessionId) return;
	deps.backgroundPermissions.update((map) => {
		if (!map.has(sessionId)) return map;
		const next = new Map(map);
		next.delete(sessionId);
		return next;
	});
	if (deps.getState().sessionId !== sessionId) return;
	deps.update((s) => {
		if (
			s.sessionId !== sessionId ||
			(s.pendingPermissions.length === 0 &&
				s.deferredPermissions.length === 0 &&
				s.pendingQuestions.length === 0)
		) {
			return s;
		}
		return { ...s, pendingPermissions: [], deferredPermissions: [], pendingQuestions: [] };
	});
}
