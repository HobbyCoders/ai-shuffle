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

vi.mock('$lib/stores/hotkeyPreferences.svelte', async () => {
	const { writable } = await import('svelte/store');
	return { hotkeyPreferences: writable({ globalSttEnabled: false }) };
});
vi.mock('$lib/services/globalStt', async () => {
	const { writable } = await import('svelte/store');
	return { globalSttState: writable({ isRecording: false }), triggerToggle: vi.fn() };
});

import { hotkeyPreferences } from '$lib/stores/hotkeyPreferences.svelte';
import { triggerToggle } from '$lib/services/globalStt';
import AppTopBar from './AppTopBar.svelte';
import * as layoutStore from '$lib/stores/layout.svelte';
import * as sessionStore from '$lib/stores/active-session';
import * as notificationStore from '$lib/stores/notifications';
import * as appData from '$lib/stores/app-data.svelte';

const activeView = layoutStore.activeView as unknown as Writable<{ type: string } | null>;
const activeSession = sessionStore.activeSession as unknown as Writable<Record<string, unknown>>;
const unreadCount = notificationStore.unreadCount as unknown as Writable<number>;
const projects = appData.projects as unknown as Writable<{ id: string; name: string }[]>;
const hotkeys = hotkeyPreferences as unknown as Writable<{ globalSttEnabled: boolean }>;

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
/** The drawer stays mounted after first use; closed means absent or inert. */
const drawerClosed = () => {
	const drawer = document.getElementById('mobile-nav-drawer');
	return !drawer || drawer.getAttribute('aria-hidden') === 'true';
};

beforeEach(() => {
	activeView.set(null);
	activeSession.set({ sessionId: null, title: '', messages: [], project: null });
	unreadCount.set(0);
	projects.set([]);
	hotkeys.set({ globalSttEnabled: false });
	vi.mocked(triggerToggle).mockClear();
});

afterEach(() => cleanup());

describe('AppTopBar actions', () => {
	it('names the integrated Assistant home without becoming a new session mode', () => {
		activeView.set({ type: 'assistant' });
		renderBar();
		expect(screen.getByText('Assistant')).toBeTruthy();
		expect(screen.queryByTestId('breadcrumbs')).toBeNull();
	});

	it('keeps chat action controls out of the title bar', () => {
		activeView.set({ type: 'session' });
		const { container } = renderBar();
		expect(container.querySelector('.tb-actions')?.textContent).toBe('');
		expect(container.querySelector('.page-header')).toBeNull();
	});

	it('retains global voice input in the single bar except when the chat owns it', async () => {
		hotkeys.set({ globalSttEnabled: true });
		const view = renderBar();
		await fireEvent.click(screen.getByRole('button', { name: 'Start voice input' }));
		expect(triggerToggle).toHaveBeenCalledTimes(1);
		await view.rerender({ hideGlobalStt: true });
		expect(screen.queryByRole('button', { name: 'Start voice input' })).toBeNull();
	});
});

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
		activeView.set({ type: 'profile' });
		await tick();
		expect(screen.getByText('Build')).toBeTruthy();
		// MCP servers moved into Settings (#1684).
		activeView.set({ type: 'mcp' });
		await tick();
		expect(screen.getByText('Settings')).toBeTruthy();
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
		expect(drawerClosed()).toBe(true);
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
		expect(drawerClosed()).toBe(true);
		expect(document.activeElement).toBe(menuButton());
	});

	it('closes from the overlay (tap and Enter) and from Escape', async () => {
		renderBar();
		await fireEvent.click(menuButton());
		const overlays = screen.getAllByRole('button', { name: 'Close menu' });
		const overlay = overlays.find((el) => el.classList.contains('drawer-overlay'))!;
		await fireEvent.click(overlay);
		expect(drawerClosed()).toBe(true);

		await fireEvent.click(menuButton());
		const overlay2 = screen.getAllByRole('button', { name: 'Close menu' }).find((el) => el.classList.contains('drawer-overlay'))!;
		await fireEvent.keyDown(overlay2, { key: 'Enter' });
		expect(drawerClosed()).toBe(true);

		await fireEvent.click(menuButton());
		await fireEvent.keyDown(window, { key: 'Escape' });
		expect(drawerClosed()).toBe(true);
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
			expect(drawerClosed()).toBe(true);
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
		expect(drawerClosed()).toBe(true);
	});

	it('returns focus to the menu button when a navigation action or a notification closes the drawer', async () => {
		renderBar();
		for (const label of ['Stub open settings', 'Stub notification']) {
			await fireEvent.click(menuButton());
			await tick();
			const item = screen.getByRole('button', { name: label });
			item.focus();
			await fireEvent.click(item);
			await tick();
			expect(drawerClosed()).toBe(true);
			expect(document.activeElement).toBe(menuButton());
		}
	});

	it('lets a navigation action move focus where it belongs after the drawer closes', async () => {
		const target = document.createElement('input');
		document.body.append(target);
		renderBar({ onOpenSession: () => target.focus() });
		await fireEvent.click(menuButton());
		await tick();
		const item = screen.getByRole('button', { name: 'Stub open session' });
		item.focus();
		await fireEvent.click(item);
		await tick();
		expect(document.activeElement).toBe(target);
		target.remove();
	});

	it('stops listening for open-session once unmounted', async () => {
		const remove = vi.spyOn(window, 'removeEventListener');
		const { unmount } = renderBar();
		unmount();
		expect(remove.mock.calls.some(([type]) => type === 'open-session')).toBe(true);
		remove.mockRestore();
	});
});

