/**
 * Away mode per session (app/core/away_mode.py): while on, the agent's
 * questions are answered with their recommended option and approval prompts
 * are deferred, never granted. It ends with a "Decisions made for you" table.
 *
 * The flag lives on the session row; this store caches it per session and
 * flips it through PUT /sessions/{id}/away-mode (routed to the owning worker).
 */
import { api } from '$lib/api/client';

class AwayModeStore {
	values = $state<Record<string, boolean>>({});
	busy = $state<Record<string, boolean>>({});
	#loaded = new Set<string>();

	isAway(sessionId: string | null | undefined): boolean {
		return !!sessionId && !!this.values[sessionId];
	}

	/** Read the persisted flag once per session (cheap, cached). */
	async load(sessionId: string | null | undefined): Promise<void> {
		if (!sessionId || this.#loaded.has(sessionId)) return;
		this.#loaded.add(sessionId);
		try {
			const session = await api.get<{ away_mode?: boolean | null }>(`/sessions/${sessionId}`);
			if (this.values[sessionId] === undefined) this.values[sessionId] = !!session?.away_mode;
		} catch {
			this.#loaded.delete(sessionId);
		}
	}

	async set(sessionId: string, away: boolean): Promise<{ released_questions?: number }> {
		this.busy[sessionId] = true;
		try {
			const result = await api.put<{ away: boolean; released_questions?: number }>(
				`/sessions/${sessionId}/away-mode`, { away }
			);
			this.values[sessionId] = !!result?.away;
			this.#loaded.add(sessionId);
			return result ?? {};
		} finally {
			this.busy[sessionId] = false;
		}
	}
}

export const awayMode = new AwayModeStore();
