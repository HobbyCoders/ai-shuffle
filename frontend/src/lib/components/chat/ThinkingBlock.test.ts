import { render, fireEvent, waitFor } from '@testing-library/svelte';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { IMAGE_RETRY_DELAYS_MS } from '$lib/utils/media';
import ThinkingBlock from './ThinkingBlock.svelte';

describe('ThinkingBlock collapse', () => {
	it('clicking the header toggles the content', async () => {
		const { container } = render(ThinkingBlock, {
			props: { content: 'Some long thinking text that should be collapsible.' }
		});

		const header = container.querySelector<HTMLButtonElement>('.thinking-header');
		expect(header).toBeTruthy();

		const contentBefore = container.querySelector('.thinking-content');
		expect(contentBefore).toBeNull(); // collapsed by default

		await fireEvent.click(header!);

		const expanded = container.querySelector('.thinking-content');
		expect(expanded).toBeTruthy();
		expect(header!.getAttribute('aria-expanded')).toBe('true');

		await fireEvent.click(header!);
		expect(container.querySelector('.thinking-content')).toBeNull();
	});
});

describe('ThinkingBlock images (#1850)', () => {
	afterEach(() => vi.useRealTimers());

	it('reloads an image in the thinking text after a first 404', async () => {
		const source = '/api/generated-images/plan.png';
		const { container } = render(ThinkingBlock, { props: { content: `Sketch: ![plan](${source})` } });
		await fireEvent.click(container.querySelector<HTMLButtonElement>('.thinking-header')!);
		const img = container.querySelector('.thinking-content img')!;
		expect(img.getAttribute('src')).toBe(source);
		vi.useFakeTimers();
		img.dispatchEvent(new Event('error'));
		vi.advanceTimersByTime(IMAGE_RETRY_DELAYS_MS[0]);
		expect(img.getAttribute('src')).toBe(`${source}?retry=1`);
	});
});

describe('ThinkingBlock while it streams (#1627)', () => {
	// Reduced motion hands each delta through whole, so checks see the full text.
	afterEach(() => vi.unstubAllGlobals());

	async function stream(deltas: string[]) {
		vi.stubGlobal('matchMedia', (query: string) => ({ matches: query.includes('reduce'), addEventListener() {}, removeEventListener() {} }));
		let text = deltas[0];
		const view = render(ThinkingBlock, { props: { content: text, streaming: true, initialExpanded: true } });
		for (const delta of deltas.slice(1)) {
			text += delta;
			await view.rerender({ content: text, streaming: true, initialExpanded: true });
		}
		await waitFor(() => expect(view.container.querySelector('.thinking-content')?.textContent).toContain(deltas.at(-1)!.trim().slice(-6)));
		return { view, text };
	}

	it('keeps an image in a fence that is still open as code', async () => {
		const { view } = await stream(['Plan.\n\n```md\n', 'x\n', '\n', '![plan](/api/generated-images/plan.png) end']);
		expect(view.container.querySelector('.thinking-content img')).toBeNull();
		expect(view.container.querySelector('.thinking-content pre')?.textContent).toContain('![plan]');
	});

	it('streams what the full render shows after an unclosed raw <a>', async () => {
		const { view, text } = await stream(['Intro <a href="/docs">label', '\n\nhttps://example.com/page']);
		const streamed = view.container.querySelector('.thinking-content')!.innerHTML;
		await view.rerender({ content: text, streaming: false, initialExpanded: true });
		await waitFor(() => expect(view.container.querySelector('.thinking-content')!.innerHTML).toBe(streamed));
		expect(view.container.querySelector('.thinking-content a[href^="https://example.com"]')).toBeNull();
	});
});
