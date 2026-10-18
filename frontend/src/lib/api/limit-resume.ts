/**
 * Usage limits and costly sends (#1831): the "Resume when the limit resets"
 * and "Compact before resuming" switches (one pair for every provider), a
 * chat's scheduled resume and its Cancel, Compact now, and the cold-cache
 * check the composer runs before a send.
 */
import { api } from './client';

export interface LimitResumeSwitches {
	resume_on_reset: boolean;
	compact_before_resume: boolean;
}

/** The switches, which apply to every provider, and their defaults. */
export interface LimitResumeSettings extends LimitResumeSwitches {
	defaults: LimitResumeSwitches;
}

export function getLimitResume(): Promise<LimitResumeSettings> {
	return api.get('/settings/limit-resume', { cache: 'no-store' });
}

export function updateLimitResume(patch: Partial<LimitResumeSwitches>): Promise<LimitResumeSettings> {
	return api.put('/settings/limit-resume', patch);
}

export interface CancelLimitResumeResult {
	session_id: string;
	task_id: string | null;
	cancelled: boolean;
}

/** Cancel a chat's scheduled resume, or that of one of its paused workers. */
export function cancelLimitResume(sessionId: string, taskId?: string | null): Promise<CancelLimitResumeResult> {
	const query = taskId ? `?task_id=${encodeURIComponent(taskId)}` : '';
	return api.post(`/sessions/${encodeURIComponent(sessionId)}/limit-resume/cancel${query}`);
}

/** Compact now, optionally queueing the normal chat query as the next turn.
 * The queued message still runs on the original context if compaction fails.
 */
export function compactSession(sessionId: string, thenSend?: Record<string, unknown>): Promise<{
	turn_id: string; session_id: string; queued_turn_id?: string;
}> {
	return api.post(`/sessions/${encodeURIComponent(sessionId)}/compact`,
		thenSend ? { then_send: thenSend } : undefined);
}

/** API-price estimates for compaction plus one resumed call, not the whole turn. */
export interface SendCostComparison {
	model: string;
	cold_resume_usd: number;
	compaction_usd: number;
	continued_usd: number;
	compact_continue_usd: number;
	compacted_context_tokens: number;
	reply_tokens: number;
	summary_tokens: number;
	recommendation: 'compact' | 'send';
}

export interface CacheState {
	leg: string | null;
	context_tokens: number | null;
	last_call_at: number | null;
	idle_seconds: number | null;
	cache_ttl_seconds: number | null;
	threshold_tokens: number;
	cold: boolean;
	large: boolean;
	warn: boolean;
	cost_comparison?: SendCostComparison | null;
}

export function getCacheState(sessionId: string): Promise<CacheState> {
	return api.get(`/sessions/${encodeURIComponent(sessionId)}/cache-state`, { cache: 'no-store' });
}
