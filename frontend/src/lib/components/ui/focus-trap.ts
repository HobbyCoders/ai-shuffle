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
	'[tabindex]:not([tabindex="-1"])'
].join(',');

export interface FocusTrapOptions {
	/** Selector (inside the node) for the element to focus first. */
	initial?: string;
}

export function focusableWithin(node: HTMLElement): HTMLElement[] {
	return Array.from(node.querySelectorAll<HTMLElement>(FOCUSABLE)).filter(
		(el) => el.getAttribute('tabindex') !== '-1' && !el.closest('[hidden], [inert]')
	);
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
