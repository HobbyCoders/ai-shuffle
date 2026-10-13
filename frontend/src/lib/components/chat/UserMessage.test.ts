import { afterEach, describe, expect, it } from 'vitest';
import { cleanup, fireEvent, render, screen } from '@testing-library/svelte';
import UserMessage from './UserMessage.svelte';

afterEach(cleanup);

describe('UserMessage disclosure', () => {
	it('does not add disclosure chrome to a short prompt', () => {
		render(UserMessage, { props: { content: 'Keep this short prompt visible.' } });
		expect(screen.queryByRole('button', { name: /full user message/i })).toBeNull();
	});

	it('expands and collapses a genuinely long prompt accessibly', async () => {
		const content = 'x'.repeat(620);
		const { container } = render(UserMessage, { props: { content } });
		const expand = screen.getByRole('button', { name: 'Expand full user message' });
		expect(expand.getAttribute('aria-expanded')).toBe('false');
		expect(container.querySelector('.bubble-text')?.classList.contains('is-collapsed')).toBe(true);

		await fireEvent.click(expand);
		const collapse = screen.getByRole('button', { name: 'Collapse user message' });
		expect(collapse.getAttribute('aria-expanded')).toBe('true');
		expect(container.querySelector('.bubble-text')?.classList.contains('is-collapsed')).toBe(false);
	});
});

describe('UserMessage steer marker', () => {
	it('marks a mid-turn message on its time line only', () => {
		const timestamp = '2026-10-10T20:19:00.000Z';
		const { container } = render(UserMessage, { props: { content: 'Prod only', timestamp, midTurn: true } });
		expect(container.querySelector('.user-time')?.textContent).toMatch(/ · mid-turn$/);
		cleanup();
		const plain = render(UserMessage, { props: { content: 'Prod only', timestamp } });
		expect(plain.container.querySelector('.user-time')?.textContent).not.toContain('mid-turn');
	});
});
