import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/svelte';
import { writable } from 'svelte/store';
import type { Session } from '$lib/api/sessions';

vi.mock('$lib/stores/app-data.svelte', () => ({ appData: { current: { sessions: [] } }, sessions: writable<Session[]>([]) }));
vi.mock('$lib/stores/active-session', () => ({ activeSession: Object.assign(writable({}), { switchSession: vi.fn() }) }));
vi.mock('$lib/api/client', () => ({ api: { get: vi.fn(), patch: vi.fn(), delete: vi.fn() }, forkSession: vi.fn() }));
// A known non-admin: the title menu's chat identity is settled without loading the assistant.
vi.mock('$lib/stores/auth.svelte', () => ({ auth: writable({ status: { is_admin: false } }) }));
import { sessions } from '$lib/stores/app-data.svelte';
import { api } from '$lib/api/client';
import SessionBreadcrumbs from './SessionBreadcrumbs.svelte';
import { activeSession } from '$lib/stores/active-session';
import { layout } from '$lib/stores/layout.svelte';
import { openSessionLink } from '$lib/services/session-navigation';
vi.mock('$lib/services/session-navigation', async (importOriginal) => {
	const original = await importOriginal<typeof import('$lib/services/session-navigation')>();
	return { ...original, openSessionLink: vi.fn(original.openSessionLink) };
});

const row = (id: string, fields: Partial<Session> = {}) => ({ id, title: id, ...fields } as Session);
beforeEach(() => {
	vi.clearAllMocks();
	vi.mocked(api.get).mockReset();
	history.replaceState(null, '', '/');
	layout.setSidebarOverlay(false);
	(sessions as unknown as ReturnType<typeof writable<Session[]>>).set([]);
});
afterEach(() => { cleanup(); history.replaceState(null, '', '/'); });

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

it.each(['/', '/mobile/', '/assets/mobile-ui/'])('keeps a mobile parent fallback in the phone UI when hosted at %s and selects the parent on click', async pathname => {
	history.replaceState(null, '', pathname);
	const parentId = 'parent / chat &+=?#雪';
	(sessions as unknown as ReturnType<typeof writable<Session[]>>).set([
		row('child', { hidden: true, parent_session_id: parentId }), row(parentId, { title: 'Parent chat' })
	]);
	render(SessionBreadcrumbs, { title: 'Worker', sessionId: 'child', mobile: true });
	const link = screen.getByRole('link', { name: 'Open parent chat: Parent chat' });
	const target = new URL(link.getAttribute('href')!, window.location.origin);
	expect(target.origin).toBe(window.location.origin);
	expect(target.pathname).toBe('/mobile/');
	expect(target.searchParams.get('session')).toBe(parentId);
	const event = new MouseEvent('click', { bubbles: true, cancelable: true, button: 0 });
	await fireEvent(link, event);
	expect(event.defaultPrevented).toBe(true);
	expect(activeSession.switchSession).toHaveBeenCalledExactlyOnceWith(parentId, undefined, null, null, null);
	expect(layout.current.activeView).toEqual({ type: 'session', sessionId: parentId });
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

it('renders only the trail in the phone bar subline: the bar draws the title itself', () => {
	const store = sessions as unknown as ReturnType<typeof writable<Session[]>>;
	store.set([row('child', { hidden: true, parent_session_id: 'parent' }), row('parent', { title: 'Plan' })]);
	const view = render(SessionBreadcrumbs, { projectLabel: 'AI Shuffle', title: 'Worker task', sessionId: 'child', mobile: true });
	const nav = screen.getByRole('navigation');
	expect(nav.textContent).toMatch(/AI Shuffle.*Plan/s);
	expect(nav.textContent).not.toContain('Worker task');
	expect(document.querySelector('[aria-current="page"]')).toBeNull();
	expect(nav.querySelectorAll('.crumb-sep')).toHaveLength(1);
	view.unmount();
	store.set([row('plain')]);
	render(SessionBreadcrumbs, { projectLabel: 'AI Shuffle', title: 'Plain chat', sessionId: 'plain', mobile: true });
	expect(screen.getByRole('navigation').textContent?.trim()).toBe('AI Shuffle');
	expect(document.querySelectorAll('.crumb-sep')).toHaveLength(0);
});
