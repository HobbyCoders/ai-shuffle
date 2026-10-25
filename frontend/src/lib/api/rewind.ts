/**
 * Rewind API types and functions
 */

import { api } from './client';

// ============================================================================
// Rewind Types
// ============================================================================

export interface RewindExecuteResponse {
	success: boolean;
	message: string;
	chat_rewound: boolean;
	files_rewound: boolean;
	messages_removed: number;
	error: string | null;
	partial_success: boolean;  // True if chat rewound but files didn't (when files were requested)
	warning: string | null;    // Warning message for partial success scenarios
	unsent_restored?: boolean;
	restored_prompt?: string;
	restored_upload_paths?: string[];
}

// ============================================================================
// Rewind API Functions
// ============================================================================

/**
 * Execute a rewind operation to a specific checkpoint
 * @param sessionId - The session to rewind
 * @param targetUuid - UUID of the checkpoint to rewind to ('' when using targetOccurrence)
 * @param includeResponse - If true, keep Claude's response to the target message
 * @param targetRole - The role of the target message ("user" or "assistant")
 * @param targetOccurrence - Fallback for uuid-less messages: 0-based occurrence
 *   of the target among the client's user prompts with IDENTICAL content.
 *   Only sent when targetUuid is empty/falsy. The backend resolves the target
 *   by content match against the transcript's user checkpoints and FAILS
 *   CLOSED when nothing matches, so the worst case is a refusal.
 * @param targetContent - The target message's content, sent verbatim — the
 *   anchor the backend matches checkpoints against.
 */
export async function executeRewind(
	sessionId: string,
	targetUuid: string,
	includeResponse: boolean = true,
	targetRole: 'user' | 'assistant' = 'user',
	targetOccurrence: number | null = null,
	targetContent: string | null = null,
	targetNotSentId: string | null = null
): Promise<RewindExecuteResponse> {
	const payload: Record<string, unknown> = {
		target_uuid: targetUuid,
		restore_chat: true,
		// File restore isn't supported: the backend refuses it for every session.
		restore_files: false,
		include_response: includeResponse,
		target_role: targetRole
	};
	if (targetNotSentId) {
		payload.target_not_sent_id = targetNotSentId;
		payload.restore_chat = false;
		payload.restore_files = false;
	} else if (!targetUuid && targetOccurrence != null) {
		payload.target_uuid = '';
		payload.target_occurrence = targetOccurrence;
		payload.target_content = targetContent;
	}
	return api.post<RewindExecuteResponse>(`/commands/rewind/execute/${sessionId}`, payload);
}
