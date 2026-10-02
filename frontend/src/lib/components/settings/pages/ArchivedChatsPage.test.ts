import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/svelte';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import type { Session } from '$lib/api/sessions';

const mocks = vi.hoisted(() => ({ get: vi.fn(), restore: vi.fn(), openSession: vi.fn() }));
vi.mock('$lib/api/client', () => ({ api: { get: mocks.get } }));
vi.mock('$lib/stores/app-data.svelte', () => ({ appData: { setSessionArchived: mocks.restore } }));
vi.mock('$lib/services/session-navigation', () => ({ openSession: mocks.openSession }));
vi.mock('$app/navigation', () => ({ pushState: vi.fn() }));
import ArchivedChatsPage from './ArchivedChatsPage.svelte';
import { SETTINGS_ACCESS_CONTEXT } from '$lib/settings/access.svelte';

const chat = { id: 'archived-a', title: 'Archived work', status: 'archived', is_active: false, project_id: 'project', project_v2_id: 'v2-project', branch: 'work/archive' } as Session;
const page = (items: Session[], more = false) => ({ items, has_more: more, next_cursor: more ? '2026-10-01T12:00:00+00:00' : null, next_cursor_id: more ? 'archived-a' : null });
beforeEach(() => {
	vi.clearAllMocks();
	mocks.get.mockResolvedValue(page([chat]));
	mocks.restore.mockResolvedValue(undefined);
});
afterEach(cleanup);

describe('Archived chats settings page', () => {
	it('queries archived history directly instead of the loaded sidebar page', async () => {
		render(ArchivedChatsPage);
		expect(await screen.findByRole('button', { name: 'Read Archived work' })).toBeTruthy();
		const url = new URL(mocks.get.mock.calls[0][0], 'https://example.test');
		expect(url.pathname).toBe('/sessions/cursor');
		expect(url.searchParams.get('status')).toBe('archived');
		expect(url.searchParams.get('admin_only')).toBe('true');
		expect(url.searchParams.has('project_id')).toBe(false);
	});
	it('opens a transcript with its project and branch without restoring it', async () => {
		render(ArchivedChatsPage);
		await fireEvent.click(await screen.findByRole('button', { name: 'Read Archived work' }));
		// The shared path sets the chat's mode and closes an overlaid sidebar.
		expect(mocks.openSession).toHaveBeenCalledWith(chat.id, expect.objectContaining({ id: chat.id, branch: 'work/archive' }));
		expect(mocks.restore).not.toHaveBeenCalled();
	});
	it('restores to Settled and removes the row after success', async () => {
		render(ArchivedChatsPage);
		await fireEvent.click(await screen.findByRole('button', { name: 'Restore Archived work' }));
		expect(mocks.restore).toHaveBeenCalledWith(chat.id, false);
		await waitFor(() => expect(screen.queryByRole('button', { name: 'Read Archived work' })).toBeNull());
		expect(screen.getByText('No archived chats.')).toBeTruthy();
	});
	it('keeps a failed restore available and shows the server reason', async () => {
		mocks.restore.mockRejectedValueOnce({ detail: 'Owning worker is offline.' });
		render(ArchivedChatsPage);
		await fireEvent.click(await screen.findByRole('button', { name: 'Restore Archived work' }));
		expect(await screen.findByRole('alert')).toHaveProperty('textContent', 'Owning worker is offline.');
		expect(screen.getByRole('button', { name: 'Read Archived work' })).toBeTruthy();
	});
	it('pages through archived history and deduplicates rows', async () => {
		mocks.get.mockResolvedValueOnce(page([chat], true)).mockResolvedValueOnce(page([chat, { ...chat, id: 'older', title: 'Older chat' }]));
		render(ArchivedChatsPage);
		await fireEvent.click(await screen.findByRole('button', { name: 'Load older archived chats' }));
		expect(await screen.findByRole('button', { name: 'Read Older chat' })).toBeTruthy();
		expect(screen.getAllByRole('button', { name: 'Read Archived work' })).toHaveLength(1);
		const url = new URL(mocks.get.mock.calls[1][0], 'https://example.test');
		expect(url.searchParams.get('cursor')).toBe('2026-10-01T12:00:00+00:00');
		expect(url.searchParams.get('cursor_id')).toBe(chat.id);
		expect(screen.queryByRole('button', { name: 'Load older archived chats' })).toBeNull();
	});
	it('retries a failed initial load using Refresh', async () => {
		mocks.get.mockRejectedValueOnce(new Error('offline')).mockResolvedValueOnce(page([]));
		render(ArchivedChatsPage);
		expect(await screen.findByRole('alert')).toBeTruthy();
		await fireEvent.click(screen.getByRole('button', { name: 'Refresh archived chats' }));
		expect(await screen.findByText('No archived chats.')).toBeTruthy();
	});
	it('disables restores for read-only settings viewers', async () => {
		render(ArchivedChatsPage, { context: new Map([[SETTINGS_ACCESS_CONTEXT, { readOnly: true }]]) });
		expect(await screen.findByRole('button', { name: 'Restore Archived work' })).toHaveProperty('disabled', true);
	});
	it('shows approval guidance for a forbidden load', async () => {
		mocks.get.mockRejectedValueOnce({ status: 403 });
		render(ArchivedChatsPage);
		expect(await screen.findByRole('alert')).toHaveProperty('textContent', 'Available after approval');
	});
});
