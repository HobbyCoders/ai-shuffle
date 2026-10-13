import { afterEach, describe, expect, it, vi } from 'vitest';
import { cleanup, render, screen } from '@testing-library/svelte';

// The one bar draws the real worker chip, so the Android app must still hear
// `worker-bar shown:true` from it (or it would draw its own native bar), on a
// chat and off one alike.
vi.mock('$lib/components/layout/Sidebar.svelte', async () => ({
	default: (await import('./test-fixtures/SidebarStub.svelte')).default
}));
vi.mock('$lib/components/layout/SessionBreadcrumbs.svelte', async () => ({
	default: (await import('./test-fixtures/BreadcrumbsStub.svelte')).default
}));
vi.mock('$lib/components/layout/SessionTitleMenu.svelte', async () => ({
	default: (await import('./test-fixtures/TitleMenuStub.svelte')).default
}));
vi.mock('$lib/api/installs', () => ({
	listInstalls: vi.fn().mockResolvedValue({ installs: [
		{ id: 'pc', device_name: 'DesktopWorker', base_url: 'https://pc.test', platform: null, version: null, capabilities: {}, last_seen: null, online: true, is_self: true }
	], self_instance_id: 'pc' })
}));
vi.mock('$lib/stores/layout.svelte', async () => {
	const { writable } = await import('svelte/store');
	return { activeView: writable({ type: 'session' }) };
});
vi.mock('$lib/stores/active-session', async () => {
	const { writable } = await import('svelte/store');
	return {
		activeSession: writable({ sessionId: 's-1', title: 'Fix login', messages: [], project: null }),
		streamingSessions: writable(new Map())
	};
});
vi.mock('$lib/stores/notifications', async () => {
	const { writable } = await import('svelte/store');
	return { unreadCount: writable(0), notify: vi.fn() };
});
vi.mock('$lib/stores/app-data.svelte', async () => {
	const { writable } = await import('svelte/store');
	return { projects: writable([]), sessions: writable([]) };
});
vi.mock('$lib/stores/hotkeyPreferences.svelte', async () => {
	const { writable } = await import('svelte/store');
	return { hotkeyPreferences: writable({ globalSttEnabled: false }) };
});
vi.mock('$lib/services/globalStt', async () => {
	const { writable } = await import('svelte/store');
	return { globalSttState: writable({ isRecording: false }), triggerToggle: vi.fn() };
});

import AppTopBar from './AppTopBar.svelte';

type TestWindow = Window & { shuffleShell?: unknown };
const postMessage = vi.fn();

afterEach(() => {
	cleanup();
	postMessage.mockReset();
	delete window.__SHUFFLE_MOBILE__;
	delete (window as TestWindow).shuffleShell;
});

describe('AppTopBar in the Android app', () => {
	it('draws the worker switcher in the bar and tells the app to hide its own worker bar', async () => {
		window.__SHUFFLE_MOBILE__ = { apiOrigin: 'https://pc.test', token: 'shc_test', native: { platform: 'android', bridge: 1, capabilities: ['worker-switch'] } };
		(window as TestWindow).shuffleShell = { postMessage };
		const { container, unmount } = render(AppTopBar, {
			props: { onNewSession: vi.fn(), onSettledOpenSession: vi.fn(), onOpenSession: vi.fn(), onOpenFeature: vi.fn() }
		});
		const chip = await screen.findByRole('button', { name: 'Connected to DesktopWorker. Switch worker' });
		expect(container.querySelector('header.topbar .tb-sub')?.contains(chip)).toBe(true);
		const posted = () => postMessage.mock.calls.map(([raw]) => JSON.parse(raw));
		expect(posted()).toContainEqual({ type: 'worker-bar', shown: true });
		unmount();
		expect(posted().at(-1)).toEqual({ type: 'worker-bar', shown: false });
	});
});
