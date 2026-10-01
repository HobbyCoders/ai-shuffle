import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, waitFor } from '@testing-library/svelte';
import { getDocByFullSlug, preloadAllDocs } from '$lib/docs/index';
import DocsCard from './DocsCard.svelte';
import DocsContent from './DocsContent.svelte';

afterEach(() => { cleanup(); vi.useRealTimers(); });

describe('DocsContent code copy', () => {
	it('keeps native keyboard semantics and handles a keyboard-originated click once', async () => {
		const writeText = vi.fn().mockResolvedValue(undefined);
		Object.defineProperty(navigator, 'clipboard', { value: { writeText }, configurable: true });
		const { getByRole, container } = render(DocsContent, { content: '```js\nconst value = 1;\n```' });
		const button = getByRole('button', { name: 'Copy' }) as HTMLButtonElement;
		expect(button.type).toBe('button');
		expect(button.tabIndex).toBe(0);
		button.focus();
		expect(document.activeElement).toBe(button);
		// jsdom does not synthesize native activation from key events. Dispatch
		// its resulting click; real Enter/Space activation is checked in browser.
		await fireEvent.click(button, { detail: 0 });
		expect(writeText).toHaveBeenCalledExactlyOnceWith('const value = 1;');
		await fireEvent.click(container.querySelector('code')!);
		expect(writeText).toHaveBeenCalledOnce();
	});

	it('copies through a delegated listener because the sanitizer strips inline handlers', async () => {
		const writeText = vi.fn().mockResolvedValue(undefined);
		Object.defineProperty(navigator, 'clipboard', { value: { writeText }, configurable: true });
		const { container } = render(DocsContent, { content: '```js\nconst value = 1;\n```' });
		const button = container.querySelector('.code-copy-btn') as HTMLButtonElement;
		expect(button).not.toBeNull();
		expect(button.hasAttribute('onclick')).toBe(false);
		await fireEvent.click(button);
		expect(writeText).toHaveBeenCalledWith(expect.stringContaining('const value = 1;'));
		expect(button.textContent).toBe('Copied!');
	});
});