describe('AppTopBar drawer lifecycle', () => {
	it('mounts on first open and stays mounted, inert and hidden, once closed', async () => {
		renderBar();
		expect(document.getElementById('mobile-nav-drawer')).toBeNull();
		await fireEvent.click(menuButton());
		await fireEvent.keyDown(window, { key: 'Escape' });
		const drawer = document.getElementById('mobile-nav-drawer')!;
		expect(drawer).toBeTruthy();
		expect((drawer as HTMLElement & { inert?: boolean }).inert || drawer.hasAttribute('inert')).toBe(true);
		expect(drawer.getAttribute('aria-hidden')).toBe('true');
		expect(screen.getByTestId('sidebar')).toBeTruthy();
	});

	it('pushes a history entry on open, closes on Back, and pops its own entry when closed in the app', async () => {
		const nav = await import('$app/navigation');
		const { page } = await import('$app/state');
		const push = vi.spyOn(nav, 'pushState').mockImplementation((_url, state) => {
			page.state = state as Record<string, unknown>;
		});
		const back = vi.spyOn(history, 'back').mockImplementation(() => {
			page.state = {};
		});
		try {
			renderBar();
			await fireEvent.click(menuButton());
			await tick();
			expect(push).toHaveBeenCalledTimes(1);
			expect(page.state.mobileDrawer).toBe(true);

			// System Back: the router drops our entry and the drawer follows.
			page.state = {};
			await tick();
			expect(drawerClosed()).toBe(true);
			expect(back).not.toHaveBeenCalled();

			// Closed from inside the app: our entry is popped exactly once.
			await fireEvent.click(menuButton());
			await tick();
			await fireEvent.keyDown(window, { key: 'Escape' });
			await tick();
			expect(back).toHaveBeenCalledTimes(1);
			expect(drawerClosed()).toBe(true);
		} finally {
			push.mockRestore();
			back.mockRestore();
			page.state = {};
		}
	});
});

describe('AppTopBar drawer swipe', () => {
	// Event time in ms; each swipe step advances it by `stepMs`.
	let clock = 0;
	let stepMs = 16;
	function touch(target: Element, type: string, x: number, y = 100) {
		const t = { identifier: 1, clientX: x, clientY: y, target } as unknown as Touch;
		const list = type === 'touchend' ? [] : [t];
		const event = new Event(type, { bubbles: true, cancelable: true }) as TouchEvent;
		Object.defineProperty(event, 'touches', { value: list });
		Object.defineProperty(event, 'changedTouches', { value: [t] });
		Object.defineProperty(event, 'timeStamp', { value: (clock += stepMs) });
		target.dispatchEvent(event);
		return event;
	}

	function swipe(target: Element, from: number, to: number) {
		touch(target, 'touchstart', from);
		const steps = 6;
		for (let i = 1; i <= steps; i++) touch(target, 'touchmove', from + ((to - from) * i) / steps);
		touch(target, 'touchend', to);
	}

	beforeEach(() => {
		Object.defineProperty(window, 'innerWidth', { configurable: true, value: 400 });
		stepMs = 16;
	});

	it('opens on a long right swipe across the main screen', async () => {
		renderBar();
		const main = document.createElement('div');
		document.body.append(main);
		swipe(main, 20, 260);
		await tick();
		expect(drawerClosed()).toBe(false);
		main.remove();
	});

	it('springs back on a short, slow right swipe and ignores a vertical one', async () => {
		renderBar();
		const main = document.createElement('div');
		document.body.append(main);
		stepMs = 120;
		swipe(main, 20, 70);
		await tick();
		expect(drawerClosed()).toBe(true);

		touch(main, 'touchstart', 20, 100);
		touch(main, 'touchmove', 25, 160);
		touch(main, 'touchmove', 200, 170);
		touch(main, 'touchend', 200, 170);
		await tick();
		expect(drawerClosed()).toBe(true);
		main.remove();
	});

	it('does not open from a text field', async () => {
		renderBar();
		const field = document.createElement('textarea');
		document.body.append(field);
		swipe(field, 20, 300);
		await tick();
		expect(drawerClosed()).toBe(true);
		field.remove();
	});

	it('closes on a left swipe inside the drawer, but not from a session row', async () => {
		renderBar();
		await fireEvent.click(menuButton());
		await tick();
		const drawer = document.getElementById('mobile-nav-drawer')!;
		const row = document.createElement('div');
		row.setAttribute('data-session-card', '');
		drawer.querySelector('[data-testid="sidebar"]')!.append(row);
		swipe(row, 300, 40);
		await tick();
		expect(drawerClosed()).toBe(false);

		swipe(screen.getByTestId('sidebar'), 300, 40);
		await tick();
		expect(drawerClosed()).toBe(true);
	});
});
