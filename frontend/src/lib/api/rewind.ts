/**
 * Rewind API types and functions
 */

import { api } from './client';

// ============================================================================
// Rewind Types
// ============================================================================

export interface RewindCheckpoint {
	uuid: string;
	index: number;
	message_preview: string;
	full_message: string;
	role: 'user' | 'assistant';
	timestamp: string | null;
}

export interface RewindCheckpointsResponse {
	success: boolean;
	session_id: string;
	sdk_session_id: string | null;
	checkpoints: RewindCheckpoint[];
	error: string | null;
}

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
 * Get available rewind checkpoints for a session
 */
export async function getRewindCheckpoints(sessionId: string): Promise<RewindCheckpointsResponse> {
	return api.get<RewindCheckpointsResponse>(`/commands/rewind/checkpoints/${sessionId}`);
}

/**
 * Execute a rewind operation to a specific checkpoint
 * @param sessionId - The session to rewind
 * @param targetUuid - UUID of the checkpoint to rewind to
 * @param includeResponse - If true, keep Claude's response to the target message
 * @param targetRole - The role of the target message ("user" or "assistant")
 * @param restoreFiles - If true, also restore files to checkpoint state (requires file checkpointing enabled)
 */
export async function executeRewind(
	sessionId: string,
	targetUuid: string,
	includeResponse: boolean = true,
	targetRole: 'user' | 'assistant' = 'user',
	restoreFiles: boolean = false
): Promise<RewindExecuteResponse> {
	return api.post<RewindExecuteResponse>(`/commands/rewind/execute/${sessionId}`, {
		target_uuid: targetUuid,
		restore_chat: true,
		restore_files: restoreFiles,
		include_response: includeResponse,
		target_role: targetRole
	});
}
