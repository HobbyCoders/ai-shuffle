/**
 * Draft Manager — Ephemeral UI state, system messages, rewind, and session takeover.
 *
 * Handles:
 * - Draft text, scroll position, error clearing
 * - System message injection
 * - Pending interrupt prompt clearing
 * - Session rewind and reload
 * - Session takeover (lock acquisition)
 *
 * Plain .ts file — no $state/$derived/$effect runes (see KNOWN_ISSUES.md Root Cause 10).
 */

import type { ActiveSessionState } from '../active-session';
import type { MessageType } from '../types';

// ─── Interfaces ──────────────────────────────────────────────────────────────

interface DraftManagerDeps {
	update: (fn: (s: ActiveSessionState) => ActiveSessionState) => void;
	getState: () => ActiveSessionState;
	wsSend: (data: Record<string, unknown>) => boolean;
	wsIsConnected: () => boolean;
	/** Explicit full from_seq=0 hydration for user-requested reloads. */
	feedRestart: (sessionId: string) => void;
	/** Gap-free resume at the current cursor; does not reset transcript state. */
	feedRevive: (sessionId: string) => void;
}

// ─── Factory ─────────────────────────────────────────────────────────────────

export function createDraftManager(deps: DraftManagerDeps) {
	const { update, getState, wsSend, wsIsConnected, feedRestart, feedRevive } = deps;

	return {
		setDraft(text: string) {
			update(s => ({ ...s, draft: text }));
		},

		setPendingRestorePrompt(prompt: string) {
			update(s => ({ ...s, pendingRestorePrompt: prompt }));
		},

		clearPendingRestorePrompt() {
			update(s => s.pendingRestorePrompt ? { ...s, pendingRestorePrompt: null } : s);
		},

		setScrollTop(pos: number) {
			update(s => ({ ...s, scrollTop: pos }));
		},

		clearError() {
			update(s => ({ ...s, error: null }));
		},

		clearWorktreeError() {
			update(s => ({ ...s, worktreeError: null }));
		},

		/** Clear all messages (e.g. before reloading after rewind). */
		clearMessages() {
			update(s => ({ ...s, messages: [] }));
		},

		/** Reload the current session from backend (e.g. after rewind).
		 * Restarts the SSE feed, which replays the event log from_seq=0 — a full
		 * reset that picks up truncated/changed data. */
		reloadSession(_forceJsonl = false) {
			const state = getState();
			if (state.sessionId) {
				feedRestart(state.sessionId);
			}
		},

		addSystemMessage(content: string, subtype?: string) {
			update(s => ({
				...s,
				messages: [...s.messages, {
					id: `system-${Date.now()}`,
					role: 'system' as const,
					content,
					type: 'system' as MessageType,
					systemSubtype: subtype,
				}]
			}));
		},

		/** @deprecated Use clearError() instead. Kept for backward compatibility. */
		acknowledgeError() {
			this.clearError();
		},

		/** Rewind session to a specific message. The rewind command is executed
		 * externally (via MessageActions → executeRewind HTTP), and the backend
		 * emits a `rewound` event on the session's SSE event log; the live feed's
		 * reducer folds that truncation into the transcript. No client-side
		 * truncation or reload is required here. */
		rewindToMessage(_messageId: string) {
			// Intentionally a no-op: truncation arrives via the live SSE `rewound`
			// event. Kept for API compatibility.
		},

		/** Take over a locked session from another device. */
		takeOverSession() {
			const state = getState();
			if (!state.sessionId) return;
			update(s => ({ ...s, error: null, sessionLocked: false, sessionLockedBy: undefined }));
			// Send takeover via WS to acquire the lock. SSE is already the transcript
			// authority, so resume it at lastSeq instead of resetting and replaying the
			// same session from zero a second time.
			if (wsIsConnected()) {
				wsSend({ type: 'load_session', session_id: state.sessionId, force: true });
			}
			feedRevive(state.sessionId);
		},
	};
}
