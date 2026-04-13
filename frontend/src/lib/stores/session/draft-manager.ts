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
import type { PollAppliedData } from './session-poller';

// ─── Interfaces ──────────────────────────────────────────────────────────────

export interface DraftManagerDeps {
	update: (fn: (s: ActiveSessionState) => ActiveSessionState) => void;
	getState: () => ActiveSessionState;
	wsSend: (data: Record<string, unknown>) => boolean;
	wsIsConnected: () => boolean;
	sessionStateRemoveSession: (sessionId: string) => void;
	sessionPollerLoadSession: (sessionId: string) => Promise<PollAppliedData | null>;
}

// ─── Factory ─────────────────────────────────────────────────────────────────

export function createDraftManager(deps: DraftManagerDeps) {
	const { update, getState, wsSend, wsIsConnected, sessionStateRemoveSession, sessionPollerLoadSession } = deps;

	return {
		setDraft(text: string) {
			update(s => ({ ...s, draft: text }));
		},

		clearPendingInterruptPrompt() {
			update(s => s.pendingInterruptPrompt ? { ...s, pendingInterruptPrompt: null } : s);
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
			update(s => ({ ...s, messages: [], todos: [] }));
		},

		/** Reload the current session from backend (e.g. after rewind).
		 * Unlike WS-reconnect reloads, this is an explicit user action that
		 * should do a full reset to pick up truncated/changed data. */
		async reloadSession(_forceJsonl = false) {
			const state = getState();
			if (state.sessionId) {
				// Force a fresh fetch by removing stale session state first
				sessionStateRemoveSession(state.sessionId);
				await sessionPollerLoadSession(state.sessionId);
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

		/** Rewind session to a specific message. After rewind completes externally
		 * (via MessageActions component), reload the session to pick up the
		 * truncated message list. */
		async rewindToMessage(_messageId: string) {
			const state = getState();
			if (!state.sessionId) return;
			// Force reset since the JSONL was modified by the rewind.
			sessionStateRemoveSession(state.sessionId);
			await sessionPollerLoadSession(state.sessionId);
		},

		/** Take over a locked session from another device. */
		async takeOverSession() {
			const state = getState();
			if (!state.sessionId) return;
			update(s => ({ ...s, error: null, sessionLocked: false, sessionLockedBy: undefined }));
			// Send takeover via WS (to acquire the lock), then load state via HTTP
			if (wsIsConnected()) {
				wsSend({ type: 'load_session', session_id: state.sessionId, force: true });
			}
			// Force reset — takeover may load different state from another device
			sessionStateRemoveSession(state.sessionId);
			await sessionPollerLoadSession(state.sessionId);
		},
	};
}
