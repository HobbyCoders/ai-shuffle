/**
 * Auto-resize a notes textarea to fit its content without jumping the
 * surrounding scroll container.
 *
 * The bug this fixes: the notes editor grows the textarea to fit its content
 * using the classic `height = 'auto'` → `height = scrollHeight` trick. A
 * textarea does NOT grow with its content on its own, so `height = 'auto'`
 * momentarily collapses it to its CSS `min-height`. While collapsed, the
 * enclosing scroll container (`.ns-editorcol`, `overflow-y: auto`) shrinks, and
 * the browser clamps its `scrollTop` toward the top to keep it within the new
 * (smaller) scroll range. Restoring the full height afterwards re-expands the
 * container, but `scrollTop` has already been clamped — so on every keystroke
 * the view jumps to the top and the caret the user is typing at scrolls out of
 * sight.
 *
 * The fix: snapshot the scroller's `scrollTop` before the collapse and restore
 * it after the height is re-expanded. Restoring against the now-larger scroll
 * range yields the original position (clamped only if content actually shrank).
 */

/** Minimal shape of the textarea we resize (satisfied by HTMLTextAreaElement). */
export interface ResizableElement {
	style: Pick<CSSStyleDeclaration, 'height'>;
	scrollHeight: number;
}

/** Minimal shape of the scroll container (satisfied by HTMLElement). */
export interface ScrollableElement {
	scrollTop: number;
}

/**
 * Resize `textarea` to fit its content while preserving `scroller`'s vertical
 * scroll position. Pass `scroller = null` when there is no scrolling ancestor
 * (the resize still happens; there is just nothing to preserve).
 */
export function resizeToFitContent(
	textarea: ResizableElement,
	scroller: ScrollableElement | null
): void {
	const savedScrollTop = scroller ? scroller.scrollTop : 0;
	// Collapse, measure the true content height, then expand to it.
	textarea.style.height = 'auto';
	textarea.style.height = `${textarea.scrollHeight}px`;
	// Re-apply the saved position now that the scroll range is restored.
	if (scroller) scroller.scrollTop = savedScrollTop;
}
