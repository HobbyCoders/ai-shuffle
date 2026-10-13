/**
 * Coordinate + modifier mapping for the live browser panel.
 *
 * The screencast frame is drawn into a `<canvas>` that letterboxes the page
 * image (aspect-preserving fit). A pointer event lands in canvas/client space;
 * the backend's CDP input dispatch wants coordinates in PAGE CSS pixels
 * (0..deviceWidth × 0..deviceHeight, the visual-viewport frame). These helpers
 * are the single source of truth for that mapping and for the CDP modifier
 * bitmask — kept pure so they're unit-testable without a DOM.
 */

/** Frame metadata as delivered on a `{"type":"frame"}` message. */
export interface FrameMetadata {
	deviceWidth: number;
	deviceHeight: number;
	pageScaleFactor?: number;
	offsetTop?: number;
	scrollOffsetX?: number;
	scrollOffsetY?: number;
}

/** Minimal rect shape (a real DOMRect satisfies this). */
export interface CanvasRect {
	left: number;
	top: number;
	width: number;
	height: number;
}

export interface Point {
	x: number;
	y: number;
}

/** Anything with the four DOM modifier flags (MouseEvent / KeyboardEvent / WheelEvent). */
export interface ModifierFlags {
	altKey: boolean;
	ctrlKey: boolean;
	metaKey: boolean;
	shiftKey: boolean;
}

/**
 * Scale factor that fits a `deviceWidth × deviceHeight` frame inside the canvas
 * while preserving aspect ratio (the smaller of the two axis ratios — "contain").
 * Returns 0 for a degenerate frame so callers can guard against divide-by-zero.
 */
export function fitScale(rect: CanvasRect, meta: FrameMetadata): number {
	if (meta.deviceWidth <= 0 || meta.deviceHeight <= 0) return 0;
	if (rect.width <= 0 || rect.height <= 0) return 0;
	return Math.min(rect.width / meta.deviceWidth, rect.height / meta.deviceHeight);
}

/** Letterbox offsets (px) of the drawn image inside the canvas, given the fit scale. */
export function letterbox(rect: CanvasRect, meta: FrameMetadata, scale: number): Point {
	const drawnW = meta.deviceWidth * scale;
	const drawnH = meta.deviceHeight * scale;
	return {
		x: (rect.width - drawnW) / 2,
		y: (rect.height - drawnH) / 2,
	};
}

function clamp(v: number, min: number, max: number): number {
	return v < min ? min : v > max ? max : v;
}

/**
 * Map a client-space pointer coordinate (e.g. `event.clientX/clientY`) to a page
 * CSS-pixel coordinate inside the frame, accounting for the canvas position, the
 * aspect-preserving letterbox offsets, and the fit scale. The result is clamped
 * to `[0, deviceWidth] × [0, deviceHeight]` so an event on the letterbox bars
 * still maps to a valid edge coordinate. Returns `null` for a degenerate frame.
 */
export function canvasToPage(
	clientX: number,
	clientY: number,
	rect: CanvasRect,
	meta: FrameMetadata,
): Point | null {
	const scale = fitScale(rect, meta);
	if (scale <= 0) return null;
	const off = letterbox(rect, meta, scale);
	const localX = clientX - rect.left - off.x;
	const localY = clientY - rect.top - off.y;
	return {
		x: clamp(localX / scale, 0, meta.deviceWidth),
		y: clamp(localY / scale, 0, meta.deviceHeight),
	};
}

/** Page-CSS-pixel rect (e.g. an element's `getBoundingClientRect` in the page). */
export interface PageRect {
	x: number;
	y: number;
	width: number;
	height: number;
}

/**
 * Map a page-CSS-pixel rect into canvas-local coordinates (the inverse of
 * `canvasToPage`, for drawing element highlights over the screencast). The
 * result is NOT clamped — a rect partially outside the viewport draws
 * partially outside the letterboxed image, which the canvas clips naturally.
 * Returns `null` for a degenerate frame.
 */
export function pageRectToCanvas(
	pageRect: PageRect,
	rect: CanvasRect,
	meta: FrameMetadata,
): { left: number; top: number; width: number; height: number } | null {
	const scale = fitScale(rect, meta);
	if (scale <= 0) return null;
	const off = letterbox(rect, meta, scale);
	return {
		left: off.x + pageRect.x * scale,
		top: off.y + pageRect.y * scale,
		width: pageRect.width * scale,
		height: pageRect.height * scale,
	};
}

// ─── CDP modifier bitmask ──────────────────────────────────────────────────────
// Chrome DevTools Protocol Input.dispatch* modifiers: 1=Alt, 2=Ctrl, 4=Meta, 8=Shift.

export const MOD_ALT = 1;
export const MOD_CTRL = 2;
export const MOD_META = 4;
export const MOD_SHIFT = 8;

/** Build the CDP modifier bitmask from a DOM event's modifier flags. */
export function modifiersFromEvent(e: ModifierFlags): number {
	let mask = 0;
	if (e.altKey) mask |= MOD_ALT;
	if (e.ctrlKey) mask |= MOD_CTRL;
	if (e.metaKey) mask |= MOD_META;
	if (e.shiftKey) mask |= MOD_SHIFT;
	return mask;
}
