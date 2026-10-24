/**
 * Watches (#371 step 3): durable background agents that poll a source (mail,
 * GitHub, a session, usage, a schedule) and fire into a chat when it changes.
 *
 * Created only in conversation (CreateWatch, confirmed by the owner); the UI
 * lists them in the running-tasks panel and pauses, resumes or stops them.
 * Stop is terminal.
 *
 * Placement (#565): every row names the worker that executes it. The owner can
 * set where new watches run (`PUT /watches/preference`) and move one
 * (`PATCH /watches/{id}` with `worker_id`). A move fences the old worker first;
 * a 409 is a refusal (with the reason), a 503 means the outcome is unknown.
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
	/**
	 * The executing worker's install id, or `transfer:<device id>` while a
	 * move to that worker is unfinished (the row is fenced: it does not run).
	 */
	worker_id?: string | null;
	/** The network device that reported the row: where it executes. */
	placement_device_id?: string | null;
	ends_at?: string | null;
	created_at?: string | null;
	can_pause: boolean;
	can_stop: boolean;
}

export interface WatchListing {
	watches: WatchRow[];
	/** Where new watches run; `null` is automatic (an always-on worker). */
	preferredWorkerId: string | null;
}

export async function listWatches(): Promise<WatchListing> {
	const body = await api.get<{ watches?: WatchRow[]; preferred_worker_id?: string | null }>('/watches', {
		cache: 'no-store'
	});
	return {
		watches: Array.isArray(body?.watches) ? body.watches : [],
		preferredWorkerId: body?.preferred_worker_id || null
	};
}

/** Pause, resume (`active`) or stop a watch. Returns the stored row. */
export function setWatchStatus(id: string, status: WatchStatus): Promise<WatchRow> {
	return api.patch(`/watches/${encodeURIComponent(id)}`, { status });
}

/**
 * Move a watch to another worker. Resolves with the destination's row.
 *
 * `confirmOnly` finishes a move that already landed on `workerId` (closing
 * the old worker's transfer receipt) and never starts a transfer: it is
 * refused with 409 when the watch is anywhere else.
 */
export function moveWatch(id: string, workerId: string, confirmOnly = false): Promise<WatchRow> {
	const body = confirmOnly ? { worker_id: workerId, confirm: true } : { worker_id: workerId };
	return api.patch(`/watches/${encodeURIComponent(id)}`, body);
}

/** Where new watches run: a worker id, or `null` for automatic. */
export async function setWatchPreference(workerId: string | null): Promise<string | null> {
	const body = await api.put<{ preferred_worker_id?: string | null }>('/watches/preference', { worker_id: workerId });
	return body?.preferred_worker_id || null;
}
