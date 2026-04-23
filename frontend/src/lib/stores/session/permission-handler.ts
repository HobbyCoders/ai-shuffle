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

export interface PermissionHandlerDeps {
	wsSend: (data: Record<string, unknown>) => boolean;
}

// ─── Factory ─────────────────────────────────────────────────────────────────

export function createPermissionHandler(deps: PermissionHandlerDeps) {
	const { wsSend } = deps;

	return {
		sendPermissionResponse(
			requestId: string,
			decision: 'allow' | 'deny',
			remember?: 'none' | 'session' | 'profile',
			pattern?: string,
			sessionId?: string
		) {
			wsSend({
				type: 'permission_response',
				request_id: requestId,
				session_id: sessionId,
				decision,
				remember: remember || 'none',
				pattern,
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
