/**
 * focusTrap — Svelte action for modal dialogs.
 *
 * On mount it remembers the element that had focus (the opener) and moves focus
 * into the node: the first element matching `initial` if given, else the first
 * focusable descendant, else the node itself. While mounted, Tab / Shift+Tab
 * wrap inside the node so keyboard focus cannot wander to the page behind the
 * modal. On destroy it returns focus to the opener if it is still in the page.
 *
 * Usage: `<div role="dialog" aria-modal="true" tabindex="-1" use:focusTrap>`
 */

const FOCUSABLE = [
	'a[href]',
	'button:not([disabled])',
	'input:not([disabled]):not([type="hidden"])',
	'select:not([disabled])',
	'textarea:not([disabled])',
	'[contenteditable="true"]',
	'[tabindex]:not([tabindex="-1"])',
	'summary'
].join(',');

export interface FocusTrapOptions {
	/** Selector (inside the node) for the element to focus first. */
	initial?: string;
}

/** The summary that opens and closes `details`: its first `summary` child. */
function summaryOf(details: HTMLDetailsElement): HTMLElement | null {
	return (Array.from(details.children).find((child) => child.tagName === 'SUMMARY') as HTMLElement | undefined) ?? null;
}

/**
 * The elements Tab stops on inside `node`, in document order. Like the
 * browser: a `<details>` element's summary is a stop, and the content of a
 * closed `<details>` (its summary aside) is not.
 */
export function focusableWithin(node: HTMLElement): HTMLElement[] {
	return Array.from(node.querySelectorAll<HTMLElement>(FOCUSABLE)).filter((el) => {
		if (el.getAttribute('tabindex') === '-1' || el.closest('[hidden], [inert]')) return false;
		if (el.tagName === 'SUMMARY') {
			const details = el.parentElement;
			if (!(details instanceof HTMLDetailsElement) || summaryOf(details) !== el) return false;
		}
		for (let d = el.parentElement?.closest('details'); d && node.contains(d); d = d.parentElement?.closest('details')) {
			if (!d.open && !summaryOf(d)?.contains(el)) return false;
		}
		return true;
	});
}

/**
 * Keep a Tab or Shift+Tab press inside `node`: from its last (or first)
 * focusable element focus wraps to the other end. Other keys are ignored. For
 * a modal that stays mounted while closed and so cannot use `focusTrap`.
 */
export function wrapTabWithin(node: HTMLElement, event: KeyboardEvent) {
	if (event.key !== 'Tab') return;
	const focusable = focusableWithin(node);
	if (focusable.length === 0) {
		event.preventDefault();
		node.focus();
		return;
	}
	const first = focusable[0];
	const last = focusable[focusable.length - 1];
	const active = document.activeElement;
	const outside = !node.contains(active);
	if (event.shiftKey && (active === first || active === node || outside)) {
		event.preventDefault();
		last.focus();
	} else if (!event.shiftKey && (active === last || active === node || outside)) {
		event.preventDefault();
		first.focus();
	}
}

export function focusTrap(node: HTMLElement, options: FocusTrapOptions = {}) {
	const opener = document.activeElement instanceof HTMLElement ? document.activeElement : null;
	let initial = options.initial;

	function focusInitial() {
		if (!node.isConnected || node.contains(document.activeElement)) return;
		const target =
			(initial ? node.querySelector<HTMLElement>(initial) : null) ?? focusableWithin(node)[0] ?? node;
		target.focus({ preventScroll: true });
	}

	function onKeydown(event: KeyboardEvent) {
		wrapTabWithin(node, event);
	}

	node.addEventListener('keydown', onKeydown);
	// A microtask lets a sibling `use:portal` reparent the node and child
	// components finish mounting before focus moves.
	queueMicrotask(focusInitial);

	return {
		update(next: FocusTrapOptions = {}) {
			initial = next.initial;
		},
		destroy() {
			node.removeEventListener('keydown', onKeydown);
			if (opener?.isConnected) opener.focus({ preventScroll: true });
		}
	};
}
