/**
 * Where a desktop sidebar popover opens when a row opened it.
 *
 * A row in the upper half of the panel gets the popover just under it; a row
 * in the lower half (the Snoozed lane rests at the bottom) gets it just above,
 * so the picker never jumps to the top of the panel, far from the chat it is for.
 */

/** The opener's edges and the panel's height, in px from the panel's top. */
export interface SheetAnchor {
	top: number;
	bottom: number;
	height: number;
}

export interface AnchoredPopover {
	side: 'below' | 'above';
	/** CSS `top` in px, or null when the popover hangs from `bottom`. */
	top: number | null;
	/** CSS `bottom` in px, or null when the popover sits at `top`. */
	bottom: number | null;
	maxHeight: number;
}

/** Space between the row and the popover. */
const GAP = 4;
/** The popover never touches the panel's edge. */
const EDGE = 8;
/** Below this the popover would be useless, so it gets this much and scrolls. */
const MIN_HEIGHT = 120;

const clamp = (v: number, lo: number, hi: number) => Math.min(Math.max(v, lo), hi);

export function anchoredPopover({ top, bottom, height }: SheetAnchor): AnchoredPopover {
	// A row half scrolled out of view still anchors to the part that shows.
	const rowTop = clamp(top, 0, height);
	const rowBottom = clamp(bottom, rowTop, height);
	if ((rowTop + rowBottom) / 2 < height / 2) {
		const at = Math.min(rowBottom + GAP, Math.max(EDGE, height - EDGE - MIN_HEIGHT));
		return { side: 'below', top: at, bottom: null, maxHeight: Math.max(MIN_HEIGHT, height - at - EDGE) };
	}
	const from = Math.min(height - rowTop + GAP, Math.max(EDGE, height - EDGE - MIN_HEIGHT));
	return { side: 'above', top: null, bottom: from, maxHeight: Math.max(MIN_HEIGHT, height - from - EDGE) };
}
