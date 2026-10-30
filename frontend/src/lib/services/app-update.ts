/**
 * Version skew: this page still runs the build it was loaded with after the
 * worker it talks to has updated.
 *
 * Every build's hashed chunks replace the previous build's, so a page that
 * outlives an update cannot load any code it has not loaded yet: a lazy
 * panel or the voice audio stream module 404s. The phone app keeps one page
 * for as long as Android keeps the app, across days of resumes, so it is the
 * client that hits this most (#707).
 *
 * This module holds the rules and a dependency-free hub. The browser wiring
 * (events, the version check, the banner) is in `app-update-watch.ts`, so
 * code anywhere can report what it knows without importing the app shell:
 *
 *  - `registerReloadBlocker` — a module holding something a reload would
 *    destroy (a live voice call, an unsent composer draft) says so.
 *  - `reportServerReconnected` — live sync reconnected after a gap; a worker
 *    update restarts the server, so this is when a new build appears.
 *  - `reportChunkLoadFailure` — a lazy import failed.
 */

/** What the outcome banner says when an update broke a feature mid-use. */
export const APP_UPDATED_MESSAGE = 'This app was updated. Reload to continue.';

/**
 * What a call start says when its code would not load. Neutral: a dropped
 * connection fails the same way as a missing chunk, and only the version
 * check (the watcher's banner) can tell them apart. Reload fixes both.
 */
export const CALL_LOAD_FAILED_MESSAGE = "Couldn't load the call. Reload to continue.";

/** What the banner says when a newer build was found before anything broke. */
export const APP_UPDATE_READY_MESSAGE = 'App updated — reload to get the latest version.';

/**
 * A check that runs within this long after the page became visible counts as
 * "just resumed": the user has not started doing anything yet, so a reload
 * costs them nothing. Covers the live-sync reconnect that follows a resume.
 */
export const RESUME_GRACE_MS = 8_000;

/**
 * A reload that has not unloaded the page by now was cancelled (a
 * beforeunload prompt the user answered "Stay"); the monitor works again.
 */
export const RELOAD_SETTLE_MS = 1_500;

/** At most one automatic reload per tab in this window (flapping replicas). */
export const AUTO_RELOAD_COOLDOWN_MS = 10 * 60_000;

/** Checks closer together than this share one answer (focus and visibility fire together). */
export const CHECK_MIN_INTERVAL_MS = 5_000;
/**
 * After a service-worker activation that read as "same build", ask once more
 * this long after: a failed version fetch also reads as "same build", and the
 * worker never signals that build again.
 */
export const SW_RECHECK_MS = 30_000;

// ── Chunk-load failures ─────────────────────────────────────────────────────

/**
 * The messages browsers and Vite give a dynamic import whose file is gone:
 * Chromium, Firefox, Safari, then Vite's preload helper for CSS and JS deps.
 */
const CHUNK_LOAD_PATTERNS = [
	/failed to fetch dynamically imported module/i,
	/error loading dynamically imported module/i,
	/importing a module script failed/i,
	/unable to preload css/i,
	/failed to load module script/i,
	/'text\/html' is not a valid javascript mime type/i
];

/** True when `error` is a failed dynamic import (a missing or unreachable chunk). */
export function isChunkLoadError(error: unknown): boolean {
	if (!error) return false;
	if (typeof error === 'object') {
		const named = error as { name?: unknown; message?: unknown; payload?: unknown };
		if (named.name === 'ChunkLoadError') return true;
		// `vite:preloadError` carries the underlying error as `payload`.
		if (named.payload && named.payload !== error && isChunkLoadError(named.payload)) return true;
		if (typeof named.message === 'string') return CHUNK_LOAD_PATTERNS.some((p) => p.test(named.message as string));
		return false;
	}
	return typeof error === 'string' && CHUNK_LOAD_PATTERNS.some((p) => p.test(error));
}

// ── Safe-reload rules ───────────────────────────────────────────────────────

export interface ReloadContext {
	/** The page is not visible (another tab, the app in the background). */
	hidden: boolean;
	/** The page became visible moments ago (see RESUME_GRACE_MS). */
	resumed: boolean;
	/** Reasons a reload would destroy something (see registerReloadBlocker). */
	blockers: readonly string[];
}

/**
 * What to do once the page is known to be out of date.
 *
 * Never reload over something a reload would destroy: a live voice call or
 * an unsent draft. Otherwise reload when the user cannot be interrupted by
 * it (hidden) or has not started anything yet (just resumed). A user in the
 * middle of using the page gets a banner instead.
 */
export function reloadDecision(ctx: ReloadContext): 'reload' | 'banner' {
	if (ctx.blockers.length) return 'banner';
	return ctx.hidden || ctx.resumed ? 'reload' : 'banner';
}

// ── The hub ─────────────────────────────────────────────────────────────────

type Blocker = () => string | null | false | undefined;

const blockers = new Set<Blocker>();

/**
 * Register a check that answers why a reload now would lose something (or
 * nothing). Returns the unregister function. Checks must be cheap and never
 * throw; one that throws counts as a blocker.
 */
export function registerReloadBlocker(blocker: Blocker): () => void {
	blockers.add(blocker);
	return () => { blockers.delete(blocker); };
}

/** Every reason a reload now would lose something. */
export function reloadBlockers(): string[] {
	const reasons: string[] = [];
	for (const blocker of blockers) {
		try {
			const reason = blocker();
			if (reason) reasons.push(reason);
		} catch {
			reasons.push('unknown');
		}
	}
	return reasons;
}

