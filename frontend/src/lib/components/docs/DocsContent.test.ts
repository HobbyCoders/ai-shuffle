import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, waitFor } from '@testing-library/svelte';
import { getDocByFullSlug, preloadAllDocs } from '$lib/docs/index';
import DocsCard from './DocsCard.svelte';
import DocsContent from './DocsContent.svelte';

afterEach(() => { cleanup(); vi.useRealTimers(); });

describe('DocsContent headings', () => {
	it('renders inline code in a heading and keeps its anchor (#1220)', () => {
		const { container } = render(DocsContent, { content: '## Scripts and `curl`' });
		const heading = container.querySelector('h2')!;
		expect(heading.textContent).toBe('Scripts and curl');
		expect(heading.querySelector('code')?.textContent).toBe('curl');
		expect(heading.id).toBe('scripts-and-curl');
	});

	it('gives repeated headings unique anchors that match the contents list (#1475)', async () => {
		const { extractHeadings } = await import('$lib/docs/markdown');
		const md = '## 1.1\n\n### Removed\n\n## 1.0\n\n### Removed';
		const { container } = render(DocsContent, { content: md });
		const ids = [...container.querySelectorAll('h2, h3')].map((h) => h.id);
		expect(ids).toEqual(['11', 'removed', '10', 'removed-1']);
		expect(extractHeadings(md).map((h) => h.id)).toEqual(ids);
	});
});

