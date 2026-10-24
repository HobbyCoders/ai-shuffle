/**
 * Watches (#371 step 3): durable background agents that poll a source (mail,
 * GitHub, a session, usage, a schedule) and fire into a chat when it changes.
 *
 * Created only in conversation (CreateWatch, confirmed by the owner); the UI
 * lists them in the running-tasks panel and pauses, resumes or stops them.
 * Stop is terminal.
 */
import { api } from './client';

export type WatchStatus = 'active' | 'paused' | 'stopped';

/** One row of GET /api/v1/watches: the stored watch plus render fields. */
export interface WatchRow {
	id: string;
	kind: 'watch';
	/** What it watches, in plain words. */
	what: string;
	description?: string;
	status: WatchStatus;
	/** Why it paused itself (flood, failures, budget), or the last failure. */
	reason?: string | null;
	last_fired_at: string | null;
	/** Total spent since it was created, in USD. */
	cost_usd: number;
	daily_budget_usd?: number | null;
	creating_session_id?: string | null;
	worker_id?: string | null;
	ends_at?: string | null;
	created_at?: string | null;
	can_pause: boolean;
	can_stop: boolean;
}

export async function listWatches(): Promise<WatchRow[]> {
	const body = await api.get<{ watches?: WatchRow[] }>('/watches', { cache: 'no-store' });
	return Array.isArray(body?.watches) ? body.watches : [];
}

/** Pause, resume (`active`) or stop a watch. Returns the stored row. */
export function setWatchStatus(id: string, status: WatchStatus): Promise<WatchRow> {
	return api.patch(`/watches/${encodeURIComponent(id)}`, { status });
}
