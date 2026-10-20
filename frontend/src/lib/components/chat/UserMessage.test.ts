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

it('renders the unsent label separately without changing the copyable content', () => {
	const { container } = render(UserMessage, { props: { content: 'My original words', notSent: true } });
	expect(screen.getByText('Not sent (stopped)')).toBeTruthy();
	expect(container.querySelector('.bubble-text')?.textContent).toBe('My original words');
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

describe('UserMessage time line', () => {
	it('carries the date: "Today" for today, a real date for an older message', () => {
		const today = new Date();
		const { container } = render(UserMessage, { props: { content: 'Now', timestamp: today } });
		expect(container.querySelector('.user-time time')?.textContent).toMatch(/^Today /);
		cleanup();
		const old = '2024-02-03T12:00:00.000Z';
		const older = render(UserMessage, { props: { content: 'Then', timestamp: old } });
		const stamp = older.container.querySelector('.user-time time');
		expect(stamp?.textContent).toContain('2024');
		expect(stamp?.getAttribute('datetime')).toBe(old);
		expect(older.container.querySelector('.bubble')?.getAttribute('title')).toContain('2024');
	});
});
