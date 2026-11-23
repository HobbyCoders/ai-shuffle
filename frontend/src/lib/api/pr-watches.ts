/**
 * PR watch: the app polls a pull request in plain code and wakes the chat that
 * opened it only when CI fails, a reviewer with write access comments, or the
 * branch conflicts. The limits live in Settings > Usage > PR watch.
 */
import { api } from './client';

export type PrWatchMode = 'fix' | 'triage' | 'notify';

export interface PrWatchLimits {
	mode: PrWatchMode;
	max_wakes_per_day: number;
	max_usd_per_day: number;
	idle_days: number;
	max_repeat_failures: number;
	trusted_bots: string[];
}

export interface PrWatchSettings extends PrWatchLimits {
	defaults: PrWatchLimits;
	limits: Record<'max_wakes_per_day' | 'max_usd_per_day' | 'idle_days' | 'max_repeat_failures', [number, number]>;
}

export interface PrWatch {
	id: string;
	repo: string;
	pr_number: number;
	pr_url: string | null;
	pr_title: string | null;
	session_id: string | null;
	status: 'active' | 'stopped' | 'ended';
	reason: string | null;
	wakes_today: number;
	spent_today_usd: number;
	needs_owner: boolean;
	pending: number;
	last_error: string | null;
	last_wake_at: string | null;
	created_at: string | null;
	updated_at: string | null;
}

export function getPrWatchSettings(): Promise<PrWatchSettings> {
	return api.get('/settings/pr-watch', { cache: 'no-store' });
}

export function updatePrWatchSettings(patch: Partial<PrWatchLimits>): Promise<PrWatchSettings> {
	return api.put('/settings/pr-watch', patch);
}

export function listPrWatches(): Promise<{ watches: PrWatch[] }> {
	return api.get('/pr-watches', { cache: 'no-store' });
}

export interface StartPrWatch {
	repo: string;
	pr_number: number;
	pr_url?: string | null;
	pr_title?: string | null;
	/** The chat to wake; omitted, the server finds the chat that opened the PR. */
	session_id?: string | null;
}

export function startPrWatch(body: StartPrWatch): Promise<PrWatch> {
	return api.post('/pr-watches', body);
}

/** The stopped watch, or null when the response had no body. */
export async function stopPrWatch(id: string): Promise<PrWatch | null> {
	return (await api.delete<PrWatch>(`/pr-watches/${encodeURIComponent(id)}`)) || null;
}
