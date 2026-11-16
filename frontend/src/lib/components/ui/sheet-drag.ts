/**
 * Drag a phone bottom sheet down to dismiss it.
 *
 * The pure half of the gesture, shared by the sheets that offer it
 * (SidebarSheet, AttachProjectSheet) so it can be tested without touch
 * events. Each sheet owns its listeners and its `--sheet-drag` offset.
 *
 * A touch first moves through a small slop. Past it, a mostly downward move
 * on an eligible start drags the sheet with the finger; anything else (an
 * upward or sideways move, or a start inside content that is scrolled away
 * from its top) belongs to the page and the gesture lets go. On release the
 * sheet closes when it travelled far enough or was flicked down, and springs
 * back otherwise.
 */

import { FLICK_STALE_MS, FlickTracker, coalescedMoves } from '$lib/utils/swipe-arm';

/** Movement before the gesture picks a direction. Below it, a touch is a tap. */
export const DRAG_SLOP = 8;
/** Past this pull, letting go closes the sheet. */
export const DISMISS_DISTANCE = 70;
/** A downward flick this fast (px/ms) closes the sheet from a shorter pull. */
export const FLICK_VELOCITY = 0.5;
/** The shortest pull a flick may close from, so a tap's jitter never does. */
export const FLICK_MIN_DISTANCE = 16;
/** A release this long after the last move was a hold, not a flick. */
export { FLICK_STALE_MS };

export interface DragPoint {
	x: number;
	y: number;
	/** Milliseconds, e.g. `event.timeStamp`. */
	time: number;
}

export type SheetDragMode = 'pending' | 'drag' | 'scroll';

export interface SheetDrag {
	start: DragPoint;
	/** Downward speed at the last move, px/ms, over the last few moves. */
	velocity: number;
	/** The finger's downward positions, read by the shared flick reading (#1637). */
	flick: FlickTracker;
	/** False when the touch began where a downward move must scroll content. */
	eligible: boolean;
	mode: SheetDragMode;
	/** How far the sheet sits below its resting place, px (never negative). */
	offset: number;
}

export function startSheetDrag(point: DragPoint, eligible = true): SheetDrag {
	const flick = new FlickTracker();
	flick.addAt(point.y, point.time);
	return { start: point, velocity: 0, flick, eligible, mode: eligible ? 'pending' : 'scroll', offset: 0 };
}

/**
 * Feed a move. Returns the mode: `drag` means the caller should cancel the
 * native scroll and move the sheet to `offset`; `scroll` means the gesture
 * let go and the page keeps the touch.
 */
export function moveSheetDrag(state: SheetDrag, point: DragPoint): SheetDragMode {
	if (state.mode === 'scroll') return state.mode;
	state.flick.addAt(point.y, point.time);
	const dx = point.x - state.start.x;
	const dy = point.y - state.start.y;
	if (state.mode === 'pending') {
		if (Math.max(Math.abs(dx), Math.abs(dy)) <= DRAG_SLOP) return state.mode;
		state.mode = dy > 0 && dy > Math.abs(dx) ? 'drag' : 'scroll';
		if (state.mode === 'scroll') return state.mode;
	}
	// Over the last few moves, so one jittery event cannot fake or hide a flick.
	state.velocity = state.flick.velocityAt();
	state.offset = Math.max(0, dy);
	return state.mode;
}

/**
 * A pointermove's positions: every one the browser coalesced into it (a busy
 * page hands over a quick pull in one event), else the event's own.
 */
export function pointerDragPoints(e: PointerEvent): DragPoint[] {
	return coalescedMoves(e).map((m) => ({ x: m.clientX, y: m.clientY, time: m.timeStamp }));
}

/**
 * Add a touch's pointermove to a drag that touch events drive: touch events
 * carry no coalesced moves, the pointer events for the same finger do. Only
 * the speed reading takes them; the touch events still move the sheet.
 */
export function sampleSheetDrag(state: SheetDrag, e: PointerEvent): void {
	if (state.mode === 'scroll') return;
	for (const point of pointerDragPoints(e)) state.flick.addAt(point.y, point.time);
}

/**
 * Would letting go at `releaseTime` (the touchend / pointerup `timeStamp`)
 * close the sheet? A finger held still sends no moves, so the last measured
 * speed goes stale: past FLICK_STALE_MS without a move, it counts as zero.
 */
export function sheetDragDismisses(state: SheetDrag, releaseTime: number, distance = DISMISS_DISTANCE): boolean {
	if (state.mode !== 'drag') return false;
	if (state.offset > distance) return true;
	const velocity = state.flick.velocityAt(releaseTime);
	return state.offset > FLICK_MIN_DISTANCE && velocity > FLICK_VELOCITY;
}

/** Fields and sliders keep their own touch; a drag never starts on them. */
const OWN_GESTURE = [
	'input',
	'textarea',
	'select',
	'[contenteditable]:not([contenteditable="false"])',
	'[role="slider"]',
	'[data-no-sheet-drag]'
].join(', ');

/**
 * May a touch that starts on `target` drag the sheet down?
 *
 * Not from a field, and not from inside content scrolled away from its top
 * (up to and including the sheet itself): there a downward move scrolls the
 * content back, so the list keeps scrolling until it reaches its top.
 */
export function canStartSheetDrag(target: EventTarget | null, sheet: Element): boolean {
	const el = target instanceof Element ? target : target instanceof Node ? target.parentElement : null;
	if (!el || !sheet.contains(el)) return false;
	if (el.closest(OWN_GESTURE)) return false;
	for (let node: Element | null = el; node; node = node.parentElement) {
		if (node.scrollTop > 0) return false;
		if (node === sheet) break;
	}
	return true;
}
