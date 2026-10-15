/**
 * Windowed transcript mounting — scheduling and geometry helpers.
 *
 * Why (PERFORMANCE_REVIEW §3.2 R4): MessageArea used to mount EVERY display
 * item. `content-visibility: auto` skips paint and layout for off-screen rows,
 * but component creation, markdown parsing, DOMPurify sanitising and Shiki
 * tokenisation all still run at mount — so opening a 500-message session paid
 * hundreds of parse passes before first paint.
 *
 * `createTranscriptWindow` mounts a bounded slice and shifts it on explicit
 * scroll/search/turn navigation. It never schedules idle growth. The caller
 * preserves a keyed DOM anchor across shifts, including eviction at either edge.
 *
 * Legacy progressive-window helpers remain below for compatibility; unlike the
 * bounded controller they only limit initial mounting, not eventual DOM size.
 */

/** A bounded, half-open slice. Capture a keyed DOM anchor before moving it and
 * restore that anchor after rendering; bottom-distance compensation is not safe
 * when a shift also unmounts rows at the opposite edge. */
export interface TranscriptRange { readonly start: number; readonly end: number }

/** Hard limit for outer items; nested row windows need their own bound. */
export const TRANSCRIPT_WINDOW_CAP = 120;

function rangeAt(start: number, count: number, cap: number, previous?: TranscriptRange): TranscriptRange {
	if (!Number.isInteger(count) || count < 0 || !Number.isInteger(cap) || cap < 1) {
		throw new RangeError('Item count must be non-negative and cap must be positive integers');
	}
	start = Math.max(0, Math.min(Math.floor(start), Math.max(0, count - cap)));
	const end = Math.min(count, start + cap);
	return previous?.start === start && previous.end === end ? previous : { start, end };
}

/** Pure per-view range controller. No timers or idle expansion: only explicit
 * navigation moves into history. Apply the same controller to rows inside a
 * large group: an outer-item cap alone cannot bound nested mounted rows. */
export function createTranscriptWindow({ cap = TRANSCRIPT_WINDOW_CAP, step = 30 }: { cap?: number; step?: number } = {}) {
	if (!Number.isInteger(cap) || cap < 1 || !Number.isInteger(step) || step < 1) {
		throw new RangeError('Transcript cap and step must be positive integers');
	}
	// Keep overlap for a keyed DOM anchor whenever the cap permits it.
	step = Math.min(step, Math.max(1, cap - 1));
	let count = 0;
	let range: TranscriptRange = { start: 0, end: 0 };
	let followsTail = true;
	let hidden = false;
	let streaming = false;
	function move(start: number): TranscriptRange {
		range = rangeAt(start, count, cap, range);
		return range;
	}
	function toTail(): TranscriptRange {
		followsTail = true;
		return move(count - cap);
	}
	function extendOlder(): TranscriptRange {
		followsTail = false;
		return move(range.start - step);
	}
	return {
		cap,
		get visibleRange() { return range; },
		get followsTail() { return followsTail; },
		get isStreaming() { return streaming; },
		setItemCount(next: number, options: { followTail?: boolean } = {}): TranscriptRange {
			if (!Number.isInteger(next) || next < 0) throw new RangeError('Item count must be a non-negative integer');
			count = next;
			followsTail = options.followTail ?? followsTail;
			// Hidden appends do not grow the mounted slice. Shrinks must still clamp.
			if (hidden && count >= range.end) return range;
			return move(followsTail ? count - cap : range.start);
		},
		extendOlder,
		loadOlder: extendOlder,
		extendNewer(): TranscriptRange {
			const next = move(range.start + step);
			followsTail = next.end === count;
			return next;
		},
		windowAround(index: number): TranscriptRange {
			if (!Number.isInteger(index) || index < 0 || index >= count) return range;
			followsTail = false;
			return move(index - Math.floor(cap / 2));
		},
		toTail,
		onStreamingChange(value: boolean): TranscriptRange {
			streaming = value;
			return range;
		},
		pauseWhenHidden(value: boolean): TranscriptRange {
			hidden = value;
			return hidden ? range : move(followsTail ? count - cap : range.start);
		}
	};
}

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
const TRANSCRIPT_WINDOW_LEAD = 8;
/** Scroll-up expansion trigger: distance from the top of the mounted content. */
const TRANSCRIPT_EXPAND_THRESHOLD_PX = 900;

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

/**
 * Correct anchors the observer skipped. It only reports an anchor when its band
 * membership changes, so a jump that carries an anchor from below the band to
 * above it (or back) within one frame reports nothing, and the stale state then
 * pins the rail to the wrong turn. An anchor just reported inside the band is
 * real geometry and anchors are in document order, so earlier turns cannot be
 * below the band and later turns cannot be above it. No layout is read.
 * `reportedIds` are the anchors in this observer batch; only those whose
 * resulting state is 'inside' count, because one batch can carry several
 * entries for the same anchor and the last one wins.
 */
export function settleSkippedTurns(
	turnIds: readonly string[],
	states: Map<string, TurnVisibility>,
	reportedIds: ReadonlySet<string>
): void {
	if (reportedIds.size === 0) return;
	let first = -1;
	let last = -1;
	turnIds.forEach((id, index) => {
		if (!reportedIds.has(id) || states.get(id) !== 'inside') return;
		if (first < 0) first = index;
		last = index;
	});
	if (first < 0) return;
	turnIds.forEach((id, index) => {
		const state = states.get(id);
		if (index < first && state === 'below') states.set(id, 'above');
		else if (index > last && state === 'above') states.set(id, 'below');
	});
}
