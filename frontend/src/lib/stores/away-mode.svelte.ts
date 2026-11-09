/**
 * Away mode per session (app/core/away_mode.py): while on, the agent's
 * questions are answered with their recommended option and approval prompts
 * are deferred, never granted. It ends with a "Decisions made for you" table.
 *
 * The flag lives on the owning worker's session row; other workers mirror it
 * with the session catalog. This store caches it per session, flips it through
 * PUT /sessions/{id}/away-mode (routed to the owning worker), and follows the
 * session list rows, which reload when another device or worker changes it.
 *
 * Away for all chats (app/core/global_away_mode.py) is a separate, owner-only
 * switch shared by every worker. It never writes a chat's own flag, and a chat's
 * row never reports it: `isChatAway` is the chat's own flag, `isEffectivelyAway`
 * is what the chat's agent actually obeys. Display reads the second one; only
 * the per-chat toggle reads the first.
 */
import { api } from '$lib/api/client';

/**
 * Performance kill switch VITE_PERF_SLIM_SESSION_READ (default on; see
 * "Performance flags" in docs/internal/DEVELOPMENT.md). `0` restores reading
 * the whole `GET /sessions/{id}` (transcript included) for one field.
 */
export function slimSessionReadEnabled(): boolean {
	return import.meta.env.VITE_PERF_SLIM_SESSION_READ !== '0';
}

/** The session row fields away mode and the private-access panel read. */
export interface SessionOwnerMeta {
	mode?: string | null;
	away_mode?: boolean | null;
	is_worker?: boolean;
	is_remote?: boolean;
	device_name?: string | null;
}

/**
 * The row fields without the transcript: `/live` metadata (about 20 ms) in
 * place of `GET /sessions/{id}` (megabytes on a long chat). Concurrent callers
 * share one request. A worker too old to report a field gets the full read.
 */
export async function getSessionOwnerMeta(sessionId: string, field: keyof SessionOwnerMeta): Promise<SessionOwnerMeta> {
	const full = () => api.get<SessionOwnerMeta>(`/sessions/${encodeURIComponent(sessionId)}`);
	if (!slimSessionReadEnabled()) return full();
	const live = await api.getShared<{ session_meta?: SessionOwnerMeta }>(
		`/sessions/${encodeURIComponent(sessionId)}/live?include_messages=false&owner_fields=true`
	);
	const meta = live?.session_meta;
	return meta && field in meta ? meta : full();
}

/** How long a just-confirmed change outranks a session row that disagrees.
 *  A session owned by another worker answers the PUT before its mirror here
 *  has caught up, so a list reload in that gap still carries the old value. */
export const AWAY_CONFIRM_GRACE_MS = 30_000;

/** What `PUT /away-mode` did to questions and approvals already waiting. */
export interface GlobalAwayResult {
	away: boolean;
	released_questions: number;
	deferred_approvals: number;
}

function counted(count: number, noun: string): string {
	return `${count} ${noun}${count === 1 ? '' : 's'}`;
}

/** The confirmation after turning Away for all chats on. */
export function globalAwayOnMessage(result: Pick<GlobalAwayResult, 'released_questions' | 'deferred_approvals'>): string {
	const done = [
		result.released_questions > 0 ? `answered ${counted(result.released_questions, 'waiting question')}` : '',
		result.deferred_approvals > 0 ? `put off ${counted(result.deferred_approvals, 'approval')}` : ''
	].filter(Boolean).join(' and ');
	if (!done) return 'Away for all chats is on.';
	return `Away for all chats is on. ${done[0].toUpperCase()}${done.slice(1)}.`;
}

class AwayModeStore {
	values = $state<Record<string, boolean>>({});
	busy = $state<Record<string, boolean>>({});
	#loaded = new Set<string>();
	#confirmed = new Map<string, { away: boolean; until: number }>();
	/** The latest value each session's row reported, and a pending re-check at
	 *  the end of a grace period. The composer only calls observe() when the
	 *  row's value changes, so a row that disagreed during the grace would
	 *  otherwise never be applied. */
	#rows = new Map<string, boolean | null>();
	#settle = new Map<string, ReturnType<typeof setTimeout>>();

	/** Away for all chats, as last confirmed by the worker. False until loaded. */
	globalAway = $state(false);
	/** The worker has answered, so the switch can show a value it knows. */
	globalLoaded = $state(false);
	globalBusy = $state(false);
	#globalAllowed = false;
	/** Bumped by every read and write, so only the newest read can apply. */
	#globalGeneration = 0;
	/** A change signal came in mid-write: read again once the write answers. */
	#globalRefreshAfterWrite = false;

	/** This chat's own flag. Stays what the chat stored while the global switch is on. */
	isChatAway(sessionId: string | null | undefined): boolean {
		return !!sessionId && !!this.values[sessionId];
	}

