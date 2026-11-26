/**
 * Settings > Memory > Proposed memories against a mocked API: the row's
 * count, each proposal's text, kind, quote, chats and expiry, Accept saving a
 * memory (and refreshing the list), Dismiss, and a proposal another worker
 * already decided.
 */
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/svelte';

const http = vi.hoisted(() => ({ get: vi.fn(), put: vi.fn(), post: vi.fn(), delete: vi.fn() }));
vi.mock('$lib/api/client', async (importOriginal) => ({
	...(await importOriginal<typeof import('$lib/api/client')>()),
	api: http
}));

import { settingsNav } from '$lib/settings/navigation.svelte';
import MemoryCollection from './MemoryCollection.svelte';
import { MemoryModel, releaseMemoryModel } from './memory-model.svelte';
import { expiryText, inboxSummary, sessionText, type Proposal } from './proposal-model.svelte';

const NOW = Date.now() / 1000;
const proposal = (over: Partial<Proposal> = {}): Proposal => ({
	id: 'p1',
	text: 'The project deploys with make release.',
	kind: 'project',
	kind_label: 'Project knowledge',
	quote: 'use make release',
	when: 'When deploying',
	scope: 'project:site',
	scope_label: 'Website',
	sessions: [
		{ id: 'ses_1', title: 'Fix the deploy' },
		{ id: 'ses_2', title: null }
	],
	session_count: 2,
	created_at: NOW - 86_400,
	expires_at: NOW + 13 * 86_400 - 60,
	...over
});

let inbox: Proposal[] = [];
const memories = { items: [], limits: { text_chars: 2000, when_chars: 140 } };

async function show(item?: string) {
	settingsNav.reset();
	settingsNav.selectCategory('memory');
	if (item) settingsNav.go({ category: 'memory', item });
	render(MemoryCollection);
	await waitFor(() => expect(http.get).toHaveBeenCalledWith('/memories/proposals'));
}

const row = () => document.querySelector('[data-settings-item="proposals"]');

beforeEach(() => {
	vi.clearAllMocks();
	releaseMemoryModel(new MemoryModel());
	inbox = [proposal(), proposal({ id: 'p2', text: 'The owner works from a home server.', kind: 'owner_fact', kind_label: 'Owner fact', quote: null, scope: 'global', scope_label: 'All projects', sessions: [{ id: 'ses_3', title: 'Backups' }], session_count: 1 })];
	http.get.mockImplementation(async (path: string) =>
		path === '/memories/proposals' ? { items: inbox, ttl_days: 14, mode: 'reflection' } : path === '/memories' ? memories : {}
	);
});
afterEach(() => cleanup());

it('formats the row, chat count and expiry', () => {
	expect(inboxSummary(0)).toBe('None waiting');
	expect(inboxSummary(3)).toBe('3 waiting for you');
	expect(sessionText({ session_count: 1 })).toBe('From 1 chat');
	expect(expiryText(NOW + 86_400 * 3 - 10, NOW * 1000)).toBe('Expires in 3 days');
	expect(expiryText(NOW - 5, NOW * 1000)).toBe('Expires today');
	expect(expiryText(null)).toBe('');
});

it('shows how many proposals wait on the Memory list without opening them', async () => {
	await show();
	await waitFor(() => expect(row()?.textContent).toContain('2 waiting for you'));
	expect(row()?.textContent).toContain('Proposed memories');
	expect(screen.queryByText('The project deploys with make release.')).toBeNull();
});

it('lists each proposal with its kind, quote, chats and expiry', async () => {
	await show('proposals');
	const items = await screen.findAllByTestId('memory-proposal');
	expect(items).toHaveLength(2);
	const first = items[0].textContent ?? '';
	expect(first).toContain('The project deploys with make release.');
	expect(first).toContain('“use make release”');
	expect(first).toContain('Project knowledge · Website · From 2 chats · Expires in 13 days');
	expect(first).toContain('Fix the deploy');
	expect(first).toContain('A chat on another worker or since deleted');
	expect(items[1].textContent).not.toContain('“');
	expect(screen.getByText(/shared with all your workers/i)).toBeTruthy();
});

it('accepts a proposal into a saved memory and refreshes the memory list', async () => {
	http.post.mockResolvedValue({ id: 'p1', status: 'accepted', memory: { id: 'm9' } });
	await show('proposals');
	await screen.findAllByTestId('memory-proposal');
	const memoryLoads = http.get.mock.calls.filter(([p]) => p === '/memories').length;
	await fireEvent.click(screen.getByRole('button', { name: 'Accept: The project deploys with make release.' }));
	expect(http.post).toHaveBeenCalledWith('/memories/proposals/p1/accept');
	await waitFor(() => expect(screen.getAllByTestId('memory-proposal')).toHaveLength(1));
	expect(screen.getByRole('status').textContent).toContain('Saved as a memory');
	await waitFor(() => expect(http.get.mock.calls.filter(([p]) => p === '/memories').length).toBe(memoryLoads + 1));
});

it('dismisses a proposal without saving anything', async () => {
	http.post.mockResolvedValue({ id: 'p2', status: 'dismissed', memory: null });
	await show('proposals');
	await screen.findAllByTestId('memory-proposal');
	const memoryLoads = http.get.mock.calls.filter(([p]) => p === '/memories').length;
	await fireEvent.click(screen.getByRole('button', { name: 'Dismiss: The owner works from a home server.' }));
	expect(http.post).toHaveBeenCalledWith('/memories/proposals/p2/dismiss');
	await waitFor(() => expect(screen.getAllByTestId('memory-proposal')).toHaveLength(1));
	expect(http.get.mock.calls.filter(([p]) => p === '/memories').length).toBe(memoryLoads);
});

it('drops a proposal another worker already decided and says so', async () => {
	http.post.mockRejectedValue(Object.assign(new Error('gone'), { status: 404, detail: 'This proposal was already decided or has expired.' }));
	await show('proposals');
	await screen.findAllByTestId('memory-proposal');
	await fireEvent.click(screen.getByRole('button', { name: 'Accept: The project deploys with make release.' }));
	await waitFor(() => expect(screen.getByRole('alert').textContent).toContain('already decided'));
	expect(screen.getAllByTestId('memory-proposal')).toHaveLength(1);
});

it('says when nothing waits', async () => {
	inbox = [];
	await show('proposals');
	await waitFor(() => expect(screen.getByText(/Nothing waiting/)).toBeTruthy());
	expect(row()?.textContent).toContain('None waiting');
});
