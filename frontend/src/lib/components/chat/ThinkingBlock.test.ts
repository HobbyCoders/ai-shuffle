import { render, fireEvent } from '@testing-library/svelte';
import { describe, expect, it } from 'vitest';
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
