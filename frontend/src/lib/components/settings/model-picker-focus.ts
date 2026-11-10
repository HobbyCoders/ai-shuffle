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
