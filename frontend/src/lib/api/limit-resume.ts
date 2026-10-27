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

/** The chat's last turn, when a usage limit stopped it. Times are epoch seconds. */
export interface LimitStopped {
	turn_id: string;
	provider: string | null;
	reset_at: number | null;
	reset_known: boolean;
	/** True when "Resume at reset" may arm this stop now. */
	can_arm: boolean;
	/** Why it cannot arm: `reset_unknown`, `already_handled`, `busy`, ... */
	reason: string | null;
}

/** A chat's limit-resume state. The worker's answer is authoritative after a reload. */
export interface SessionLimitResume {
	session_id: string;
	pending: Record<string, unknown> | null;
	workers?: unknown;
	/** Absent on workers that predate the one-shot resume. */
	limit_stopped?: LimitStopped | null;
	armed?: boolean;
	resume_at?: number | null;
}

export function getSessionLimitResume(sessionId: string): Promise<SessionLimitResume> {
	return api.get(`/sessions/${encodeURIComponent(sessionId)}/limit-resume`, { cache: 'no-store' });
}

/** Resume this chat's limit-stopped turn once at the reset. Global switches stay as they are. */
export function armLimitResume(sessionId: string, turnId: string): Promise<SessionLimitResume> {
	return api.post(`/sessions/${encodeURIComponent(sessionId)}/limit-resume/arm`, { turn_id: turnId });
}

/** Compact now, optionally queueing the normal chat query as the next turn.
 * The queued message still runs on the original context if compaction fails.
 */
export function compactSession(sessionId: string, thenSend?: Record<string, unknown>): Promise<{
	turn_id: string | null; session_id: string; queued_turn_id?: string; held?: boolean;
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
	/** null means no idle expiry (or no cache for this leg). */
	cache_ttl_seconds: number | null;
	cache_write_ttl: '1h' | '5m' | null;
	threshold_tokens: number;
	cold: boolean;
	large: boolean;
	warn: boolean;
	cost_comparison?: SendCostComparison | null;
}

export function getCacheState(sessionId: string): Promise<CacheState> {
	return api.get(`/sessions/${encodeURIComponent(sessionId)}/cache-state`, { cache: 'no-store' });
}
