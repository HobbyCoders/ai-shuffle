/**
 * Per-session git state for the sidebar cards.
 *
 * The sidebar needs one more fact than the sessions store carries: what the
 * session's own checkout looks like right now — dirty, ahead, clean — plus the
 * diffstat that goes with it. That is a request per checkout, so this store is
 * deliberately stingy about making them:
 *
 * - only ACTIVE (non-compact) cards ever subscribe; settled rows read their
 *   glyph off `pr_state` and never cost a request,
 * - subscriptions dedupe by session id, so two cards for one session share a
 *   single poll,
 * - the poll is slow (60s) and stops the moment the last watcher releases,
 *   aborting whatever is in flight,
 * - the session open in the main panel doesn't subscribe at all: SessionCard
 *   reads the real git store, which already holds that checkout.
 *
 * Failures are silent by design. A card that cannot read its checkout falls
 * back to the quiet dot; it must never raise a toast or spin.
 */
import {
	getDiffStats,
	getGitStatus,
	type DiffStats,
	type GitStatus
} from '$lib/api/git';

/** What a card gets back. `loaded` separates "asked and got nothing" from
 *  "haven't asked yet", so a fresh row doesn't flash a wrong stage. */
export interface SessionGitEntry {
	status: GitStatus | null;
	diffStats: DiffStats | null;
	loaded: boolean;
}

/** Slow on purpose: this is ambient context, not a live view. */
export const REFRESH_MS = 60_000;

interface Watch {
	/** How many mounted cards want this session. */
	refs: number;
	/** project + worktree the poll is bound to; a change restarts it. */
	scope: string;
	projectId: string;
	worktreeId: string | null;
	abort: AbortController | null;
	timer: ReturnType<typeof setInterval> | null;
	inFlight: boolean;
}

const entries = $state<Record<string, SessionGitEntry>>({});
const watches = new Map<string, Watch>();
/** Which checkout each cached entry describes. Kept alongside — an entry
 *  outlives its watch (so a remount paints instantly) and must be thrown away
 *  if the session comes back pointing somewhere else. */
const cachedScopes = new Map<string, string>();

function scopeKey(projectId: string, worktreeId: string | null): string {
	return worktreeId ? `${projectId}@${worktreeId}` : projectId;
}

async function load(sessionId: string) {
	const watch = watches.get(sessionId);
	if (!watch || watch.inFlight || (typeof document !== 'undefined' && document.hidden)) return;
	watch.inFlight = true;
	const controller = new AbortController();
	watch.abort = controller;
	// A watcher that was torn down (or re-scoped) mid-flight must not write.
	const live = () => !controller.signal.aborted && watches.get(sessionId) === watch;

	try {
		const status = await getGitStatus(
			watch.projectId,
			watch.worktreeId ?? undefined,
			{ signal: controller.signal },
			sessionId
		);
		if (!live()) return;
		// The diffstat only ever appears next to uncommitted work, so a clean
		// tree costs one request instead of two.
		let diffStats: DiffStats | null = null;
		if (status.is_git_repo && status.has_changes) {
			diffStats = await getDiffStats(
				watch.projectId,
				true,
				watch.worktreeId ?? undefined,
				{ signal: controller.signal },
				sessionId
			).catch(() => null);
			if (!live()) return;
		}
		entries[sessionId] = { status, diffStats, loaded: true };
		cachedScopes.set(sessionId, watch.scope);
	} catch {
		// Quietly settle on "nothing to show" rather than leaving the card
		// pending forever; a later tick can still succeed.
		if (live() && !entries[sessionId]) {
			entries[sessionId] = { status: null, diffStats: null, loaded: true };
			cachedScopes.set(sessionId, watch.scope);
		}
	} finally {
		if (watches.get(sessionId) === watch && watch.abort === controller) {
			watch.inFlight = false;
			watch.abort = null;
		}
	}
}

function teardown(sessionId: string, watch: Watch) {
	if (watch.timer !== null) clearInterval(watch.timer);
	watch.timer = null;
	watch.abort?.abort();
	watch.abort = null;
	if (watches.get(sessionId) === watch) watches.delete(sessionId);
	if (watches.size === 0 && typeof document !== 'undefined') {
		document.removeEventListener('visibilitychange', onVisibilityChange);
	}
}

function resumeWatch(sessionId: string, watch: Watch) {
	if (typeof document !== 'undefined' && document.hidden) return;
	void load(sessionId);
	if (watch.timer === null) watch.timer = setInterval(() => void load(sessionId), REFRESH_MS);
}

function onVisibilityChange() {
	for (const [sessionId, watch] of watches) {
		if (document.hidden) {
			if (watch.timer !== null) clearInterval(watch.timer);
			watch.timer = null;
			watch.abort?.abort();
			watch.abort = null;
			watch.inFlight = false;
		} else resumeWatch(sessionId, watch);
	}
}

export const sessionGitState = {
	/** Last known state for a session, or null when nobody has fetched it. */
	get(sessionId: string, projectId?: string | null, worktreeId: string | null = null): SessionGitEntry | null {
		// Read the reactive slot even before its first scope has been cached.
		const entry = entries[sessionId] ?? null;
		if (projectId !== undefined && (!projectId || cachedScopes.get(sessionId) !== scopeKey(projectId, worktreeId))) return null;
		return entry;
	},

	/** Retain a hydrated main-panel snapshot when selection moves elsewhere. */
	remember(sessionId: string, projectId: string, worktreeId: string | null, entry: SessionGitEntry) {
		const watch = watches.get(sessionId);
		// A slow sidebar request must not replace this newer snapshot.
		watch?.abort?.abort();
		entries[sessionId] = entry;
		cachedScopes.set(sessionId, scopeKey(projectId, worktreeId));
	},

	/**
	 * Register a card's interest in a session's checkout and start (or join) the
	 * poll. Returns the release function — hand it straight back from an
	 * `$effect` so unmounting aborts the in-flight request.
	 *
	 * A session with no project has no checkout to read: that call is a no-op.
	 */
	watch(sessionId: string, projectId: string | null, worktreeId: string | null = null): () => void {
		if (!sessionId || !projectId) return () => {};

		const scope = scopeKey(projectId, worktreeId);
		const existing = watches.get(sessionId);
		if (existing && existing.scope === scope) {
			existing.refs += 1;
			return () => release(sessionId, existing);
		}
		// Same session pointed at a different checkout: drop the stale poll and
		// its cached answer rather than showing the old branch's state.
		if (existing) teardown(sessionId, existing);
		if (cachedScopes.get(sessionId) !== scope) {
			delete entries[sessionId];
			cachedScopes.delete(sessionId);
		}

		const watch: Watch = {
			refs: 1,
			scope,
			projectId,
			worktreeId,
			abort: null,
			timer: null,
			inFlight: false
		};
		watches.set(sessionId, watch);
		if (typeof document !== 'undefined') document.addEventListener('visibilitychange', onVisibilityChange);
		resumeWatch(sessionId, watch);
		return () => release(sessionId, watch);
	},

	/** Test hook: drop every poll and every cached answer. */
	reset() {
		for (const [sessionId, watch] of [...watches]) teardown(sessionId, watch);
		for (const key of Object.keys(entries)) delete entries[key];
		cachedScopes.clear();
	}
};

function release(sessionId: string, watch: Watch) {
	if (watch.refs <= 0) return;
	watch.refs -= 1;
	if (watch.refs > 0) return;
	teardown(sessionId, watch);
}
