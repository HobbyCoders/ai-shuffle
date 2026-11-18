/**
 * floatingMenu — a Svelte action that lifts a dropdown out of its container
 * and anchors it to its trigger in the viewport.
 *
 * A dropdown rendered inside its trigger's box is trapped by whatever that box
 * does to painting: a stacking context (isolation, transform, filter, an
 * animation that fills a transform) caps the menu's z-index at the box's own
 * level, and `overflow` clips it. The new-session agent card is both an
 * isolated stacking context and a filled transform animation, so its menus
 * painted under the composer that follows it (#371).
 *
 * The action moves the node to <body> (the layer the app's other body-level
 * menus and dialogs already use) and positions it `fixed` against the anchor:
 * below it unless the space below is too small and there is more room above
 * (flip), clamped horizontally inside the viewport (shift), and capped to the
 * chosen side's height so a long list scrolls instead of leaving the screen.
 * It follows the anchor on scroll, resize, the visual viewport (phone
 * keyboard) and size changes of either element.
 *
 * The node carries `data-placement="bottom" | "top"` so its entry animation
 * can come from the anchor's side. Scoped styles keep working (the node keeps
 * its classes), but selectors through an ancestor of the original spot no
 * longer match, so style the menu by its own class.
 *
 * Because the node leaves its parent, "click outside" checks must treat the
 * menu as inside: test `menu.contains(target)` as well as the trigger's root.
 */

export type FloatingAlign = 'start' | 'center' | 'end';

export interface FloatingMenuOptions {
	/** The trigger (or its wrapper) the menu hangs from. */
	anchor: HTMLElement | null | undefined;
	/** Which anchor edge the menu lines up with (`center`: centred under it). Default `start` (left). */
	align?: FloatingAlign;
	/** Gap between anchor and menu, px. Default 6. */
	gap?: number;
	/** Minimum distance from the viewport edges, px. Default 8. */
	margin?: number;
	/** `false` leaves the node where it is (an in-flow variant). Default true. */
	enabled?: boolean;
	/**
	 * Keep the side (above or below) chosen when it opened while the menu
	 * stays open, so a list that shrinks as you filter it does not jump to the
	 * other side. It still moves when that side gets too small (a resize).
	 */
	keepPlacement?: boolean;
}

/** Below this many px a kept side gives way to the roomier one. */
const KEPT_SIDE_MIN = 120;

export interface FloatingPosition {
	top: number;
	left: number;
	maxHeight: number;
	placement: 'top' | 'bottom';
}

/** Only z-index this layer needs: above app content, below sheets and dialogs. */
export const FLOATING_MENU_Z = 60;

interface Rect {
	top: number;
	bottom: number;
	left: number;
	right: number;
}

/**
 * Pure placement maths, exported for tests. `size` is the menu's natural
 * size; `view` the visible viewport.
 */
export function computeFloatingPosition(
	anchor: Rect,
	size: { width: number; height: number },
	view: { width: number; height: number },
	{ align = 'start', gap = 6, margin = 8, placement: kept }: Pick<FloatingMenuOptions, 'align' | 'gap' | 'margin'> & { placement?: 'top' | 'bottom' } = {}
): FloatingPosition {
	const below = Math.max(0, view.height - margin - (anchor.bottom + gap));
	const above = Math.max(0, anchor.top - gap - margin);
	// A kept side stays while it can still show a useful part of the list.
	const keptSpace = kept === 'bottom' ? below : kept === 'top' ? above : 0;
	const keepIt = !!kept && (keptSpace >= Math.min(size.height, KEPT_SIDE_MIN) || keptSpace >= (kept === 'bottom' ? above : below));
	// Prefer below; flip only when it does not fit there and above is roomier.
	const placement: 'top' | 'bottom' = keepIt && kept ? kept : size.height <= below || below >= above ? 'bottom' : 'top';
	const space = placement === 'bottom' ? below : above;
	const height = Math.min(size.height, space);
	const top = placement === 'bottom' ? anchor.bottom + gap : anchor.top - gap - height;

	const width = Math.min(size.width, Math.max(0, view.width - margin * 2));
	const preferred = align === 'end'
		? anchor.right - width
		: align === 'center'
			? anchor.left + (anchor.right - anchor.left - width) / 2
			: anchor.left;
	const left = Math.max(margin, Math.min(preferred, view.width - margin - width));

	return { top, left, maxHeight: space, placement };
}

