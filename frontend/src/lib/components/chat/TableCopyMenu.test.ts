import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/svelte';
import { tick } from 'svelte';

const clip = vi.hoisted(() => ({ copyText: vi.fn() }));
vi.mock('$lib/utils/clipboard', () => ({ copyText: clip.copyText, COPY_ERROR_MESSAGE: 'Could not copy.' }));

import TableCopyMenu from './TableCopyMenu.svelte';
import { renderChatMarkdown } from '$lib/utils/chat-markdown';
import { clearTables } from '$lib/utils/table-export';

let wrapper: HTMLElement;

beforeEach(() => {
	clip.copyText.mockReset().mockResolvedValue(undefined);
	vi.stubGlobal('ResizeObserver', class { observe() {} unobserve() {} disconnect() {} });
	const host = document.body.appendChild(document.createElement('div'));
	host.innerHTML = renderChatMarkdown('| Item | Cost |\n| --- | ---: |\n| **Tea** | =2+2 |', true);
	wrapper = host.querySelector<HTMLElement>('.md-table')!;
});
afterEach(() => {
	cleanup();
	clearTables();
	document.body.innerHTML = '';
	vi.unstubAllGlobals();
});

describe('TableCopyMenu', () => {
	it('copies Markdown from the source the agent wrote', async () => {
		render(TableCopyMenu, { props: { wrapper } });
		await fireEvent.click(screen.getByRole('button', { name: 'Copy table' }));
		await fireEvent.click(screen.getByRole('menuitem', { name: /Copy as Markdown/ }));
		expect(clip.copyText).toHaveBeenCalledWith('| Item | Cost |\n| --- | ---: |\n| **Tea** | =2+2 |');
		await waitFor(() => expect(screen.getByText('Copied')).toBeTruthy());
		expect(screen.queryByRole('menu')).toBeNull();
	});

	it('copies formula-guarded CSV of the rendered text', async () => {
		render(TableCopyMenu, { props: { wrapper } });
		await fireEvent.click(screen.getByRole('button', { name: 'Copy table' }));
		await fireEvent.click(screen.getByRole('menuitem', { name: /Copy as CSV/ }));
		expect(clip.copyText).toHaveBeenCalledWith("Item,Cost\r\nTea,'=2+2");
	});

	it('works from the keyboard and returns focus on Escape', async () => {
		render(TableCopyMenu, { props: { wrapper } });
		const trigger = screen.getByRole('button', { name: 'Copy table' });
		trigger.focus();
		await fireEvent.keyDown(trigger, { key: 'ArrowDown' });
		await tick();
		const items = screen.getAllByRole('menuitem');
		expect(trigger.getAttribute('aria-expanded')).toBe('true');
		expect(document.activeElement).toBe(items[0]);
		await fireEvent.keyDown(screen.getByRole('menu'), { key: 'ArrowDown' });
		expect(document.activeElement).toBe(items[1]);
		await fireEvent.keyDown(screen.getByRole('menu'), { key: 'ArrowDown' });
		expect(document.activeElement).toBe(items[0]);
		await fireEvent.keyDown(screen.getByRole('menu'), { key: 'Escape' });
		expect(screen.queryByRole('menu')).toBeNull();
		expect(document.activeElement).toBe(trigger);
	});

	it('says so when the copy fails', async () => {
		clip.copyText.mockRejectedValue(new Error('denied'));
		render(TableCopyMenu, { props: { wrapper } });
		await fireEvent.click(screen.getByRole('button', { name: 'Copy table' }));
		await fireEvent.click(screen.getByRole('menuitem', { name: /Copy as CSV/ }));
		await waitFor(() => expect(screen.getByText('Copy failed')).toBeTruthy());
		expect(screen.getByRole('status').textContent).toBe('Could not copy.');
	});

	it('closes on a click outside', async () => {
		render(TableCopyMenu, { props: { wrapper } });
		await fireEvent.click(screen.getByRole('button', { name: 'Copy table' }));
		expect(screen.getByRole('menu')).toBeTruthy();
		await fireEvent.pointerDown(document.body);
		expect(screen.queryByRole('menu')).toBeNull();
	});
});