/** No registered blocker objects to a reload right now. */
export function canReloadSilently(): boolean {
	return reloadBlockers().length === 0;
}

export interface AppUpdateListener {
	reconnected(): void;
	chunkFailed(error: unknown): void;
}

let listener: AppUpdateListener | null = null;

/** The app shell's watcher receives the reports below (one per page). */
export function setAppUpdateListener(next: AppUpdateListener | null): void {
	listener = next;
}

/** Live sync reconnected after a gap (the server may have restarted on a new build). */
export function reportServerReconnected(): void {
	try { listener?.reconnected(); } catch { /* never break the caller */ }
}

/**
 * A lazy import failed. Returns whether it looks like a missing chunk, so a
 * caller can choose its own wording; the watcher confirms the skew against
 * the server before saying anything.
 */
export function reportChunkLoadFailure(error: unknown): boolean {
	if (!isChunkLoadError(error)) return false;
	try { listener?.chunkFailed(error); } catch { /* never break the caller */ }
	return true;
}

// ── The monitor ─────────────────────────────────────────────────────────────

export interface AppUpdateMonitorDeps {
	/** True when the server serves a different build than this page runs. */
	checkForNewBuild: () => Promise<boolean>;
	isHidden: () => boolean;
	blockers: () => readonly string[];
	reload: () => void;
	/**
	 * False when an automatic reload already ran from this very build and the
	 * page still came back on it (a worker whose version file does not match
	 * its pages): offer the banner instead of reloading in a loop.
	 */
	autoReloadAllowed?: () => boolean;
	/** Show (or upgrade) the single update banner; `broken` means a feature already failed. */
	showBanner: (broken: boolean) => void;
	now?: () => number;
	/** Run `fn` after `ms` (a page that unloads never runs it). */
	schedule?: (fn: () => void, ms: number) => void;
}

export type CheckReason = 'resume' | 'reconnect' | 'online' | 'chunk' | 'service-worker' | 'start';

/**
 * Decides when to check and what to do with the answer. No browser APIs, so
 * the whole policy is unit-tested with fakes.
 */
export function createAppUpdateMonitor(deps: AppUpdateMonitorDeps) {
	const now = deps.now ?? (() => Date.now());
	const schedule = deps.schedule ?? ((fn: () => void, ms: number) => { setTimeout(fn, ms); });
	let stale = false;
	let broken = false;
	let reloading = false;
	let visibleSince = deps.isHidden() ? null : now();
	let lastCheckAt = -Infinity;
	let inflight: Promise<boolean> | null = null;

	function justResumed(): boolean {
		return visibleSince !== null && now() - visibleSince <= RESUME_GRACE_MS;
	}

	/**
	 * Reload, and if the page is still here a moment later (the user kept it
	 * at a beforeunload prompt), let checks, hides and the banner work again.
	 */
	function reload(): void {
		reloading = true;
		deps.reload();
		schedule(() => { reloading = false; }, RELOAD_SETTLE_MS);
	}

	/** Apply the rules to a known-stale page. */
	function act(): 'reload' | 'banner' | 'none' {
		if (!stale || reloading) return 'none';
		let decision = reloadDecision({ hidden: deps.isHidden(), resumed: justResumed(), blockers: deps.blockers() });
		if (decision === 'reload' && deps.autoReloadAllowed && !deps.autoReloadAllowed()) decision = 'banner';
		if (decision === 'reload') {
			reload();
		} else {
			deps.showBanner(broken);
		}
		return decision;
	}

	async function check(reason: CheckReason): Promise<boolean> {
		if (reloading) return true;
		if (reason === 'chunk') broken = true;
		if (stale) {
			act();
			return true;
		}
		// A missing chunk or a service-worker activation is worth asking about
		// even right after another check (the start-up one, say).
		const urgent = reason === 'chunk' || reason === 'service-worker';
		if (!urgent && now() - lastCheckAt < CHECK_MIN_INTERVAL_MS) return inflight ? inflight : false;
		lastCheckAt = now();
		inflight ??= deps.checkForNewBuild().catch(() => false).finally(() => { inflight = null; });
		const updated = await inflight;
		if (updated && !stale) stale = true;
		// A chunk that failed while the server still runs this build was the
		// network, not an update: the next skew is not "a feature broke".
		if (!stale) broken = false;
		if (stale) act();
		return stale;
	}

	return {
		get stale() { return stale; },
		check,
		/** The page was just hidden: a pending update applies now if nothing blocks it. */
		hidden() {
			visibleSince = null;
			act();
		},
		/** The page became visible again. */
		visible() {
			visibleSince = now();
			return check('resume');
		},
		/**
		 * A service worker took control of the page. That alone is no news: a
		 * fresh visit's first install claims the page too, on the very build
		 * the page runs (#908). Ask the server, past the throttle and after
		 * any check already in flight (it may have asked before the update
		 * landed); only a different build makes the page stale.
		 */
		async serviceWorkerActivated(): Promise<boolean> {
			// A reload already under way is not a confirmed new build.
			if (reloading) return stale;
			if (inflight) await inflight;
			const updated = await check('service-worker');
			if (!updated) schedule(() => { if (!stale) void check('service-worker'); }, SW_RECHECK_MS);
			return updated;
		},
		/** Something outside the version check already confirmed a newer build. */
		markStale() {
			stale = true;
			act();
		},
		/** The banner's Reload. Always honoured: the user chose it, even after a cancelled one. */
		reloadNow() {
			reload();
		}
	};
}

export type AppUpdateMonitor = ReturnType<typeof createAppUpdateMonitor>;
