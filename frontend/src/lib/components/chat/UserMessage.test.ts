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

describe('UserMessage attachments', () => {
	const path = '/workspace/project/.uploads/note.txt';

	it('shows a recorded upload as a chip where it was mentioned, never its path', () => {
		const { container } = render(UserMessage, { props: { content: `Read @${path} please`, uploadPaths: [path] } });
		const bubble = container.querySelector('.bubble-text');
		expect(bubble?.textContent).toBe('Read note.txt please');
		expect(container.querySelector('.bubble')?.innerHTML).not.toContain('.uploads');
		expect(container.querySelector('.attachment-chip-name')?.textContent).toBe('note.txt');
	});

	it('adds a chip for a recorded upload the words never mention', () => {
		const { container } = render(UserMessage, { props: { content: 'Read this', uploadPaths: [path] } });
		expect(container.querySelector('.bubble-text')?.textContent).toBe('Read thisnote.txt');
		expect(container.querySelectorAll('.attachment-chip')).toHaveLength(1);
	});

	it('leaves an older message with no recorded uploads exactly as it was', () => {
		const content = `Read @${path} please`;
		const { container } = render(UserMessage, { props: { content } });
		expect(container.querySelector('.bubble-text')?.textContent).toBe(content);
		expect(container.querySelector('.attachment-chip')).toBeNull();
	});

	it('does not chip an @mention the send did not record', () => {
		const { container } = render(UserMessage, { props: { content: `Ask @/other/file.txt and @${path}`, uploadPaths: [path] } });
		expect(container.querySelector('.bubble-text')?.textContent).toBe('Ask @/other/file.txt and note.txt');
	});
});
