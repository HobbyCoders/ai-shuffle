/**
 * The phone controls sheet uses `focusModelSearch` to put focus in the model
 * page's search box (#1339): with a fine pointer it focuses the box, with a
 * coarse one it leaves focus alone so no on-screen keyboard opens.
 */
import { afterEach, describe, expect, it, vi } from 'vitest';
import { canFocusModelSearch, focusLeftRoots, focusModelSearch } from './model-picker-focus';

function pointer(kind: 'fine' | 'coarse') {
	vi.stubGlobal('matchMedia', (query: string) => ({ matches: query === `(pointer: ${kind})`, media: query }));
}

function page(): { root: HTMLElement; input: HTMLInputElement; before: HTMLButtonElement } {
	const root = document.createElement('div');
	root.innerHTML =
		'<button type="button">Back</button><div data-model-picker-panel><input class="mpp-search-inp" aria-label="Search models" /></div>';
	document.body.appendChild(root);
	const before = root.querySelector('button') as HTMLButtonElement;
	before.focus();
	return { root, input: root.querySelector('input') as HTMLInputElement, before };
}

afterEach(() => {
	vi.unstubAllGlobals();
	document.body.innerHTML = '';
});

describe('focusModelSearch', () => {
	it('focuses the search box with a fine pointer', () => {
		pointer('fine');
		const { root, input } = page();
		expect(canFocusModelSearch()).toBe(true);
		expect(focusModelSearch(root)).toBe(true);
		expect(document.activeElement).toBe(input);
	});

	it('leaves focus where it was with a coarse pointer', () => {
		pointer('coarse');
		const { root, before } = page();
		expect(canFocusModelSearch()).toBe(false);
		expect(focusModelSearch(root)).toBe(false);
		expect(document.activeElement).toBe(before);
	});

	it('reports false when there is no panel to focus', () => {
		pointer('fine');
		expect(focusModelSearch(null)).toBe(false);
		expect(focusModelSearch(document.createElement('div'))).toBe(false);
	});
});

describe('focusLeftRoots (#1407)', () => {
	function move(to: EventTarget | null) {
		return new FocusEvent('focusout', { relatedTarget: to });
	}

	it('is true only when focus moves to an element outside every root', () => {
		const { root, input, before } = page();
		const panel = root.querySelector('[data-model-picker-panel]') as HTMLElement;
		const elsewhere = document.createElement('button');
		document.body.appendChild(elsewhere);

		expect(focusLeftRoots(move(elsewhere), root)).toBe(true);
		expect(focusLeftRoots(move(input), root)).toBe(false);
		// A second root, as the composer popover's portaled list layer.
		expect(focusLeftRoots(move(input), before, panel)).toBe(false);
		expect(focusLeftRoots(move(elsewhere), before, panel)).toBe(true);
		// A root that is not mounted yet is skipped.
		expect(focusLeftRoots(move(input), null, undefined, root)).toBe(false);
	});

	it('is false when focus goes nowhere (a press on plain text, the window losing focus)', () => {
		const { root } = page();
		expect(focusLeftRoots(move(null), root)).toBe(false);
	});
});
