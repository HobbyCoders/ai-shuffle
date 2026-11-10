/**
 * The phone controls sheet uses `focusModelSearch` to put focus in the model
 * page's search box (#1339): with a fine pointer it focuses the box, with a
 * coarse one it leaves focus alone so no on-screen keyboard opens.
 */
import { afterEach, describe, expect, it, vi } from 'vitest';
import { canFocusModelSearch, focusModelSearch } from './model-picker-focus';

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
