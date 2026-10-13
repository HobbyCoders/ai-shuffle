/**
 * "Merge when green" per session (app/core/auto_merge.py): the worker watches
 * the session's open PR and merges it once every CI check has passed and no
 * other session is working in its branch. Outcomes post as rows in the chat.
 */
import { api } from '$lib/api/client';

export interface AutoMergeStatus {
	enabled: boolean;
	pr_number: number | null;
}

class AutoMergeStore {
	values = $state<Record<string, AutoMergeStatus>>({});
	busy = $state<Record<string, boolean>>({});
	error = $state<Record<string, string>>({});

	enabled(sessionId: string | null | undefined): boolean {
		return !!sessionId && !!this.values[sessionId]?.enabled;
	}

	async load(sessionId: string | null | undefined): Promise<void> {
		if (!sessionId) return;
		try {
			this.values[sessionId] = await api.get<AutoMergeStatus>(`/sessions/${sessionId}/auto-merge`);
		} catch {
			/* older worker without the endpoint: leave the toggle off */
		}
	}

	async toggle(sessionId: string): Promise<void> {
		this.busy[sessionId] = true;
		this.error[sessionId] = '';
		try {
			this.values[sessionId] = await api.put<AutoMergeStatus>(`/sessions/${sessionId}/auto-merge`, {
				enabled: !this.enabled(sessionId)
			});
		} catch (err) {
			this.error[sessionId] = err instanceof Error ? err.message : 'Could not change Merge when green';
		} finally {
			this.busy[sessionId] = false;
		}
	}
}

export const autoMerge = new AutoMergeStore();
