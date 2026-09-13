import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen } from '@testing-library/svelte';
import { writable } from 'svelte/store';
import { createRawSnippet } from 'svelte';

vi.mock('$lib/stores/auth.svelte', () => ({
	username: writable<string | null>('Local owner'),
	isAdmin: writable(true),
}));
vi.mock('$lib/stores/app-data.svelte', () => ({ appData: {} }));
vi.mock('$lib/stores/active-session', () => ({ activeSession: writable({}) }));
vi.mock('$lib/api/client', () => ({
	api: { get: vi.fn(), patch: vi.fn(), delete: vi.fn() },
	forkSession: vi.fn(),
}));

import StudioTopBar from './StudioTopBar.svelte';
import { api } from '$lib/api/client';
import { ACCOUNT_CHANGED_EVENT } from '$lib/api/account';

beforeEach(() => vi.clearAllMocks());
afterEach(() => { cleanup(); vi.useRealTimers(); });

it('does not render an account bubble or its separator, or refresh account metadata', async () => {
	vi.useFakeTimers();
	const { container } = render(StudioTopBar);
	window.dispatchEvent(new Event(ACCOUNT_CHANGED_EVENT));
	window.dispatchEvent(new Event('focus'));
	document.dispatchEvent(new Event('visibilitychange'));
	await vi.advanceTimersByTimeAsync(300_000);
	expect(container.querySelector('.tb-avatar')).toBeNull();
	expect(container.querySelector('.tb-sep')).toBeNull();
	expect(screen.queryByTitle('Local owner')).toBeNull();
	expect(screen.queryByTitle('Account')).toBeNull();
	expect(api.get).not.toHaveBeenCalled();
});

it('keeps the sidebar toggle and project navigation', async () => {
	const onToggleSidebar = vi.fn();
	const onOpenProject = vi.fn();
	const view = render(StudioTopBar, {
		projectLabel: 'ai-shuffle', title: 'Example session',
		onToggleSidebar, onOpenProject,
	});
	expect(screen.getByRole('navigation', { name: 'Breadcrumb' })).toBeTruthy();
	expect(screen.getByText('Example session')).toBeTruthy();
	await fireEvent.click(screen.getByRole('button', { name: 'Hide sidebar' }));
	expect(onToggleSidebar).toHaveBeenCalledOnce();
	await fireEvent.click(screen.getByRole('button', { name: 'ai-shuffle' }));
	expect(onOpenProject).toHaveBeenCalledOnce();
	await view.rerender({ sidebarCollapsed: true });
	expect(screen.getByRole('button', { name: 'Show sidebar' }).getAttribute('aria-pressed')).toBe('false');
});

it('keeps caller-provided header actions', () => {
	const actions = createRawSnippet(() => ({ render: () => '<button>Download session</button>' }));
	render(StudioTopBar, { actions });
	expect(screen.getByRole('button', { name: 'Download session' })).toBeTruthy();
});
