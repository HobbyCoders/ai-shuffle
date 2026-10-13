/**
 * When the terminal panel may take keyboard focus (#1945).
 *
 * A paste can land seconds after the click (a new shell waits for its
 * prompt). By then the owner may be typing somewhere else, and an Enter
 * meant for that field must never reach the shell. So focus moves only when
 * nothing else wants it: the page body, the button that was clicked, the
 * terminal's own input, or a non-text control inside the panel. Never from
 * a text field, in the panel (its search box) or anywhere else.
 */
export function mayTakeFocus(
	active: Element | null,
	panel: Element | null | undefined,
	origin: Element | null = null,
): boolean {
	if (!active) return true;
	const doc = active.ownerDocument;
	if (active === doc?.body || active === doc?.documentElement) return true;
	if (origin && active === origin) return true;
	if (!panel || !panel.contains(active)) return false;
	if (active.classList.contains('xterm-helper-textarea')) return true;
	return !isTextField(active);
}

/** Inputs that take typed text, not buttons, checkboxes and the like. */
const NON_TEXT_INPUTS = new Set(['button', 'submit', 'reset', 'checkbox', 'radio', 'range', 'color', 'file', 'image', 'hidden']);

function isTextField(el: Element): boolean {
	const tag = el.tagName;
	if (tag === 'TEXTAREA' || tag === 'SELECT') return true;
	if (tag === 'INPUT') return !NON_TEXT_INPUTS.has((el as HTMLInputElement).type);
	if ((el as HTMLElement).isContentEditable) return true;
	const editable = el.getAttribute('contenteditable');
	return editable !== null && editable !== 'false';
}
