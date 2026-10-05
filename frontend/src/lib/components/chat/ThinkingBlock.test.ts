import { render, fireEvent } from '@testing-library/svelte';
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
