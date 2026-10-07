/**
 * The mobile drawer's swipe: a right swipe anywhere on the main screen pulls
 * the drawer open, a left swipe inside it pushes it closed, and the drawer
 * follows the finger in between.
 *
 * These helpers are the pure half of that gesture so it can be tested
 * without touch events. AppTopBar owns the listeners and the rendering.
 */

/** Movement before the gesture picks an axis. Below it, a touch is a tap. */
export const AXIS_SLOP = 8;
/** Past this share of the screen width, letting go completes the move. */
export const SETTLE_FRACTION = 0.3;
/** A flick this fast (px/ms) completes the move whatever the distance. */
export const FLICK_VELOCITY = 0.5;

export type DrawerSwipe = 'open' | 'close';

/**
 * Decide what the first real movement means: `true` takes the gesture (it is
 * horizontal and heads the right way), `false` gives it up to the page
 * (vertical scroll, or the wrong direction), `null` waits for more movement.
 */
export function lockAxis(dx: number, dy: number, swipe: DrawerSwipe): boolean | null {
	if (Math.max(Math.abs(dx), Math.abs(dy)) <= AXIS_SLOP) return null;
	if (Math.abs(dx) <= Math.abs(dy)) return false;
	return swipe === 'open' ? dx > 0 : dx < 0;
}

/** How far the drawer sits from where it started, clamped to the screen. */
export function dragOffset(dx: number, width: number, swipe: DrawerSwipe): number {
	return swipe === 'open' ? Math.min(Math.max(dx, 0), width) : Math.max(Math.min(dx, 0), -width);
}

/**
 * On release, does the move complete? Either the drawer travelled far enough
 * or the finger was still moving fast in the swipe's direction.
 */
export function completes(offset: number, velocity: number, width: number, swipe: DrawerSwipe): boolean {
	const sign = swipe === 'open' ? 1 : -1;
	return offset * sign > width * SETTLE_FRACTION || velocity * sign > FLICK_VELOCITY;
}

/** Elements whose own horizontal gesture outranks the drawer's. */
const OWN_GESTURE = [
	'input',
	'textarea',
	'select',
	'[contenteditable]:not([contenteditable="false"])',
	'[role="slider"]',
	'[role="dialog"]',
	'[role="menu"]',
	'[aria-modal="true"]',
	'[data-no-drawer-swipe]'
].join(', ');

/**
 * May a touch that starts on `target` drive the drawer?
 *
 * Not from a field, a slider, an open dialog or menu, or anything marked
 * `data-no-drawer-swipe`. Not from a session row when closing, because a left
 * swipe on a row settles it. And not from inside something that can still
 * scroll sideways in the swipe's direction (a wide code block, a table), so
 * that content keeps its own scroll.
 */
export function canStartDrawerSwipe(target: EventTarget | null, swipe: DrawerSwipe): boolean {
	const el = target instanceof Element ? target : target instanceof Node ? target.parentElement : null;
	if (!el) return false;
	if (el.closest(OWN_GESTURE)) return false;
	if (swipe === 'close' && el.closest('[data-session-card]')) return false;
	const selection = typeof window !== 'undefined' ? window.getSelection?.() : null;
	if (selection && !selection.isCollapsed) return false;
	for (let node: Element | null = el; node && node !== document.body; node = node.parentElement) {
		if (node.scrollWidth <= node.clientWidth || node.clientWidth === 0) continue;
		if (!/auto|scroll/.test(getComputedStyle(node).overflowX)) continue;
		// A right swipe drags content toward its start; only a node already
		// scrolled away from its start can use it. A left swipe is the reverse.
		const canScroll = swipe === 'open'
			? node.scrollLeft > 0
			: node.scrollLeft + node.clientWidth < node.scrollWidth - 1;
		if (canScroll) return false;
	}
	return true;
}
