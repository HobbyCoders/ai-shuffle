import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen } from '@testing-library/svelte';
import { tick } from 'svelte';
import type { Writable } from 'svelte/store';

// The drawer hosts the real Sidebar in the app; here a stub exposes each of its
// navigation callbacks as a button, and the stores are plain writables.
vi.mock('$lib/components/layout/Sidebar.svelte', async () => ({
	default: (await import('./test-fixtures/SidebarStub.svelte')).default
}));
vi.mock('$lib/components/layout/SessionBreadcrumbs.svelte', async () => ({
	default: (await import('./test-fixtures/BreadcrumbsStub.svelte')).default
}));
vi.mock('$lib/stores/layout.svelte', async () => {
	const { writable } = await import('svelte/store');
	return { activeView: writable(null) };
});
vi.mock('$lib/stores/active-session', async () => {
	const { writable } = await import('svelte/store');
	return { activeSession: writable({ sessionId: null, title: '', messages: [], project: null }) };
});
vi.mock('$lib/stores/notifications', async () => {
	const { writable } = await import('svelte/store');
	return { unreadCount: writable(0) };
});
vi.mock('$lib/stores/app-data.svelte', async () => {
	const { writable } = await import('svelte/store');
	return { projects: writable([]) };
});

import AppTopBar from './AppTopBar.svelte';
import * as layoutStore from '$lib/stores/layout.svelte';
import * as sessionStore from '$lib/stores/active-session';
import * as notificationStore from '$lib/stores/notifications';
import * as appData from '$lib/stores/app-data.svelte';

const activeView = layoutStore.activeView as unknown as Writable<{ type: string } | null>;
const activeSession = sessionStore.activeSession as unknown as Writable<Record<string, unknown>>;
const unreadCount = notificationStore.unreadCount as unknown as Writable<number>;
const projects = appData.projects as unknown as Writable<{ id: string; name: string }[]>;

function renderBar(extra: Record<string, unknown> = {}) {
	const props = {
		onNewSession: vi.fn(),
		onSettledOpenSession: vi.fn(),
		onOpenSession: vi.fn(),
		onOpenFeature: vi.fn(),
		...extra
	};
	return { props, ...render(AppTopBar, { props }) };
}

const menuButton = () => screen.getByRole('button', { name: /^Open menu/ });

beforeEach(() => {
	activeView.set(null);
	activeSession.set({ sessionId: null, title: '', messages: [], project: null });
	unreadCount.set(0);
	projects.set([]);
});

afterEach(() => cleanup());

describe('AppTopBar title', () => {
	it('falls back to the app name with no view and maps feature views to page titles', async () => {
		renderBar();
		expect(screen.getByText('AI Shuffle')).toBeTruthy();
		activeView.set({ type: 'settings' });
		await tick();
		expect(screen.getByText('Settings')).toBeTruthy();
		activeView.set({ type: 'board' });
		await tick();
		expect(screen.getByText('Sessions')).toBeTruthy();
		activeView.set({ type: 'mcp' });
		await tick();
		expect(screen.getByText('Build')).toBeTruthy();
		activeView.set({ type: 'unknown-view' });
		await tick();
		expect(screen.getByText('AI Shuffle')).toBeTruthy();
	});

	it('shows "New Chat" for a session view with no session yet', async () => {
		activeView.set({ type: 'session' });
		renderBar();
		expect(screen.getByText('New Chat')).toBeTruthy();
		expect(screen.queryByRole('navigation', { name: 'Breadcrumbs' })).toBeNull();
	});

	it('renders breadcrumbs with the chat title and its project name for a bound session', async () => {
		projects.set([{ id: 'p1', name: 'Website' }]);
		activeSession.set({ sessionId: 's-1', title: 'Fix login', messages: [], project: 'p1' });
		activeView.set({ type: 'session' });
		renderBar();
		const crumbs = screen.getByRole('navigation', { name: 'Breadcrumbs' });
		expect(crumbs.getAttribute('data-session-id')).toBe('s-1');
		expect(crumbs.textContent).toContain('Website');
		expect(crumbs.textContent).toContain('Fix login');
	});

	it('labels an untitled bound session and falls back to "Sessions" for an unknown project', async () => {
		activeSession.set({ sessionId: 's-1', title: '  ', messages: [], project: 'gone' });
		activeView.set({ type: 'session' });
		renderBar();
		const crumbs = screen.getByRole('navigation', { name: 'Breadcrumbs' });
		expect(crumbs.textContent).toContain('Untitled');
		expect(crumbs.textContent).toContain('Sessions');
	});
});

