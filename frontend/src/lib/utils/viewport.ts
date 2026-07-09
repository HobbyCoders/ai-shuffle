/**
 * Viewport / responsive helpers for slide-out panels.
 *
 * The diff-review and changed-files panels render as a side drawer on wide
 * screens and fullscreen on handheld screens. The cutoff is intentionally
 * wider than the 640px Tailwind `sm` breakpoint: unfolded foldables (e.g. the
 * Galaxy Z Fold inner screen ≈ 884px CSS wide) exceed 640px yet are still
 * held in the hand, where a partial-width drawer feels broken. 1080px matches
 * the app-wide mobile cutoff already used for touch fallbacks in
 * ChangedFilesPanel.svelte.
 */

/** Width (px) at or below which slide-out panels render fullscreen. */
export const PANEL_FULLSCREEN_MAX_WIDTH = 1080;

/**
 * True when a slide-out panel should render fullscreen at the given viewport
 * width. Pair with the CSS backstop `@media (max-width: 1080px)` so behavior
 * holds before hydration too.
 */
export function isPanelFullscreenWidth(width: number): boolean {
	return width <= PANEL_FULLSCREEN_MAX_WIDTH;
}
