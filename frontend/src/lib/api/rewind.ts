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
 * @param restoreFiles - If true, also restore files to checkpoint state (requires file checkpointing enabled)
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
	restoreFiles: boolean = false,
	targetOccurrence: number | null = null,
	targetContent: string | null = null
): Promise<RewindExecuteResponse> {
	const payload: Record<string, unknown> = {
		target_uuid: targetUuid,
		restore_chat: true,
		restore_files: restoreFiles,
		include_response: includeResponse,
		target_role: targetRole
	};
	if (!targetUuid && targetOccurrence != null) {
		payload.target_uuid = '';
		payload.target_occurrence = targetOccurrence;
		payload.target_content = targetContent;
	}
	return api.post<RewindExecuteResponse>(`/commands/rewind/execute/${sessionId}`, payload);
}