	/** Whether this chat's agent is away right now, whichever switch says so. A new chat counts. */
	isEffectivelyAway(sessionId: string | null | undefined): boolean {
		return this.globalAway || this.isChatAway(sessionId);
	}

	/** Only admins may read or change the global switch; everyone else never asks. */
	setGlobalAccess(allowed: boolean): void {
		if (allowed === this.#globalAllowed) return;
		this.#globalAllowed = allowed;
		this.#globalGeneration++;
		this.#globalRefreshAfterWrite = false;
		this.globalAway = false;
		this.globalLoaded = false;
		this.globalBusy = false;
		if (allowed) void this.refreshGlobal();
	}

	/** Read the global switch. A failure keeps what is shown; the next signal retries. */
	async refreshGlobal(): Promise<void> {
		if (!this.#globalAllowed) return;
		if (this.globalBusy) {
			this.#globalRefreshAfterWrite = true;
			return;
		}
		const generation = ++this.#globalGeneration;
		try {
			const result = await api.get<{ away: boolean }>('/away-mode');
			if (generation !== this.#globalGeneration) return;
			this.globalAway = !!result?.away;
			this.globalLoaded = true;
		} catch {
			/* Reconnect, return to the page or the next change signal reads it again. */
		}
	}

	/** Flip the global switch. It shows the worker's answer, never a guess. */
	async setGlobal(away: boolean): Promise<GlobalAwayResult> {
		if (!this.#globalAllowed) throw new Error('Only the owner can change Away for all chats');
		this.globalBusy = true;
		this.#globalGeneration++;
		try {
			const result = await api.put<Partial<GlobalAwayResult>>('/away-mode', { away });
			this.globalAway = !!result?.away;
			this.globalLoaded = true;
			return {
				away: !!result?.away,
				released_questions: result?.released_questions ?? 0,
				deferred_approvals: result?.deferred_approvals ?? 0
			};
		} catch (error) {
			// The write may have landed before the answer was lost.
			this.#globalRefreshAfterWrite = true;
			throw error;
		} finally {
			this.globalBusy = false;
			this.#globalGeneration++;
			if (this.#globalRefreshAfterWrite) {
				this.#globalRefreshAfterWrite = false;
				void this.refreshGlobal();
			}
		}
	}

	/** Read the persisted flag once per session (cheap, cached). */
	async load(sessionId: string | null | undefined): Promise<void> {
		if (!sessionId || this.#loaded.has(sessionId)) return;
		this.#loaded.add(sessionId);
		try {
			const session = await getSessionOwnerMeta(sessionId, 'away_mode');
			if (this.values[sessionId] === undefined) this.values[sessionId] = !!session?.away_mode;
		} catch {
			this.#loaded.delete(sessionId);
		}
	}

	/**
	 * A session row reported the flag (the session list reloads when the row
	 * changes on this worker or arrives from the owning worker). `undefined`
	 * means the row does not carry the field and changes nothing; `null` is a
	 * flag never set, which is off. After a confirmed toggle, a disagreeing row
	 * wins once the grace ends, except `null`: the owner wrote a value, so a row
	 * still without one cannot report it (an owner too old to publish it).
	 */
	observe(sessionId: string | null | undefined, away: boolean | null | undefined): void {
		if (!sessionId || away === undefined) return;
		this.#rows.set(sessionId, away);
		if (this.busy[sessionId]) return;
		const value = !!away;
		const confirmed = this.#confirmed.get(sessionId);
		if (confirmed) {
			if (confirmed.away !== value) {
				const wait = confirmed.until - Date.now();
				if (wait > 0) {
					this.#scheduleSettle(sessionId, wait);
					return;
				}
				if (away === null) return;
			}
			this.#confirmed.delete(sessionId);
			this.#clearSettle(sessionId);
		}
		this.#loaded.add(sessionId);
		if (this.values[sessionId] !== value) this.values[sessionId] = value;
	}

	#scheduleSettle(sessionId: string, wait: number): void {
		this.#clearSettle(sessionId);
		this.#settle.set(sessionId, setTimeout(() => {
			this.#settle.delete(sessionId);
			this.observe(sessionId, this.#rows.get(sessionId));
		}, wait + 1));
	}

	#clearSettle(sessionId: string): void {
		const timer = this.#settle.get(sessionId);
		if (timer !== undefined) clearTimeout(timer);
		this.#settle.delete(sessionId);
	}

	async set(sessionId: string, away: boolean): Promise<{ released_questions?: number }> {
		this.busy[sessionId] = true;
		try {
			const result = await api.put<{ away: boolean; released_questions?: number }>(
				`/sessions/${sessionId}/away-mode`, { away }
			);
			this.values[sessionId] = !!result?.away;
			this.#confirmed.set(sessionId, { away: !!result?.away, until: Date.now() + AWAY_CONFIRM_GRACE_MS });
			this.#loaded.add(sessionId);
			return result ?? {};
		} finally {
			this.busy[sessionId] = false;
		}
	}
}

export const awayMode = new AwayModeStore();
