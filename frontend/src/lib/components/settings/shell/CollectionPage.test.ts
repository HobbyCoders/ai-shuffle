import { cleanup, render } from '@testing-library/svelte';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { createRawSnippet, flushSync, tick } from 'svelte';
import { settingsNav } from '$lib/settings/navigation.svelte';
import {
	showsOnePane,
	LIST_WIDTH_STORAGE_KEY,
	SPLIT_WIDTH_MIN
} from '$lib/settings/list-width';
import CollectionPage from './CollectionPage.svelte';
import NotFoundCollection from './test-fixtures/NotFoundCollection.svelte';

// Observers by element, so a test can say how wide the page or the list is.
let observers: { el: Element; cb: ResizeObserverCallback }[] = [];
class FakeResizeObserver {
	constructor(private cb: ResizeObserverCallback) {}
	observe(el: Element) {
		observers.push({ el, cb: this.cb });
	}
	disconnect() {
		observers = observers.filter((o) => o.cb !== this.cb);
	}
}
function resize(el: Element | null, width: number) {
	for (const o of observers.filter((o) => o.el === el)) {
		o.cb([{ contentRect: { width } } as ResizeObserverEntry], {} as ResizeObserver);
	}
	flushSync();
}

const list = createRawSnippet(() => ({
	render: () => '<ul><li><button data-settings-item="a" data-settings-nav-item>A</button></li></ul>'
}));
const detail = createRawSnippet(() => ({
	render: () => '<h2 tabindex="-1" data-settings-heading="detail">Item A</h2>'
}));

function setup(props: Record<string, unknown> = {}) {
	const view = render(CollectionPage, { category: 'hooks', list, detail, ...props });
	const coll = view.container.querySelector<HTMLElement>('.st-coll')!;
	const lcol = view.container.querySelector<HTMLElement>('.st-lcol')!;
	return { coll, lcol, listWidth: () => coll.style.getPropertyValue('--st-list-width') };
}

beforeEach(() => {
	observers = [];
	localStorage.clear();
	vi.stubGlobal('ResizeObserver', FakeResizeObserver);
	settingsNav.reset();
	settingsNav.selectCategory('hooks');
});
afterEach(() => {
	cleanup();
	settingsNav.reset();
	vi.unstubAllGlobals();
});

describe('showsOnePane', () => {
	it('needs room for the narrow list and the detail minimum, and waits for a measurement', () => {
		expect(SPLIT_WIDTH_MIN).toBe(720);
		expect(showsOnePane(0)).toBe(false);
		expect(showsOnePane(460)).toBe(true);
		expect(showsOnePane(719)).toBe(true);
		expect(showsOnePane(720)).toBe(false);
		expect(showsOnePane(940)).toBe(false);
	});
});

describe('CollectionPage width', () => {
	it('shows the list or the open item, one at a time, when too narrow for both', async () => {
		const { coll } = setup();
		resize(coll, 460);
		expect(coll.dataset.replace).toBe('list');
		expect(coll.hasAttribute('data-narrow')).toBe(true);

		settingsNav.selectItem('a');
		flushSync();
		expect(coll.dataset.replace).toBe('detail');
		await tick();
		expect(document.activeElement?.textContent).toBe('Item A');

		settingsNav.clearSelection('hooks');
		flushSync();
		expect(coll.dataset.replace).toBe('list');
		await tick();
		expect(document.activeElement?.textContent).toBe('A');
	});

	it('keeps the list beside the detail when there is room, or before it is measured', () => {
		settingsNav.selectItem('a');
		const { coll } = setup();
		expect(coll.dataset.replace).toBeUndefined();
		resize(coll, 940);
		expect(coll.dataset.replace).toBeUndefined();
		expect(coll.hasAttribute('data-narrow')).toBe(false);
		resize(coll, 460);
		expect(coll.dataset.replace).toBe('detail');
		resize(coll, 720);
		expect(coll.dataset.replace).toBeUndefined();
	});

	it('leaves stacked mode to its own steps', () => {
		settingsNav.stacked = true;
		const { coll } = setup();
		resize(coll, 460);
		expect(coll.dataset.replace).toBeUndefined();
		expect(coll.hasAttribute('data-narrow')).toBe(false);
	});

	it('still replaces the list at every width with replaceOnSelect', () => {
		const { coll } = setup({ replaceOnSelect: true });
		resize(coll, 1200);
		expect(coll.dataset.replace).toBe('list');
		expect(coll.hasAttribute('data-narrow')).toBe(false);
	});

	it('moves focus to the open item when the list hides under it', () => {
		settingsNav.selectItem('a');
		const { coll, lcol } = setup();
		resize(coll, 940);
		const row = coll.querySelector<HTMLElement>('[data-settings-item="a"]')!;
		row.focus();
		expect(document.activeElement).toBe(row);

		resize(coll, 460);
		row.blur(); // a hidden element drops focus
		resize(lcol, 0);
		expect(document.activeElement?.textContent).toBe('Item A');
	});

	it('returns the list to the remembered width after a squeeze, without saving', () => {
		localStorage.setItem(LIST_WIDTH_STORAGE_KEY, '420');
		const { coll, listWidth } = setup();
		resize(coll, 1200);
		expect(listWidth()).toBe('420px');
		resize(coll, 600);
		expect(listWidth()).toBe('300px');
		resize(coll, 1200);
		expect(listWidth()).toBe('420px');
		expect(localStorage.getItem(LIST_WIDTH_STORAGE_KEY)).toBe('420');
	});
});

describe('CollectionPage with an item that is only a state', () => {
	function setupNotFound() {
		const view = render(NotFoundCollection);
		const coll = view.container.querySelector<HTMLElement>('.st-coll')!;
		return { coll };
	}
	const pageBack = (coll: HTMLElement) => coll.querySelector<HTMLElement>('.st-dpane > .st-dback .st-back');

	it('gives a not-found item that replaces the list its own Back to the list', async () => {
		settingsNav.selectItem('gone');
		const { coll } = setupNotFound();
		resize(coll, 460);
		expect(coll.dataset.replace).toBe('detail');
		// The rule settings.css shows the page's Back for: a non-idle state
		// straight in the detail pane while the list is hidden.
		expect(coll.matches("[data-replace='detail']:has(> .st-dpane > .st-empty-detail:not([data-idle]))")).toBe(true);
		const back = pageBack(coll)!;
		expect(back.getAttribute('aria-label')).toBe('Back to Hooks');

		back.focus();
		back.click();
		flushSync();
		expect(settingsNav.selectedItem('hooks')).toBeNull();
		expect(coll.dataset.replace).toBe('list');
		expect(pageBack(coll)).toBeNull();
		await tick();
		// The item has no row any more, so the list's first row takes focus.
		expect(document.activeElement?.getAttribute('data-settings-item')).toBe('a');
	});

	it('adds nothing with no item open', () => {
		const { coll } = setupNotFound();
		resize(coll, 460);
		expect(pageBack(coll)).toBeNull();
	});
});
