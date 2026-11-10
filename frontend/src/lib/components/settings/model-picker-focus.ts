/**
 * Where focus goes when a model picker opens (#1339).
 *
 * `ModelPickerPanel` moves its highlight with the arrow keys from its search
 * box, so a picker opened with the mouse or Enter must put focus there or the
 * arrows do nothing. On a touch-first device focusing a text field raises the
 * on-screen keyboard over the list, so there focus stays where it was.
 */

/** True when opening a picker may focus its search box (not a touch-first device). */
export function canFocusModelSearch(): boolean {
	if (typeof window === 'undefined' || typeof window.matchMedia !== 'function') return true;
	return !window.matchMedia('(pointer: coarse)').matches;
}

/**
 * True when a `focusout` moved keyboard focus to an element outside every
 * root: Tab or Shift+Tab left an open picker, so it should close (#1407).
 * A focus that goes nowhere (a press on plain panel text, the window losing
 * focus, a focused row filtered away) is not leaving: the outside press
 * handlers deal with real outside clicks.
 */
export function focusLeftRoots(event: FocusEvent, ...roots: (Node | null | undefined)[]): boolean {
	const next = event.relatedTarget;
	if (!(next instanceof Node)) return false;
	return !roots.some((root) => root?.contains(next));
}

/**
 * True when Escape on an open picker should hand focus back to its trigger:
 * focus is inside one of the roots, or it was lost to the page (on the body
 * or nowhere), so nothing else holds it.
 */
export function pickerHoldsFocus(...roots: (Node | null | undefined)[]): boolean {
	const active = document.activeElement;
	if (!active || active === document.body) return true;
	return roots.some((root) => root?.contains(active));
}

/**
 * Keep keyboard focus where it is when the mouse presses a part of a model
 * picker panel that cannot take focus itself (a group name, the list's
 * padding, the context limit's text). Otherwise the browser moves focus to the
 * page while the panel stays open, and the arrows and Escape stop working.
 * Fields, buttons and rows take focus as usual, so the caret and text
 * selection in the search box are untouched. Every mouse button counts: a
 * middle or right press moves focus too (the context menu still opens).
 * Touch is left alone: there a tap on plain text should still drop focus and
 * close the on-screen keyboard. The browser's mousedown after a tap does not
 * say it came from touch, so `notePanelPointer` (on pointerdown) records the
 * input type first.
 */
let lastPanelPointer = 'mouse';
export function notePanelPointer(event: PointerEvent) {
	lastPanelPointer = event.pointerType;
}
export function keepFocusOnPanelPress(event: MouseEvent) {
	if (lastPanelPointer === 'touch') return;
	const panel = event.currentTarget;
	const target = event.target;
	if (!(panel instanceof Element) || !(target instanceof Element)) return;
	const focusable = target.closest('input, textarea, select, label, a[href], button, [tabindex], [contenteditable]');
	if (focusable && panel.contains(focusable)) return;
	event.preventDefault();
}

/**
 * Focus the search box of the model picker panel inside `root`. Returns false,
 * and leaves focus alone, on a touch-first device or when there is no panel.
 */
export function focusModelSearch(root: ParentNode | null | undefined): boolean {
	if (!root || !canFocusModelSearch()) return false;
	const input = root.querySelector<HTMLInputElement>('[data-model-picker-panel] .mpp-search-inp');
	if (!input) return false;
	input.focus({ preventScroll: true });
	return true;
}
