/**
 * Claude Code CLI conversations on this worker that are not AI Shuffle
 * sessions yet, and importing one. Owner only.
 */
import { api } from './client';

export interface CliConversation {
	jsonl_path: string;
	sdk_session_id: string;
	title: string;
	message_count: number;
	/** Seconds since the epoch. */
	modified_at: number;
	cwd: string | null;
	/** The AI Shuffle project the conversation ran in, when one matches. */
	project_id: string | null;
}

export async function listCliConversations(days = 30, limit = 50): Promise<CliConversation[]> {
	const result = await api.get<{ conversations: CliConversation[] }>(`/sessions/import/cli-history?days=${days}&limit=${limit}`);
	return result.conversations;
}

export async function importCliConversation(conversation: CliConversation): Promise<{ session_id: string; status: string }> {
	return api.post<{ session_id: string; status: string }>('/sessions/import-jsonl', {
		jsonl_path: conversation.jsonl_path,
		project_id: conversation.project_id
	});
}
