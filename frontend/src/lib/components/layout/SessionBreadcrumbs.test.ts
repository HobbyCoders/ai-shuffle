import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/svelte';
import { writable } from 'svelte/store';
import type { Session } from '$lib/api/sessions';

vi.mock('$lib/stores/app-data.svelte', () => ({ appData: {}, sessions: writable<Session[]>([]) }));
vi.mock('$lib/stores/active-session', () => ({ activeSession: writable({}) }));
vi.mock('$lib/api/client', () => ({ api: { get: vi.fn(), patch: vi.fn(), delete: vi.fn() }, forkSession: vi.fn() }));
import { sessions } from '$lib/stores/app-data.svelte';
import { api } from '$lib/api/client';
import SessionBreadcrumbs from './SessionBreadcrumbs.svelte';
import { openSessionLink } from '$lib/services/session-navigation';
vi.mock('$lib/services/session-navigation', () => ({ openSessionLink: vi.fn((event: MouseEvent) => event.preventDefault()) }));

const row = (id: string, fields: Partial<Session> = {}) => ({ id, title: id, ...fields } as Session);
beforeEach(() => { vi.resetAllMocks(); (sessions as unknown as ReturnType<typeof writable<Session[]>>).set([]); });
afterEach(cleanup);

it('shows project, parent and current transcript in order using persisted identity', async () => {
	vi.mocked(api.get).mockResolvedValueOnce(row('child', { hidden: true, parent_session_id: 'parent / chat' }))
		.mockResolvedValueOnce(row('parent / chat', { title: 'Plan the release' }));
	render(SessionBreadcrumbs, { projectLabel: 'AI Shuffle', title: 'Review Android', sessionId: 'child' });
	const link = await screen.findByRole('link', { name: 'Open parent chat: Plan the release' });
	expect(link.getAttribute('href')).toBe('/?session=parent%20%2F%20chat');
	await fireEvent.click(link);
	expect(openSessionLink).toHaveBeenCalledWith(expect.any(MouseEvent), 'parent / chat');
	expect(screen.getByRole('navigation').textContent).toMatch(/AI Shuffle.*Plan the release.*Review Android/s);
	expect(document.querySelector('[aria-current="page"]')?.textContent).toContain('Review Android');
	expect(api.get).toHaveBeenNthCalledWith(1, '/sessions/child?message_limit=1');
	expect(api.get).toHaveBeenNthCalledWith(2, '/sessions/parent%20%2F%20chat?message_limit=1');
});

it('uses sidebar metadata without fetching either session and follows renamed titles', async () => {
	const store = sessions as unknown as ReturnType<typeof writable<Session[]>>;
	store.set([row('child', { hidden: true, parent_session_id: 'parent' }), row('parent')]);
	render(SessionBreadcrumbs, { title: 'Worker', sessionId: 'child', mobile: true });
	expect(screen.getByRole('link', { name: 'Open parent chat: parent' })).toBeTruthy();
	store.set([row('child', { hidden: true, parent_session_id: 'parent' }), row('parent', { title: 'Renamed' })]);
	expect(await screen.findByRole('link', { name: 'Open parent chat: Renamed' })).toBeTruthy();
	expect(api.get).not.toHaveBeenCalled();
});

it('leaves ordinary forks and unbound chats without a subagent parent', async () => {
	const store = sessions as unknown as ReturnType<typeof writable<Session[]>>;
	store.set([row('fork', { hidden: false, parent_session_id: 'original' })]);
	const view = render(SessionBreadcrumbs, { title: 'Fork', sessionId: 'fork' });
	expect(screen.queryByRole('link')).toBeNull();
	await view.rerender({ sessionId: null, title: 'New Chat' });
	expect(screen.queryByRole('link')).toBeNull();
	expect(api.get).not.toHaveBeenCalled();
});

it('does not apply a child lookup after navigating away', async () => {
	let resolve!: (value: Session) => void;
	vi.mocked(api.get).mockReturnValueOnce(new Promise<Session>(r => { resolve = r; }));
	const view = render(SessionBreadcrumbs, { sessionId: 'old', title: 'Old worker' });
	await view.rerender({ sessionId: null, title: 'New Chat' });
	resolve(row('old', { hidden: true, parent_session_id: 'parent' }));
	await waitFor(() => expect(screen.getByText('New Chat')).toBeTruthy());
	expect(screen.queryByRole('link')).toBeNull();
	expect(api.get).toHaveBeenCalledTimes(1);
});

it('ignores a parent response after switching to an unrelated chat', async () => {
	let resolve!: (value: Session) => void;
	const store = sessions as unknown as ReturnType<typeof writable<Session[]>>;
	store.set([row('child', { hidden: true, parent_session_id: 'parent' }), row('other')]);
	vi.mocked(api.get).mockReturnValueOnce(new Promise<Session>(r => { resolve = r; }));
	const view = render(SessionBreadcrumbs, { sessionId: 'child', title: 'Worker' });
	await view.rerender({ sessionId: 'other', title: 'Other chat' });
	resolve(row('parent'));
	await waitFor(() => expect(screen.getByText('Other chat')).toBeTruthy());
	expect(screen.queryByRole('link')).toBeNull();
});

it('does not offer a self-referential parent link', () => {
	const store = sessions as unknown as ReturnType<typeof writable<Session[]>>;
	store.set([row('child', { hidden: true, parent_session_id: 'child' })]);
	render(SessionBreadcrumbs, { sessionId: 'child', title: 'Worker' });
	expect(screen.queryByRole('link')).toBeNull();
	expect(api.get).not.toHaveBeenCalled();
});

it('keeps inaccessible parents inert and keeps the current title usable', async () => {
	vi.mocked(api.get).mockResolvedValueOnce(row('child', { hidden: true, parent_session_id: 'deleted' }))
		.mockRejectedValueOnce(new Error('Not found'));
	render(SessionBreadcrumbs, { title: 'Worker', sessionId: 'child' });
	await waitFor(() => expect(api.get).toHaveBeenCalledTimes(2));
	expect(screen.queryByRole('link')).toBeNull();
	expect(screen.getByRole('button', { name: /Worker/ })).toBeTruthy();
});