describe('DocsContent in-app links', () => {
	it.each([
		['getting-started/_index', 'Remote access', 'getting-started/remote-access'],
		['getting-started/remote-access', 'First run', 'getting-started/first-run']
	])('opens the real prose link from %s in the bundled Docs card', async (source, linkName, target) => {
		await preloadAllDocs();
		const { layout } = await import('$lib/stores/layout.svelte');
		layout.openFeaturePage('docs');
		const sourceDoc = getDocByFullSlug(source)!;
		const targetDoc = getDocByFullSlug(target)!;
		const view = render(DocsContent, { content: sourceDoc.content });
		const link = view.getByRole('link', { name: linkName });
		expect(link.getAttribute('href')).toBe(`/docs/${target}`);
		const event = new MouseEvent('click', { bubbles: true, cancelable: true });
		link.dispatchEvent(event);
		expect(event.defaultPrevented).toBe(true);
		expect(layout.current.activeView).toMatchObject({ type: 'docs', itemId: target });

		// Feed the real value the click produced to the same consumer MainPanel
		// uses, rather than seeding an unrelated deep-link fixture.
		const initialItemId = layout.current.activeView!.itemId;
		cleanup();
		const card = render(DocsCard, { initialItemId });
		await waitFor(() => expect(card.container.querySelector('.docs-content h1')?.textContent)
			.toBe(targetDoc.content.match(/^# (.+)$/m)![1]));
		expect(layout.current.activeView?.itemId).toBeNull();
	});

	it('opens a /docs/<category>/<slug> link inside the Docs panel instead of leaving the app', async () => {
		const { layout } = await import('$lib/stores/layout.svelte');
		const open = vi.spyOn(layout, 'openFeaturePage').mockImplementation(() => {});
		const { container } = render(DocsContent, { content: 'See [the guide](/docs/features/x).' });
		const link = container.querySelector('a') as HTMLAnchorElement;
		expect(link.getAttribute('href')).toBe('/docs/features/x');
		const event = new MouseEvent('click', { bubbles: true, cancelable: true });
		link.dispatchEvent(event);
		expect(event.defaultPrevented).toBe(true);
		expect(open).toHaveBeenCalledExactlyOnceWith('docs', 'features/x');
		open.mockRestore();
	});

	it('maps a bare /docs/<category> link to that category index', async () => {
		const { layout } = await import('$lib/stores/layout.svelte');
		const open = vi.spyOn(layout, 'openFeaturePage').mockImplementation(() => {});
		const { container } = render(DocsContent, { content: '[Connectors](/docs/connectors#setup)' });
		const event = new MouseEvent('click', { bubbles: true, cancelable: true });
		container.querySelector('a')!.dispatchEvent(event);
		expect(event.defaultPrevented).toBe(true);
		expect(open).toHaveBeenCalledExactlyOnceWith('docs', 'connectors/_index');
		open.mockRestore();
	});

	it('leaves external links alone', async () => {
		const { layout } = await import('$lib/stores/layout.svelte');
		const open = vi.spyOn(layout, 'openFeaturePage').mockImplementation(() => {});
		const { container } = render(DocsContent, { content: '[Site](https://example.com/docs/features/x)' });
		let preventedByDocs: boolean | null = null;
		// Record what the docs panel did, then stop jsdom from attempting the navigation.
		const spy = (e: Event) => { preventedByDocs = e.defaultPrevented; e.preventDefault(); };
		document.addEventListener('click', spy);
		container.querySelector('a')!.dispatchEvent(new MouseEvent('click', { bubbles: true, cancelable: true }));
		document.removeEventListener('click', spy);
		expect(preventedByDocs).toBe(false);
		expect(open).not.toHaveBeenCalled();
		open.mockRestore();
	});
});

describe('DocsContent anchor links', () => {
	// jsdom has no CSS.escape (every supported browser does).
	const g = globalThis as { CSS?: { escape?: (s: string) => string } };
	const hadCss = g.CSS;
	beforeEach(() => {
		if (!g.CSS?.escape) g.CSS = { ...(g.CSS ?? {}), escape: (s: string) => s.replace(/[^\w-]/g, (c) => `\\${c}`) };
	});
	afterEach(() => { g.CSS = hadCss; });

	function captureScroll() {
		const scroll = vi.fn();
		const original = Element.prototype.scrollIntoView;
		Element.prototype.scrollIntoView = function (this: Element, ...args: unknown[]) { scroll(this.id, ...args); };
		return { scroll, restore: () => { Element.prototype.scrollIntoView = original; } };
	}

	it('scrolls to an in-page #anchor heading without opening another page', async () => {
		const { layout } = await import('$lib/stores/layout.svelte');
		const open = vi.spyOn(layout, 'openFeaturePage').mockImplementation(() => {});
		const { scroll, restore } = captureScroll();
		try {
			const { container } = render(DocsContent, { content: '[Jump](#setup-steps)\n\n## Setup steps\n\nBody.' });
			expect(container.querySelector('h2')!.id).toBe('setup-steps');
			const event = new MouseEvent('click', { bubbles: true, cancelable: true });
			container.querySelector('a')!.dispatchEvent(event);
			expect(event.defaultPrevented).toBe(true);
			expect(scroll).toHaveBeenCalledExactlyOnceWith('setup-steps', { behavior: 'smooth', block: 'start' });
			expect(open).not.toHaveBeenCalled();

			// An anchor with no matching heading, or with selector metacharacters,
			// is a quiet no-op: it neither throws nor leaves the panel.
			scroll.mockClear();
			const { container: other } = render(DocsContent, { content: '[Odd](#a.b[c]) [Missing](#nope)' });
			for (const a of other.querySelectorAll('a')) {
				const e = new MouseEvent('click', { bubbles: true, cancelable: true });
				a.dispatchEvent(e);
				expect(e.defaultPrevented).toBe(true);
			}
			expect(scroll).not.toHaveBeenCalled();
		} finally {
			restore();
			open.mockRestore();
		}
	});

	it('opens a cross-page /docs link and scrolls to its anchor only on the next frame', async () => {
		const { layout } = await import('$lib/stores/layout.svelte');
		const open = vi.spyOn(layout, 'openFeaturePage').mockImplementation(() => {});
		const frames: FrameRequestCallback[] = [];
		const raf = vi.spyOn(window, 'requestAnimationFrame').mockImplementation((cb) => { frames.push(cb); return frames.length; });
		const { scroll, restore } = captureScroll();
		try {
			const { container } = render(DocsContent, { content: '[Hooks](/docs/features/hooks#exit-codes)\n\n## Exit codes' });
			frames.length = 0; // drop the heading-extraction frame from mount
			container.querySelector('a')!.dispatchEvent(new MouseEvent('click', { bubbles: true, cancelable: true }));
			expect(open).toHaveBeenCalledExactlyOnceWith('docs', 'features/hooks');
			// The target page renders after the panel switches, so the scroll waits a frame.
			await vi.waitFor(() => expect(frames).toHaveLength(1));
			expect(scroll).not.toHaveBeenCalled();
			frames[0](0);
			expect(scroll).toHaveBeenCalledExactlyOnceWith('exit-codes', { behavior: 'smooth', block: 'start' });
		} finally {
			restore();
			raf.mockRestore();
			open.mockRestore();
		}
	});
});