function viewport(): { width: number; height: number } {
	const vv = typeof window !== 'undefined' ? window.visualViewport : null;
	return {
		width: document.documentElement.clientWidth || window.innerWidth,
		height: vv?.height ?? window.innerHeight
	};
}

export function floatingMenu(node: HTMLElement, initial: FloatingMenuOptions) {
	let options = initial;
	if (options.enabled === false) {
		return { update(next: FloatingMenuOptions) { options = next; }, destroy() {} };
	}

	document.body.appendChild(node);
	node.style.position = 'fixed';
	node.style.zIndex = String(FLOATING_MENU_Z);
	node.style.margin = '0';
	// The component's own offsets (top: 100% + gap, right: 0, …) were for the
	// in-flow spot; the action owns top/left from here.
	node.style.right = 'auto';
	node.style.bottom = 'auto';

	let frame = 0;
	let kept: 'top' | 'bottom' | undefined;

	function place() {
		frame = 0;
		const anchor = options.anchor;
		if (!anchor || !node.isConnected) return;
		// Measure the natural size (the stylesheet's own cap still applies):
		// drop the previous viewport cap first. Uncapping can reset the menu's
		// own scroll, so carry it across.
		// A shrink-to-fit width also depends on `left`, so measure from 0.
		const scrollTop = node.scrollTop;
		node.style.maxHeight = '';
		node.style.left = '0px';
		const css = getComputedStyle(node);
		const cssMax = parseFloat(css.maxHeight);
		const pos = computeFloatingPosition(
			anchor.getBoundingClientRect(),
			{ width: node.offsetWidth, height: node.offsetHeight },
			viewport(),
			{ ...options, placement: options.keepPlacement ? kept : undefined }
		);
		if (options.keepPlacement) kept = pos.placement;
		node.style.top = `${Math.round(pos.top)}px`;
		node.style.left = `${Math.round(pos.left)}px`;
		node.style.maxHeight = `${Math.floor(Number.isFinite(cssMax) ? Math.min(cssMax, pos.maxHeight) : pos.maxHeight)}px`;
		if (css.overflowY === 'visible') node.style.overflowY = 'auto';
		node.scrollTop = scrollTop;
		node.dataset.placement = pos.placement;
	}

	function schedule() {
		if (!frame) frame = requestAnimationFrame(place);
	}

	place();

	const vv = window.visualViewport;
	window.addEventListener('resize', schedule);
	window.addEventListener('scroll', schedule, { capture: true, passive: true });
	vv?.addEventListener('resize', schedule);
	vv?.addEventListener('scroll', schedule);
	const observer = typeof ResizeObserver !== 'undefined' ? new ResizeObserver(schedule) : null;
	observer?.observe(node);
	if (options.anchor) observer?.observe(options.anchor);

	return {
		update(next: FloatingMenuOptions) {
			if (next.anchor !== options.anchor) {
				if (options.anchor) observer?.unobserve(options.anchor);
				if (next.anchor) observer?.observe(next.anchor);
			}
			options = next;
			schedule();
		},
		destroy() {
			if (frame) cancelAnimationFrame(frame);
			window.removeEventListener('resize', schedule);
			window.removeEventListener('scroll', schedule, { capture: true } as EventListenerOptions);
			vv?.removeEventListener('resize', schedule);
			vv?.removeEventListener('scroll', schedule);
			observer?.disconnect();
			node.remove();
		}
	};
}