describe('AppTopBar unread indicator', () => {
	it('names the unread count on the menu button and shows the dot only while the drawer is closed', async () => {
		const { container } = renderBar();
		expect(menuButton().getAttribute('aria-label')).toBe('Open menu');
		expect(container.querySelector('.tb-unread')).toBeNull();
		unreadCount.set(3);
		await tick();
		expect(menuButton().getAttribute('aria-label')).toBe('Open menu (3 unread notifications)');
		expect(container.querySelector('.tb-unread')).toBeTruthy();
		await fireEvent.click(menuButton());
		expect(container.querySelector('.tb-unread')).toBeNull();
	});
});

describe('AppTopBar drawer', () => {
	it('opens the drawer, marks the button expanded and moves focus to the close button', async () => {
		renderBar({ isAdmin: true });
		expect(screen.queryByTestId('sidebar')).toBeNull();
		expect(menuButton().getAttribute('aria-expanded')).toBe('false');
		await fireEvent.click(menuButton());
		await tick();
		const drawer = screen.getByRole('complementary', { name: 'Navigation' });
		expect(menuButton().getAttribute('aria-expanded')).toBe('true');
		expect(menuButton().getAttribute('aria-controls')).toBe(drawer.id);
		expect(screen.getByTestId('sidebar').getAttribute('data-admin')).toBe('true');
		const close = drawer.querySelector('.drawer-close');
		expect(document.activeElement).toBe(close);
	});

	it('closes from the close button and returns focus to the menu button', async () => {
		renderBar();
		await fireEvent.click(menuButton());
		await tick();
		const drawer = screen.getByRole('complementary', { name: 'Navigation' });
		await fireEvent.click(drawer.querySelector('.drawer-close')!);
		expect(screen.queryByTestId('sidebar')).toBeNull();
		expect(document.activeElement).toBe(menuButton());
	});

	it('closes from the overlay (tap and Enter) and from Escape', async () => {
		renderBar();
		await fireEvent.click(menuButton());
		const overlays = screen.getAllByRole('button', { name: 'Close menu' });
		const overlay = overlays.find((el) => el.classList.contains('drawer-overlay'))!;
		await fireEvent.click(overlay);
		expect(screen.queryByTestId('sidebar')).toBeNull();

		await fireEvent.click(menuButton());
		const overlay2 = screen.getAllByRole('button', { name: 'Close menu' }).find((el) => el.classList.contains('drawer-overlay'))!;
		await fireEvent.keyDown(overlay2, { key: 'Enter' });
		expect(screen.queryByTestId('sidebar')).toBeNull();

		await fireEvent.click(menuButton());
		await fireEvent.keyDown(window, { key: 'Escape' });
		expect(screen.queryByTestId('sidebar')).toBeNull();
	});

	it('leaves focus alone when Escape is pressed with the drawer closed', async () => {
		renderBar();
		const outside = document.createElement('button');
		document.body.append(outside);
		outside.focus();
		await fireEvent.keyDown(window, { key: 'Escape' });
		expect(document.activeElement).toBe(outside);
		outside.remove();
	});

	it('closes the drawer before running each navigation action with its arguments', async () => {
		const onOpenSpotlight = vi.fn();
		const { props } = renderBar({ onOpenSpotlight });
		const cases: [string, () => void][] = [
			['Stub new session', () => expect(props.onNewSession).toHaveBeenCalledTimes(1)],
			['Stub settled session', () => expect(props.onSettledOpenSession).toHaveBeenCalledTimes(1)],
			['Stub open session', () => expect(props.onOpenSession).toHaveBeenCalledWith('s-2')],
			['Stub open settings', () => expect(props.onOpenFeature).toHaveBeenCalledWith('settings')],
			['Stub spotlight', () => expect(onOpenSpotlight).toHaveBeenCalledTimes(1)]
		];
		for (const [label, check] of cases) {
			await fireEvent.click(menuButton());
			await fireEvent.click(screen.getByRole('button', { name: label }));
			expect(screen.queryByTestId('sidebar')).toBeNull();
			check();
		}
	});

	it('passes no spotlight action to the sidebar when the page gives none', async () => {
		renderBar();
		await fireEvent.click(menuButton());
		expect(screen.queryByRole('button', { name: 'Stub spotlight' })).toBeNull();
	});

	it('closes when a notification opens a session through the shared open-session event', async () => {
		renderBar();
		await fireEvent.click(menuButton());
		await fireEvent.click(screen.getByRole('button', { name: 'Stub notification' }));
		expect(screen.queryByTestId('sidebar')).toBeNull();
	});

	it('stops listening for open-session once unmounted', async () => {
		const remove = vi.spyOn(window, 'removeEventListener');
		const { unmount } = renderBar();
		unmount();
		expect(remove.mock.calls.some(([type]) => type === 'open-session')).toBe(true);
		remove.mockRestore();
	});
});
