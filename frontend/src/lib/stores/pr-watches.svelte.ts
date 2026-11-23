/**
 * PR watches as the PR inbox and a chat's Git header show them. One list for
 * the whole app, keyed by "owner/name#number", so starting a watch in one
 * place shows it in the other. The server does the watching
 * (app/core/pr_watch.py); this only reads and toggles.
 */
import { listPrWatches, startPrWatch, stopPrWatch, type PrWatch } from '$lib/api/pr-watches';

/** "owner/name" from a GitHub pull request link, or null. */
export function repoFromPrUrl(url: string | null | undefined): string | null {
	const m = /^https:\/\/github\.com\/([^/\s]+\/[^/\s]+)\/pull\/\d+/i.exec(url ?? '');
	return m ? m[1] : null;
}

export function watchKey(repo: string, number: number): string {
	return `${repo.toLowerCase()}#${number}`;
}

/** The second line under the toggle: what the watch is doing right now. */
export function watchSummary(w: PrWatch | null | undefined): string {
	if (!w || w.status !== 'active') return 'wakes this chat for failed CI, reviews and conflicts';
	if (w.last_error) return `could not check GitHub: ${w.last_error}`;
	if (w.needs_owner) return 'needs you: see the bell';
	if (!w.session_id) return 'no chat opened this PR: notifies you only';
	const wakes = w.wakes_today === 1 ? '1 wake today' : `${w.wakes_today} wakes today`;
	return w.wakes_today > 0 ? `watching · ${wakes}` : 'watching CI, reviews and conflicts';
}

export interface WatchTarget {
	repo: string;
	number: number;
	url?: string | null;
	title?: string | null;
	sessionId?: string | null;
}

const FRESH_MS = 30_000;

class PrWatchStore {
	byKey = $state<Record<string, PrWatch>>({});
	busy = $state<Record<string, boolean>>({});
	error = $state<Record<string, string>>({});
	private loadedAt = 0;
	private inflight: Promise<void> | null = null;

	/** The active watch for a PR, or null. */
	active(repo: string | null | undefined, number: number | null | undefined): PrWatch | null {
		if (!repo || !number) return null;
		const w = this.byKey[watchKey(repo, number)];
		return w && w.status === 'active' ? w : null;
	}

	load(force = false): Promise<void> {
		if (!force && Date.now() - this.loadedAt < FRESH_MS) return Promise.resolve();
		if (this.inflight) return this.inflight;
		this.inflight = (async () => {
			try {
				const { watches } = await listPrWatches();
				const next: Record<string, PrWatch> = {};
				// Newest last, so an active watch wins over an older ended one.
				for (const w of [...watches].sort((a, b) => (a.created_at ?? '').localeCompare(b.created_at ?? ''))) {
					next[watchKey(w.repo, w.pr_number)] = w;
				}
				this.byKey = next;
				this.loadedAt = Date.now();
			} catch {
				/* older worker without PR watch, or not the owner: show nothing watched */
			} finally {
				this.inflight = null;
			}
		})();
		return this.inflight;
	}

	async toggle(target: WatchTarget): Promise<void> {
		const key = watchKey(target.repo, target.number);
		this.busy[key] = true;
		this.error[key] = '';
		try {
			const current = this.active(target.repo, target.number);
			const row = current
				? ((await stopPrWatch(current.id)) ?? { ...current, status: 'stopped' as const })
				: await startPrWatch({
					repo: target.repo,
					pr_number: target.number,
					pr_url: target.url ?? null,
					pr_title: target.title ?? null,
					session_id: target.sessionId ?? null
				});
			this.byKey[key] = row;
		} catch (err) {
			const e = err as { detail?: string; message?: string };
			this.error[key] = e.detail || e.message || 'Could not change the watch';
		} finally {
			this.busy[key] = false;
		}
	}
}

export const prWatches = new PrWatchStore();
