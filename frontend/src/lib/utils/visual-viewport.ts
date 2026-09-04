/**
 * Keep the app shell sized to the currently visible viewport.
 *
 * `100dvh` tracks browser chrome (the URL bar) but not the virtual keyboard.
 * Android Chrome's default `interactive-widget=resizes-visual` shrinks only
 * the visual viewport, so a 100dvh overflow-hidden shell stays full-screen
 * and the chat composer is covered. Binding `--app-viewport-height` to
 * `visualViewport.height` shrinks the shell to the remaining visible area
 * whether the keyboard overlays or resizes the layout viewport.
 *
 * Offset is intentionally ignored: translating the shell would make it a
 * containing block and break `position: fixed` overlays (toasts, drawers).
 */

export const APP_VIEWPORT_HEIGHT_VAR = '--app-viewport-height';

/** CSS pixel height for the app shell given visual + layout viewport sizes. */
export function visualViewportCssHeight(
	visualHeight: number | undefined,
	innerHeight: number
): string {
	const height = visualHeight && visualHeight > 0 ? visualHeight : innerHeight;
	return `${height}px`;
}

export function applyVisualViewportHeight(
	style: CSSStyleDeclaration,
	visualHeight: number | undefined,
	innerHeight: number
): void {
	style.setProperty(APP_VIEWPORT_HEIGHT_VAR, visualViewportCssHeight(visualHeight, innerHeight));
}

/**
 * Write `--app-viewport-height` on `target` (default: `<html>`) and keep it
 * in sync with visualViewport resize/scroll and window resize.
 * Returns an unbind that also removes the custom property.
 */
export function bindVisualViewport(
	target: { style: CSSStyleDeclaration } = document.documentElement
): () => void {
	const viewport = typeof window === 'undefined' ? null : window.visualViewport;

	const sync = () => {
		applyVisualViewportHeight(target.style, viewport?.height, window.innerHeight);
	};

	sync();

	viewport?.addEventListener('resize', sync);
	viewport?.addEventListener('scroll', sync);
	window.addEventListener('resize', sync);

	return () => {
		viewport?.removeEventListener('resize', sync);
		viewport?.removeEventListener('scroll', sync);
		window.removeEventListener('resize', sync);
		target.style.removeProperty(APP_VIEWPORT_HEIGHT_VAR);
	};
}
