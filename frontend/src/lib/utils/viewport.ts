/**
 * Viewport helpers for the DESKTOP shell's narrow-window resizing.
 *
 * Width never selects the mobile UI: that is a platform decision owned by
 * `$lib/stores/platform.svelte` (the Android app's WebView). These helpers
 * only let the desktop shell degrade gracefully in a narrow browser window,
 * such as collapsing the sidebar into an overlay or clamping a side drawer to
 * the viewport width.
 */

/** Width (px) at or below which the desktop shell treats the window as narrow. */
export const NARROW_VIEWPORT_MAX_WIDTH = 1080;

/** True when the desktop shell should use its narrow-window layout. */
export function isNarrowViewportWidth(width: number): boolean {
	return width <= NARROW_VIEWPORT_MAX_WIDTH;
}
