import { cleanup, render, screen, waitFor } from '@testing-library/svelte';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { tick } from 'svelte';
import DocsTableOfContents from './DocsTableOfContents.svelte';

const headings = [
	{ id: 'one', title: 'One', level: 2 },
	{ id: 'two', title: 'Two', level: 2 },
	{ id: 'three', title: 'Three', level: 2 }
];

let offsets: number[] = [];

/** A 1000px-tall pane over a 9000px page, headings at 0, 4000 and 8200. */
function pane() {
	const root = document.createElement('div');
	Object.defineProperty(root, 'clientHeight', { value: 1000, configurable: true });
	Object.defineProperty(root, 'scrollHeight', { value: 9000, configurable: true });
	root.getBoundingClientRect = () => ({ top: 0 }) as DOMRect;
	offsets = [0, 4000, 8200];
	offsets.forEach((_, i) => {
		const el = document.createElement('h2');
		el.id = headings[i].id;
		el.getBoundingClientRect = () => ({ top: offsets[i] - root.scrollTop }) as DOMRect;
		root.appendChild(el);
	});
	document.body.appendChild(root);
	return root;
}

const active = () => document.querySelector('.toc-link.active')?.textContent?.trim();
function jump(root: HTMLElement, top: number) {
	root.scrollTop = top;
	root.dispatchEvent(new Event('scroll'));
}

describe('DocsTableOfContents', () => {
	let root: HTMLElement;
	let onResize: () => void;

	beforeEach(() => {
		vi.stubGlobal('ResizeObserver', class {
			constructor(callback: () => void) { onResize = callback; }
			observe() {}
			disconnect() {}
		});
		root = pane();
	});
	afterEach(() => {
		cleanup();
		root.remove();
		vi.unstubAllGlobals();
	});

	async function ready() {
		render(DocsTableOfContents, { headings, scrollContainer: root });
		await waitFor(() => expect(active()).toBe('One'));
	}

	it('follows a jump straight past a heading', async () => {
		await ready();
		jump(root, 4500);
		await tick();
		expect(active()).toBe('Two');
	});

	it('marks the last heading at the end of the page', async () => {
		await ready();
		jump(root, 8000);
		await tick();
		expect(active()).toBe('Three');
	});

	it('a resize after a jump keeps the reader where they jumped to', async () => {
		await ready();
		jump(root, 4500);
		// Narrower text pushes the second heading down the page; the reader was
		// 500px past it and stays 500px past it.
		offsets[1] = 5200;
		Object.defineProperty(root, 'clientWidth', { value: 500, configurable: true });
		onResize();
		expect(root.scrollTop).toBe(5700);
	});

	it('a resize at the top of the page does not move it', async () => {
		await ready();
		offsets[1] = 5200;
		Object.defineProperty(root, 'clientWidth', { value: 500, configurable: true });
		onResize();
		expect(root.scrollTop).toBe(0);
	});

	it('a chosen heading is not overruled by its own smooth scroll', async () => {
		vi.stubGlobal('matchMedia', vi.fn(() => ({ matches: true })));
		await ready();
		document.getElementById('two')!.scrollIntoView = vi.fn();
		screen.getByRole('button', { name: 'Two' }).click();
		await waitFor(() => expect(active()).toBe('Two'));
		jump(root, 100);
		await tick();
		expect(active()).toBe('Two');
	});
});
