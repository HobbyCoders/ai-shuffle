/**
 * Usage limits and costly sends (#1831): the per-provider "Resume when the
 * limit resets" switches, a chat's scheduled resume and its Cancel, Compact
 * now, and the cold-cache check the composer runs before a send.
 */
import { api } from './client';

export interface LimitResumeSwitches {
	resume_on_reset: boolean;
	compact_before_resume: boolean;
}

export interface LimitResumeSettings {
	/** Keyed by `claude` (Claude Code) or a model provider's id. */
	providers: Record<string, LimitResumeSwitches>;
	defaults: LimitResumeSwitches;
}

export function getLimitResume(): Promise<LimitResumeSettings> {
	return api.get('/settings/limit-resume', { cache: 'no-store' });
}

export function updateLimitResume(
	providerKey: string,
	patch: Partial<LimitResumeSwitches>
): Promise<LimitResumeSettings> {
	return api.put(`/settings/limit-resume/${encodeURIComponent(providerKey)}`, patch);
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

/** Compact a chat's context now (a `/compact` turn). 409 while a turn runs. */
export function compactSession(sessionId: string): Promise<{ turn_id: string; session_id: string }> {
	return api.post(`/sessions/${encodeURIComponent(sessionId)}/compact`);
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
}

export function getCacheState(sessionId: string): Promise<CacheState> {
	return api.get(`/sessions/${encodeURIComponent(sessionId)}/cache-state`, { cache: 'no-store' });
}
