/**
 * Windowed transcript mounting — scheduling and geometry helpers.
 *
 * Why (PERFORMANCE_REVIEW §3.2 R4): MessageArea used to mount EVERY display
 * item. `content-visibility: auto` skips paint and layout for off-screen rows,
 * but component creation, markdown parsing, DOMPurify sanitising and Shiki
 * tokenisation all still run at mount — so opening a 500-message session paid
 * hundreds of parse passes before first paint.
 *
 * The transcript therefore mounts a trailing WINDOW first (the part the reader
 * actually lands on) and grows it backwards: on scroll-up, on a jump to an older
 * turn, on demand from the "show earlier messages" control, and — so nothing is
 * permanently missing from in-page search — in idle-time chunks until the whole
 * transcript is mounted. The window never shrinks while a session is open, which
 * keeps the keyed `{#each}` append-only in both directions and makes every
 * transition here monotone.
 *
 * Geometry stays pure; the background scheduler takes callbacks so its
 * visibility and idle-budget decisions can be tested independently.
 */

/** Schedule optional history mounting only when the view can use it. Explicit
 * scroll/search/turn navigation stays outside this background scheduler. */
export function createWindowBackfill(canRun: () => boolean, expand: () => void) {
	let handle: number | null = null;
	let idle = false;
	function cancel() {
		if (handle === null) return;
		if (idle && typeof cancelIdleCallback !== 'undefined') cancelIdleCallback(handle);
		else clearTimeout(handle);
		handle = null;
	}
	function schedule() {
		if (handle !== null || !canRun()) return;
		const run = (deadline?: IdleDeadline) => {
			handle = null;
			if (!canRun()) return;
			// A timeout is not idle time. Leave a frame's worth of headroom before
			// mounting a chunk; never force optional work onto a busy renderer.
			if (!deadline || (!deadline.didTimeout && deadline.timeRemaining() >= 8)) expand();
			schedule();
		};
		idle = typeof requestIdleCallback !== 'undefined';
		handle = idle ? requestIdleCallback(run) : setTimeout(run, 300) as unknown as number;
	}
	return { schedule, cancel };
}

/** Items mounted immediately on open / session switch. */
export const TRANSCRIPT_WINDOW_SIZE = 60;
/** Items added per expansion step (scroll-up trigger and idle backfill). */
export const TRANSCRIPT_WINDOW_CHUNK = 30;
/** Extra items mounted above a jump target so it is not flush against the edge. */
export const TRANSCRIPT_WINDOW_LEAD = 8;
/** Scroll-up expansion trigger: distance from the top of the mounted content. */
export const TRANSCRIPT_EXPAND_THRESHOLD_PX = 900;

/**
 * Start index of the trailing window for a fresh view of `itemCount` items.
 * `windowSize <= 0` mounts everything (used to disable windowing).
 */
export function initialWindowStart(itemCount: number, windowSize = TRANSCRIPT_WINDOW_SIZE): number {
	if (windowSize <= 0) return 0;
	return Math.max(0, itemCount - windowSize);
}

/**
 * Keep an existing start index legal as items are appended or the transcript
 * shrinks (rewind, clear, reload).
 *
 * The window only ever grows: appending messages must not un-mount rows the
 * reader can already see, so the start index never moves DOWN the list — it is
 * only clamped up to the trailing window when the transcript got shorter.
 */
export function clampWindowStart(
	start: number,
	itemCount: number,
	windowSize = TRANSCRIPT_WINDOW_SIZE
): number {
	const ceiling = initialWindowStart(itemCount, windowSize);
	return Math.max(0, Math.min(start, ceiling));
}

/** One expansion step backwards. */
export function expandedWindowStart(start: number, chunk = TRANSCRIPT_WINDOW_CHUNK): number {
	if (chunk <= 0) return start;
	return Math.max(0, start - chunk);
}

/**
 * Smallest start index that mounts `itemIndex` (plus a little lead-in above it),
 * without ever moving the window forward.
 */
export function windowStartForItem(
	start: number,
	itemIndex: number,
	lead = TRANSCRIPT_WINDOW_LEAD
): number {
	if (itemIndex < 0) return start;
	return Math.min(start, Math.max(0, itemIndex - lead));
}

/** True when the reader is close enough to the top of mounted content to expand. */
export function shouldExpandOnScroll(
	scrollTop: number,
	start: number,
	threshold = TRANSCRIPT_EXPAND_THRESHOLD_PX
): boolean {
	return start > 0 && scrollTop <= threshold;
}

/**
 * Scroll compensation for a PREPEND: hold the reader's distance from the bottom
 * constant so newly mounted history above the viewport cannot shift what they
 * are reading — and so a bottom-pinned transcript stays pinned.
 */
export function scrollTopPreservingBottom(
	scrollHeight: number,
	distanceFromBottom: number
): number {
	return Math.max(0, scrollHeight - distanceFromBottom);
}

/**
 * The live tail opts out of `content-visibility` (see MessageArea's styles).
 * With a window mounted, the `{#each}` index is window-relative, so the test has
 * to run against the item's index in the FULL list.
 */
export function isLiveTailIndex(globalIndex: number, itemCount: number, liveTail: number): boolean {
	return globalIndex >= itemCount - liveTail;
}

/** Per-turn position relative to the reading line, as reported by the observer. */
export type TurnVisibility = 'above' | 'inside' | 'below';

/**
 * Translate observed turn visibility into the `turnTops` vector
 * `resolveCurrentTurnIndex()` consumes, so the rail keeps its exact semantics
 * (last turn whose top crossed the reading line) while costing O(1) per scroll
 * frame instead of a `getBoundingClientRect()` per turn.
 *
 * Turns with no observation are unmounted: anything before the mounted window is
 * older, hence above the reading line; anything after it has not been reached.
 */
export function turnTopsFromVisibility(
	turnIds: readonly string[],
	states: ReadonlyMap<string, TurnVisibility>,
	readingLine: number,
	isMounted: (turnId: string) => boolean
): number[] {
	return turnIds.map((id) => {
		const state = states.get(id);
		if (state === 'above') return -1;
		if (state === 'inside') return 0;
		if (state === 'below') return readingLine + 1;
		return isMounted(id) ? Number.POSITIVE_INFINITY : -1;
	});
}

/**
 * Classify one IntersectionObserver entry for a turn anchor.
 *
 * The observer's root is clipped to the band between the container top and the
 * reading line, so "intersecting" means the anchor is inside that band; anything
 * else is decided by comparing the anchor's top against the band's top — the
 * rect the callback already carries, so no extra layout read.
 */
export function classifyTurnEntry(
	isIntersecting: boolean,
	anchorTop: number,
	rootTop: number | null | undefined,
	previous: TurnVisibility | undefined
): TurnVisibility | undefined {
	if (isIntersecting) return 'inside';
	if (rootTop === null || rootTop === undefined) return previous;
	return anchorTop < rootTop ? 'above' : 'below';
}
