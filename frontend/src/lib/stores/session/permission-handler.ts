/**
 * Permission Handler — Permission and question response logic.
 *
 * Handles:
 * - sendPermissionResponse / sendUserQuestionResponse
 * - syncPendingPermissions / syncPendingQuestions
 *
 * Plain .ts file — no $state/$derived/$effect runes (see KNOWN_ISSUES.md Root Cause 10).
 */

// ─── Interfaces ──────────────────────────────────────────────────────────────

interface PermissionHandlerDeps {
	wsSend: (data: Record<string, unknown>) => boolean;
}

// ─── Factory ─────────────────────────────────────────────────────────────────

export function createPermissionHandler(deps: PermissionHandlerDeps) {
	const { wsSend } = deps;

	return {
		sendPermissionResponse(
			requestId: string,
			decision: 'allow' | 'deny' | 'defer',
			remember?: 'none' | 'session' | 'profile',
			pattern?: string,
			sessionId?: string
		) {
			// Slice B: when deferring, omit remember/pattern entirely — the backend
			// ignores them for defer decisions, but keeping them out of the payload
			// avoids accidental policy writes if a future backend treats them as
			// fall-through inputs.
			if (decision === 'defer') {
				wsSend({
					type: 'permission_response',
					request_id: requestId,
					session_id: sessionId,
					decision: 'defer',
				});
				return;
			}

			wsSend({
				type: 'permission_response',
				request_id: requestId,
				session_id: sessionId,
				decision,
				remember: remember || 'none',
				pattern,
			});
		},

		/**
		 * Slice B: ask the server to re-broadcast every previously-deferred
		 * permission request as fresh `permission_request` WS events. The
		 * existing handler in ws-message-handlers will pick them up and queue
		 * them in `pendingPermissions` again.
		 */
		requeueDeferredPermissions(sessionId: string) {
			wsSend({
				type: 'requeue_deferred_permissions',
				session_id: sessionId,
			});
		},

		sendUserQuestionResponse(
			requestId: string,
			toolUseId: string,
			answers: Record<string, string | string[]>,
			sessionId?: string
		) {
			wsSend({
				type: 'user_question_response',
				request_id: requestId,
				session_id: sessionId,
				tool_use_id: toolUseId,
				answers,
			});
		},

		syncPendingPermissions() {
			wsSend({ type: 'get_pending_permissions' });
		},

		syncPendingQuestions() {
			wsSend({ type: 'get_pending_questions' });
		},
	};
}
