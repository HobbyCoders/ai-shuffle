import { api } from './client';

/** What happened to one chat of a broadcast (see app/api/sessions_broadcast.py). */
export interface BroadcastResult {
	session_id: string;
	/** sent: admitted. held: kept for a move, runs after it. maybe_sent: the
	 *  owning worker may have taken it. failed: refused, it will not run. */
	status: 'sent' | 'held' | 'maybe_sent' | 'failed';
	detail?: string | null;
}

export interface BroadcastResponse {
	results: BroadcastResult[];
	sent: number;
}

/** Send one message to every chat in `sessionIds`, as a normal composer send. */
export function broadcastMessage(sessionIds: string[], prompt: string): Promise<BroadcastResponse> {
	return api.post<BroadcastResponse>('/sessions/broadcast', { session_ids: sessionIds, prompt });
}
