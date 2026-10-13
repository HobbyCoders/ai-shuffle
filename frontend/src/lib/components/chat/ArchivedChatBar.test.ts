/**
 * An archived chat is read-only until restored (#1822): ChatView swaps the
 * composer for this bar, whose only actions are export and restore.
 */
import { cleanup, fireEvent, render, screen } from '@testing-library/svelte';
import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import { get } from 'svelte/store';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

const mocks = vi.hoisted(() => ({ restore: vi.fn(), exportSession: vi.fn() }));
vi.mock('$lib/stores/app-data.svelte', () => ({ appData: { setSessionArchived: mocks.restore } }));
vi.mock('$lib/api/sessions', () => ({ exportSession: mocks.exportSession }));
vi.mock('$lib/stores/notifications', () => ({ notify: vi.fn() }));
import ArchivedChatBar from './ArchivedChatBar.svelte';
import { archivedChats } from '$lib/stores/archived-chats';

beforeEach(() => {
	vi.clearAllMocks();
	mocks.restore.mockResolvedValue(undefined);
});
afterEach(cleanup);

describe('ArchivedChatBar', () => {
	it('offers export and restore, and no message box', () => {
		render(ArchivedChatBar, { sessionId: 'old' });
		expect(screen.getByText(/archived\. It is read-only until you restore it/)).toBeTruthy();
		expect(screen.getByRole('button', { name: 'Restore' })).toBeTruthy();
		expect(screen.getByRole('button', { name: 'Download private backup (.jsonl)' })).toBeTruthy();
		expect(screen.getByRole('button', { name: 'Export for sharing (.md)' })).toBeTruthy();
		expect(screen.queryByRole('textbox')).toBeNull();
	});

	it('restores the chat', async () => {
		render(ArchivedChatBar, { sessionId: 'old' });
		await fireEvent.click(screen.getByRole('button', { name: 'Restore' }));
		expect(mocks.restore).toHaveBeenCalledWith('old', false);
	});

	it('shows the server reason when restore fails', async () => {
		mocks.restore.mockRejectedValueOnce({ detail: 'Owning worker is offline.' });
		render(ArchivedChatBar, { sessionId: 'old' });
		await fireEvent.click(screen.getByRole('button', { name: 'Restore' }));
		expect(await screen.findByRole('alert')).toHaveProperty('textContent', 'Owning worker is offline.');
	});
});

describe('archivedChats store', () => {
	it('tracks archive and restore per chat', () => {
		archivedChats.mark('a', true);
		archivedChats.mark('b', true);
		archivedChats.mark('a', false);
		expect([...get(archivedChats)]).toEqual(['b']);
		const before = get(archivedChats);
		archivedChats.mark('b', true);
		expect(get(archivedChats)).toBe(before);
		archivedChats.mark('b', false);
	});
});

describe('ChatView composer slot', () => {
	const chatView = readFileSync(resolve(process.cwd(), 'src/lib/components/layout/ChatView.svelte'), 'utf8')
		.replace(/<!--[\s\S]*?-->/g, '');

	it('shows the archived bar instead of the composer for an archived chat', () => {
		expect(chatView).toContain('$archivedChats.has($activeSession.sessionId)');
		const bar = chatView.indexOf('{#if archivedSessionId}');
		const input = chatView.indexOf('<ChatInput ');
		expect(bar).toBeGreaterThan(-1);
		expect(chatView.indexOf('<ArchivedChatBar sessionId={archivedSessionId} />')).toBeGreaterThan(bar);
		expect(chatView.indexOf('{:else}', bar)).toBeLessThan(input);
	});
});
