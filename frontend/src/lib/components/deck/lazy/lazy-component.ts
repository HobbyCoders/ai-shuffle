/**
 * Chunk-level lazy loading for the secondary feature surfaces.
 *
 * MainPanel used to statically import every feature panel (Settings, Notes,
 * Build, Docs, Stats, Automations, Files, Browser, Diff…), which is what made
 * the primary route one ~2.4 MB chunk. Each of those surfaces is now behind a
 * `createLazyComponent()` handle: an `import()` that Rollup turns into its own
 * chunk, deduped and cached here so a panel is fetched at most once per page
 * load and a *revisit* resolves synchronously (no spinner, no flash).
 *
 * Three properties the naive `{#await import(…)}` form does not give us, and
 * which is why this exists instead of inlining the await twelve times:
 *
 *  1. **Deduping** — hover-prefetch, idle-prefetch and the click itself all
 *     land on the same in-flight promise.
 *  2. **A synchronous cache hit** — `handle.resolved` lets the caller render the
 *     component in the same frame it is asked for, once loaded.
 *  3. **A real failure mode** — a chunk fetch can fail (offline, or a deploy
 *     replaced the hashed file). The rejected promise is *not* cached, so the
 *     retry button in LazyPanel actually re-attempts the fetch.
 */

/** Vite/Rollup dynamic-import shape for a Svelte component module. */
export type ComponentModuleLoader<T> = () => Promise<{ default: T }>;

export interface LazyComponentOptions {
	/**
	 * Extra work that must finish before the panel is considered ready — used by
	 * Docs, whose markdown lives in its own chunk and must be present before the
	 * card renders (its registry + search index read it synchronously).
	 * Runs in parallel with the component import.
	 */
	prepare?: () => Promise<unknown>;
}

export interface LazyComponentHandle<T = unknown> {
	/** Stable id, used for logging and preload ordering. */
	readonly id: string;
	/** The component once loaded, else null. Synchronous — cheap to read in a render. */
	readonly resolved: T | null;
	/** The last load failure, cleared when a retry succeeds. */
	readonly error: unknown;
	/** True while a load is in flight. */
	readonly loading: boolean;
	/** Fire-and-forget warm-up (hover / focus / idle). Never throws or rejects. */
	preload(): void;
	/** Load the chunk. Deduped; rejects on chunk-fetch failure so the caller can retry. */
	load(): Promise<T>;
}

export function createLazyComponent<T>(
	id: string,
	loader: ComponentModuleLoader<T>,
	options: LazyComponentOptions = {}
): LazyComponentHandle<T> {
	let resolved: T | null = null;
	let inflight: Promise<T> | null = null;
	let error: unknown = null;

	function load(): Promise<T> {
		if (resolved) return Promise.resolve(resolved);
		if (inflight) return inflight;

		const pending = (async () => {
			const [mod] = await Promise.all([loader(), options.prepare?.()]);
			return mod.default;
		})().then(
			(component) => {
				resolved = component;
				error = null;
				inflight = null;
				return component;
			},
			(err) => {
				// Do NOT cache the rejection: a failed chunk fetch is usually
				// transient (offline / stale deploy), and the retry path must be
				// able to re-attempt it.
				error = err;
				inflight = null;
				throw err;
			}
		);

		inflight = pending;
		return pending;
	}

	return {
		id,
		get resolved() {
			return resolved;
		},
		get error() {
			return error;
		},
		get loading() {
			return inflight !== null;
		},
		preload() {
			if (resolved || inflight) return;
			load().catch(() => {
				/* prefetch is best-effort; the real load surfaces the error */
			});
		},
		load
	};
}

/** True on a Data-Saver or 2G-class connection, where speculative work is rude. */
function connectionIsFrugal(): boolean {
	const conn = (navigator as { connection?: { saveData?: boolean; effectiveType?: string } })
		.connection;
	return !!conn?.saveData || conn?.effectiveType === 'slow-2g' || conn?.effectiveType === '2g';
}

/**
 * Run `fn` once the browser is idle (after `delayMs`), unless the connection is
 * frugal. Returns a cancel function. Used for speculative warm-ups that must
 * never compete with the boot requests the user is actually waiting for.
 */
export function runWhenIdle(fn: () => void, delayMs = 2_500): () => void {
	if (typeof window === 'undefined' || connectionIsFrugal()) return () => {};
	let cancelled = false;
	let idleHandle: number | null = null;
	const timer = setTimeout(() => {
		if (cancelled) return;
		const w = window as unknown as { requestIdleCallback?: (cb: () => void) => number };
		if (typeof w.requestIdleCallback === 'function') {
			idleHandle = w.requestIdleCallback(() => {
				if (!cancelled) fn();
			});
		} else if (!cancelled) {
			fn();
		}
	}, delayMs);
	return () => {
		cancelled = true;
		clearTimeout(timer);
		if (idleHandle !== null && typeof window.cancelIdleCallback === 'function') {
			window.cancelIdleCallback(idleHandle);
		}
	};
}

/**
 * Warm a list of handles one at a time while the browser is idle.
 *
 * Sequential on purpose: the panels are only *likely* to be needed, and the
 * app already competes for the 6-connections-per-host budget with the git and
 * session pollers. Returns a cancel function.
 */
export function preloadWhenIdle(
	handles: readonly LazyComponentHandle[],
	options: { startDelayMs?: number } = {}
): () => void {
	if (typeof window === 'undefined') return () => {};
	// Respect Data Saver and 2G-class links: speculative warming is a luxury,
	// and on those connections it competes with the requests the user is
	// actually waiting for. The panels still load on demand.
	if (connectionIsFrugal()) return () => {};

	const { startDelayMs = 1_500 } = options;
	let cancelled = false;
	let idleHandle: number | null = null;
	let timer: ReturnType<typeof setTimeout> | null = null;

	const ric: ((cb: () => void) => number) | null =
		typeof (window as { requestIdleCallback?: unknown }).requestIdleCallback === 'function'
			? (cb) => (window as unknown as { requestIdleCallback: (c: () => void) => number })
					.requestIdleCallback(cb)
			: null;

	function step(index: number) {
		if (cancelled || index >= handles.length) return;
		const handle = handles[index];
		if (handle.resolved) {
			step(index + 1);
			return;
		}
		handle
			.load()
			.catch(() => {})
			.then(() => {
				if (cancelled) return;
				const next = () => step(index + 1);
				if (ric) idleHandle = ric(next);
				else timer = setTimeout(next, 200);
			});
	}

	timer = setTimeout(() => {
		if (cancelled) return;
		if (ric) idleHandle = ric(() => step(0));
		else step(0);
	}, startDelayMs);

	return () => {
		cancelled = true;
		if (timer) clearTimeout(timer);
		if (idleHandle !== null && typeof window.cancelIdleCallback === 'function') {
			window.cancelIdleCallback(idleHandle);
		}
	};
}
