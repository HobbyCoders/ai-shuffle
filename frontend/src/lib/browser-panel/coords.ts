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

/**
 * Modifier bitmask for the REMOTE browser. The agent's Chrome runs on Linux,
 * where Cmd has no shortcut meaning, so Meta is sent as Ctrl: Cmd+A, Cmd+click
 * and friends then do what a Mac user expects.
 */
export function remoteModifiers(e: ModifierFlags): number {
	return modifiersFromEvent({ altKey: e.altKey, ctrlKey: e.ctrlKey || e.metaKey, metaKey: false, shiftKey: e.shiftKey });
}

/** A DOM KeyboardEvent's fields that the remote key translation reads. */
export interface KeyLike extends ModifierFlags {
	key: string;
	code: string;
	keyCode: number;
	getModifierState?: (key: string) => boolean;
}

export interface RemoteKey {
	key: string;
	code: string;
	keyCode: number;
	text: string;
	modifiers: number;
}

const META_AS_CTRL: Record<string, string> = { MetaLeft: 'ControlLeft', MetaRight: 'ControlRight', OSLeft: 'ControlLeft', OSRight: 'ControlRight' };

/**
 * Translate a local key event into the remote key payload fields.
 *
 * - Text rides only on keydown of a single-character key, and never while
 *   Ctrl/Cmd is held: Cmd+S must not also type "s" into the page.
 * - AltGr characters (Windows/Linux layouts report Ctrl+Alt with them) are
 *   typed: the character is sent with Ctrl/Alt cleared so the remote sees text,
 *   not a Ctrl+Alt shortcut.
 * - The Meta key itself becomes Control (see `remoteModifiers`).
 */
export function remoteKey(e: KeyLike, event: 'down' | 'up'): RemoteKey {
	const printable = e.key.length === 1 || [...e.key].length === 1;
	const altGraph = printable && !e.metaKey && !!e.getModifierState?.('AltGraph');
	let modifiers = remoteModifiers(e);
	if (altGraph) modifiers &= ~(MOD_CTRL | MOD_ALT);
	const shortcut = (e.ctrlKey || e.metaKey) && !altGraph;
	const isMeta = e.key === 'Meta' || e.key === 'OS';
	return {
		key: isMeta ? 'Control' : e.key,
		code: isMeta ? (META_AS_CTRL[e.code] ?? 'ControlLeft') : e.code,
		keyCode: isMeta ? 17 : e.keyCode,
		text: event === 'down' && printable && !shortcut ? e.key : '',
		modifiers,
	};
}

/** Pointer button names the remote accepts; `none` is a plain hover. */
export type RemoteButton = 'none' | 'left' | 'middle' | 'right';

/** DOM `MouseEvent.button` (the button that changed) → remote button name. */
export function buttonName(button: number): RemoteButton {
	return button === 0 ? 'left' : button === 1 ? 'middle' : button === 2 ? 'right' : 'none';
}

/**
 * The button reported on a move: the held button during a drag (primary
 * first, like Chrome itself), `none` for a hover. `buttons` is the DOM
 * bitmask (1 left, 2 right, 4 middle), which the remote also takes as-is.
 */
export function heldButton(buttons: number): RemoteButton {
	return buttons & 1 ? 'left' : buttons & 2 ? 'right' : buttons & 4 ? 'middle' : 'none';
}

/** Clicks closer than this in time (ms) and distance (CSS px) count together. */
export const MULTI_CLICK_MS = 500;
export const MULTI_CLICK_SLOP_PX = 4;

/**
 * Counts consecutive presses so double and triple clicks register: Chrome's
 * PointerEvent.detail is always 0, so the browser can't tell us. The remote
 * accepts counts 1..3; a fourth quick click stays a triple click.
 */
export class ClickCounter {
	private last: { time: number; x: number; y: number; button: number; count: number } | null = null;

	press(time: number, x: number, y: number, button: number): number {
		const prev = this.last;
		const repeat =
			!!prev &&
			prev.button === button &&
			time - prev.time <= MULTI_CLICK_MS &&
			Math.abs(x - prev.x) <= MULTI_CLICK_SLOP_PX &&
			Math.abs(y - prev.y) <= MULTI_CLICK_SLOP_PX;
		const count = repeat ? Math.min(prev!.count + 1, 3) : 1;
		this.last = { time, x, y, button, count };
		return count;
	}

	reset() {
		this.last = null;
	}
}