describe('DocsContent find (#1478)', () => {
	it('marks and scrolls to the first readable match, once per search', async () => {
		const scrolled: string[] = [];
		const original = Element.prototype.scrollIntoView;
		Element.prototype.scrollIntoView = function (this: Element) { scrolled.push(this.textContent ?? ''); };
		try {
			const content = '# Away for all chats\n\n```sh\naway\n```\n\n| Mode | Note |\n| --- | --- |\n| Quiet | Away for all chats pauses replies |';
			const view = render(DocsContent, { content, framed: true, find: { query: 'away for all', key: 1 } });
			await waitFor(() => expect(view.container.querySelectorAll('mark.doc-find-hit')).toHaveLength(1));
			const mark = view.container.querySelector('mark.doc-find-hit')!;
			// Not the hidden duplicate title, and the whole phrase before a lone word in code.
			expect(mark.closest('td')).not.toBeNull();
			expect(mark.textContent).toBe('Away for all');
			expect(scrolled).toEqual(['Away for all']);

			await view.rerender({ content, framed: true, find: { query: 'away for all', key: 1 } });
			expect(scrolled).toHaveLength(1);
			await view.rerender({ content, framed: true, find: { query: 'pauses', key: 2 } });
			await waitFor(() => expect(scrolled).toEqual(['Away for all', 'pauses']));
			expect([...view.container.querySelectorAll('mark.doc-find-hit')].map((m) => m.textContent)).toEqual(['pauses']);
			expect(view.container.querySelector('td:last-child')!.textContent).toBe('Away for all chats pauses replies');
		} finally {
			Element.prototype.scrollIntoView = original;
		}
	});

	it('finds the whole phrase across inline markup, as the search snippet does', async () => {
		await preloadAllDocs();
		const scrolled: Element[] = [];
		const original = Element.prototype.scrollIntoView;
		Element.prototype.scrollIntoView = function (this: Element) { scrolled.push(this); };
		try {
			const query = 'Away for all chats (the moon';
			const { content } = getDocByFullSlug('getting-started/changelog')!;
			const view = render(DocsContent, { content, framed: true, find: { query, key: 1 } });
			await waitFor(() => expect(view.container.querySelectorAll('mark.doc-find-hit').length).toBeGreaterThan(1));
			const marks = [...view.container.querySelectorAll('mark.doc-find-hit')];
			expect(marks.map((m) => m.textContent).join('')).toBe(query);
			expect(marks[0].closest('strong')?.textContent).toBe('Away for all chats');
			expect(new Set(marks.map((m) => m.closest('li')))).toHaveLength(1);
			expect(scrolled).toEqual([marks[0]]);

			// Clearing the marks restores the item's text exactly.
			const item = marks[0].closest('li')!;
			const text = item.textContent;
			await view.rerender({ content, framed: true, find: { query: 'qzxqzx', key: 2 } });
			expect(item.querySelector('mark')).toBeNull();
			expect(item.textContent).toBe(text);
			expect(item.querySelector('strong')?.textContent).toBe('Away for all chats');
		} finally {
			Element.prototype.scrollIntoView = original;
		}
	});

	it('finds the whole phrase across a wrapped source line, as the search snippet does', async () => {
		await preloadAllDocs();
		const { DocsSearchEngine } = await import('$lib/docs/search');
		const scrolled: Element[] = [];
		const original = Element.prototype.scrollIntoView;
		Element.prototype.scrollIntoView = function (this: Element) { scrolled.push(this); };
		try {
			const query = 'balance access separately';
			const doc = getDocByFullSlug('api-reference/model-providers')!;
			// The source wraps between "access" and "separately"; `breaks: true` renders a <br> there.
			expect(doc.content).toContain('balance access\nseparately');
			const [hit] = new DocsSearchEngine([doc]).search(query);
			expect(hit.snippets[0].toLowerCase()).toContain(query);

			const view = render(DocsContent, { content: doc.content, framed: true, find: { query, key: 1 } });
			await waitFor(() => expect(view.container.querySelectorAll('mark.doc-find-hit').length).toBeGreaterThan(0));
			const marks = [...view.container.querySelectorAll('mark.doc-find-hit')];
			expect(marks.map((m) => m.textContent).join(' ')).toBe(query);
			const paragraph = marks[0].closest('p')!;
			expect(marks.every((m) => m.closest('p') === paragraph)).toBe(true);
			expect(paragraph.innerHTML).toContain('<mark class="doc-find-hit">balance access</mark><br><mark class="doc-find-hit">separately</mark>');
			expect(paragraph.previousElementSibling?.textContent).toBe('Provider-reported API balances');
			expect(scrolled).toEqual([marks[0]]);
		} finally {
			Element.prototype.scrollIntoView = original;
		}
	});

	it('marks a whole word, not the same letters inside another word', async () => {
		const scrolled: string[] = [];
		const original = Element.prototype.scrollIntoView;
		Element.prototype.scrollIntoView = function (this: Element) { scrolled.push(this.textContent ?? ''); };
		try {
			const content = 'Pay for a subscription.\n\nRun your own script before a turn.';
			const view = render(DocsContent, { content, find: { query: '<script>', key: 1 } });
			await waitFor(() => expect(view.container.querySelectorAll('mark.doc-find-hit')).toHaveLength(1));
			const mark = view.container.querySelector('mark.doc-find-hit')!;
			expect(mark.textContent).toBe('script');
			expect(mark.closest('p')?.textContent).toBe('Run your own script before a turn.');
			expect(scrolled).toEqual(['script']);
		} finally {
			Element.prototype.scrollIntoView = original;
		}
	});

	it('reads a line break as a space next to inline code, links and a raw <br>', async () => {
		const { markdownToPlainText } = await import('$lib/docs/markdown');
		const content = '- run `git`\n  [pull](/docs/x) now\n\n| A | B |\n| --- | --- |\n| one<br>two | `x`<br/>`y` |';
		expect(markdownToPlainText(content)).toBe('run git pull now A B one two x y');
		for (const [query, expected] of [['git pull now', ['git', 'pull', 'now']], ['one two', ['one', 'two']], ['x y', ['x', 'y']]] as const) {
			const view = render(DocsContent, { content, find: { query, key: 1 } });
			await waitFor(() => expect(view.container.querySelectorAll('mark.doc-find-hit').length).toBeGreaterThan(0));
			const marks = [...view.container.querySelectorAll('mark.doc-find-hit')];
			expect(marks.map((m) => m.textContent?.trim())).toEqual(expected);
			view.unmount();
		}
	});
});

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
			frames.length = 0; // only the click's frame counts
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
