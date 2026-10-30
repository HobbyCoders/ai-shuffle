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

/** Read the actual native selection before a range shift, including reverse
 * selections and endpoints outside the list. Resolve row keys against the
 * current model: a prepend must not pin the old numerical indices. */
export function selectedRowRange(
	root: HTMLElement | null | undefined,
	selector: string,
	indexOf: (row: HTMLElement) => number
): TranscriptRange | null {
	const selection = root?.ownerDocument.getSelection();
	if (!root || !selection || selection.isCollapsed || !selection.rangeCount) return null;
	const selected = selection.getRangeAt(0);
	let start = Infinity, end = -1;
	for (const row of root.querySelectorAll<HTMLElement>(selector)) {
		if (!selected.intersectsNode(row)) continue;
		const index = indexOf(row);
		if (index < 0) continue;
		start = Math.min(start, index);
		end = Math.max(end, index + 1);
	}
	return end < 0 ? null : { start, end };
}

/** Hard limit for outer items; nested row windows need their own bound. */
export const TRANSCRIPT_WINDOW_CAP = 120;
/** Approximate nested markup work, in addition to the outer-row cap. A single
 * oversize message is shown in full, on its own: history is never truncated. */
export const TRANSCRIPT_CONTENT_BUDGET = 12_000;

import type { ChatMessage } from '$lib/stores/types';
import type { DisplayItem } from '$lib/utils/work-log';
const contentCosts = new WeakMap<DisplayItem, number>();
function messageCost(message: ChatMessage): number {
	const content = message.content ?? '';
	// Fences/tables expand into many elements (especially syntax-token spans).
	// Plain prose is cheap. No parse, sanitize or DOM creation is needed to pick
	// the window; the item identity memo makes warm navigation a constant read.
	const rich = /```|~~~|\|[^\n]+\|/.test(content);
	return 64 + Math.ceil(content.length / (rich ? 4 : 80));
}
export function transcriptItemCost(item: DisplayItem): number {
	const hit = contentCosts.get(item);
	if (hit !== undefined) return hit;
	const cost = item.kind === 'message' ? messageCost(item.message)
		: item.kind === 'system_group' ? item.messages.reduce((sum, message) => sum + messageCost(message), 32)
		: item.sequence.reduce((sum, entry) => sum +
			((item.mode === 'settled' ? !entry.hiddenWhenCollapsed : !entry.foldsInRun) ? messageCost(entry.message) : 0), 96);
	contentCosts.set(item, cost);
	return cost;
}

function rangeAt(start: number, count: number, cap: number, previous?: TranscriptRange): TranscriptRange {
	if (!Number.isInteger(count) || count < 0 || !Number.isInteger(cap) || cap < 1) {
		throw new RangeError('Item count must be non-negative and cap must be positive integers');
	}
	start = Math.max(0, Math.min(Math.floor(start), Math.max(0, count - cap)));
	const end = Math.min(count, start + cap);
	return previous?.start === start && previous.end === end ? previous : { start, end };
}

/** Per-view range controller. No timers or idle expansion: only explicit
 * navigation moves into history. Apply the same controller to rows inside a
 * large group: an outer-item cap alone cannot bound nested mounted rows. */
export function createTranscriptWindow({ cap = TRANSCRIPT_WINDOW_CAP, step = 30, cost, budget = Infinity, getPinnedRange }: {
	cap?: number; step?: number; cost?: (index: number) => number; budget?: number;
	/** Only currently mounted, selected rows may be pinned; never retained DOM trees. */
	getPinnedRange?: () => TranscriptRange | null
} = {}) {
	if (!Number.isInteger(cap) || cap < 1 || !Number.isInteger(step) || step < 1 || !(budget > 0)) {
		throw new RangeError('Transcript cap, step and content budget must be positive');
	}
	step = Math.min(step, Math.max(1, cap - 1));
	let count = 0;
	let range: TranscriptRange = { start: 0, end: 0 };
	let followsTail = true;
	let hidden = false;
	let streaming = false;
	const weighted = !!cost && Number.isFinite(budget);
	const charge = (index: number) => Math.max(1, cost?.(index) ?? 1);
	let selectionLimited = false;
	function commit(start: number, end: number): TranscriptRange {
		selectionLimited = false;
		const pinned = getPinnedRange?.();
		if (count && pinned && pinned.end > pinned.start) {
			const first = Math.max(0, pinned.start), last = Math.min(count, pinned.end);
			const requestedStart = start, requestedEnd = end;
			// A native selection can only cover the already-mounted (<= cap) rows.
			// If a model rewrite moves those keys further apart, freeze navigation
			// rather than admitting an unbounded new range.
			if (last - first > cap) { selectionLimited = true; return range; }
			start = Math.min(start, first);
			end = Math.max(end, last);
			const older = requestedStart < range.start;
			const trim = () => {
				if (older && end > last) return --end;
				if (start < first) return start++;
				if (end > last) return --end;
				return null;
			};
			while (end - start > cap && trim() !== null) { /* retain the selected span */ }
			if (weighted) {
				let used = 0;
				for (let i = start; i < end; i++) used += charge(i);
				while (used > budget) {
					const removed = trim();
					if (removed === null) break;
					used -= charge(removed);
				}
				// If selected bodies themselves grow beyond the content budget, keep
				// only that span until collapse; admit no additional unselected rows.
			}
			selectionLimited = start !== requestedStart || end !== requestedEnd;
		}
		if (range.start !== start || range.end !== end) range = { start, end };
		return range;
	}
	function startingRange(start: number): TranscriptRange {
		if (!weighted) return rangeAt(start, count, cap, range);
		start = Math.max(0, Math.min(Math.floor(start), Math.max(0, count - 1)));
		let end = start, used = 0;
		while (end < count && end - start < cap) {
			const next = charge(end);
			if (end > start && used + next > budget) break;
			used += next; end++;
		}
		return { start, end };
	}
	function endingRange(end: number): TranscriptRange {
		if (!weighted) return startingRange(end - cap);
		end = Math.max(0, Math.min(count, end));
		let start = end, used = 0;
		while (start > 0 && end - start < cap) {
			const next = charge(start - 1);
			if (start < end && used + next > budget) break;
			used += next; start--;
		}
		return { start, end };
	}
	function move(start: number): TranscriptRange {
		const next = startingRange(start);
		return commit(next.start, next.end);
	}
	function endingAt(end: number): TranscriptRange {
		const next = endingRange(end);
		return commit(next.start, next.end);
	}
	const shift = () => weighted ? Math.min(step, Math.max(1, Math.floor((range.end - range.start) / 4))) : step;
	function toTail(): TranscriptRange { followsTail = true; return endingAt(count); }
	function extendOlder(): TranscriptRange {
		followsTail = false;
		if (!weighted) return move(range.start - step);
		if (!range.start) return range;
		const previous = range;
		let next = endingRange(range.end - shift());
		if (next.start >= previous.start || next.end <= previous.start) {
			// Include the first older row, then keep the largest overlapping prefix
			// that fits. Compute candidates without committing/pinning twice.
			next = endingRange(startingRange(previous.start - 1).end);
		}
		return commit(next.start, next.end);
	}
	return {
		cap,
		get visibleRange() { return range; },
		get selectionLimited() { return selectionLimited; },
		get followsTail() { return followsTail; },
		get isStreaming() { return streaming; },
		setItemCount(next: number, options: { followTail?: boolean; hidden?: boolean } = {}): TranscriptRange {
			if (!Number.isInteger(next) || next < 0) throw new RangeError('Item count must be a non-negative integer');
			// Visibility and the current item vector form one transaction. Resuming
			// with the PREVIOUS count can charge rows removed by a session switch.
			count = next;
			hidden = options.hidden ?? hidden;
			followsTail = options.followTail ?? followsTail;
			if (hidden && count >= range.end) return range;
			return followsTail ? endingAt(count) : move(range.start);
		},
		extendOlder,
		loadOlder: extendOlder,
		extendNewer(): TranscriptRange {
			const previous = range;
			let next = startingRange(range.start + shift());
			if (weighted && previous.end < count && (next.end <= previous.end || next.start >= previous.end)) {
				// A heterogeneous body can block the usual step while a smaller
				// suffix still fits beside the first new row. Retain that keyed
				// overlap before accepting an unavoidable disjoint window.
				next = startingRange(endingRange(previous.end + 1).start);
			}
			const committed = commit(next.start, next.end);
			followsTail = committed.end === count;
			return committed;
		},
		windowAround(index: number): TranscriptRange {
			if (!Number.isInteger(index) || index < 0 || index >= count) return range;
			followsTail = false;
			if (!weighted) return move(index - Math.floor(cap / 2));
			let start = index, end = index + 1, used = charge(index);
			let left = true, right = true;
			while (end - start < cap && (left || right)) {
				if (left && start > 0 && used + charge(start - 1) <= budget) { used += charge(--start); }
				else left = false;
				if (end - start === cap) break;
				if (right && end < count && used + charge(end) <= budget) { used += charge(end++); }
				else right = false;
			}
			return commit(start, end);
		},
		/** Saved scrollTop is relative to this exact bounded range, not cap/2. */
		restoreRange(saved: TranscriptRange): TranscriptRange { followsTail = false; return move(saved.start); },
		toTail,
		onStreamingChange(value: boolean): TranscriptRange { streaming = value; return range; },
		pauseWhenHidden(value: boolean): TranscriptRange {
			hidden = value;
			return hidden ? range : followsTail ? endingAt(count) : move(range.start);
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
