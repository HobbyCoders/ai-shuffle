import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen } from '@testing-library/svelte';

vi.mock('$lib/components/layout/MainPanel.svelte', async () => ({
	default: (await import('./test-fixtures/MainPanelStub.svelte')).default
}));
vi.mock('./AppTopBar.svelte', async () => ({
	default: (await import('./test-fixtures/AppTopBarStub.svelte')).default
}));
vi.mock('$lib/components/CredentialStorageBanner.svelte', () => ({ default: () => {} }));
vi.mock('$lib/stores/layout.svelte', () => ({ layout: { setSidebarOverlay: vi.fn() } }));
vi.mock('$lib/api/installs', () => ({
	listInstalls: vi.fn().mockResolvedValue({ installs: [
		{ id: 'pc', device_name: 'DesktopWorker', base_url: 'https://pc.test', platform: null, version: null, capabilities: {}, last_seen: null, online: true, is_self: true }
	], self_instance_id: 'pc' })
}));
// PageHeader's own stores: voice input off, idle.
vi.mock('$lib/stores/hotkeyPreferences.svelte', async () => {
	const { writable } = await import('svelte/store');
	return { hotkeyPreferences: writable({ globalSttEnabled: false }) };
});
vi.mock('$lib/services/globalStt', async () => {
	const { writable } = await import('svelte/store');
	return { globalSttState: writable({}), triggerToggle: vi.fn() };
});

import MobileAppLayout from './MobileAppLayout.svelte';
import { layout } from '$lib/stores/layout.svelte';
import { mainPanelView } from './test-fixtures/main-panel-view';

function renderLayout() {
	const props = {
		isAdmin: true,
		onNewSession: vi.fn(),
		onSettledOpenSession: vi.fn(),
		onOpenSession: vi.fn(),
		onOpenFeature: vi.fn(),
		onOpenSpotlight: vi.fn()
	};
	return { props, ...render(MobileAppLayout, { props }) };
}

beforeEach(() => {
	Object.assign(mainPanelView, { isSessionView: false, embedded: false, subagentChat: false });
	vi.mocked(layout.setSidebarOverlay).mockClear();
});

afterEach(() => cleanup());

describe('MobileAppLayout', () => {
	it('always runs the sidebar as an overlay on the mobile shell', () => {
		renderLayout();
		expect(layout.setSidebarOverlay).toHaveBeenCalledWith(true);
	});

	it('hands the shell props to the app bar and the session opener to the main panel', async () => {
		const { props } = renderLayout();
		expect(screen.getByTestId('app-top-bar').getAttribute('data-admin')).toBe('true');
		await fireEvent.click(screen.getByRole('button', { name: 'Bar open session' }));
		await fireEvent.click(screen.getByRole('button', { name: 'Bar spotlight' }));
		await fireEvent.click(screen.getByRole('button', { name: 'Panel open session' }));
		expect(props.onOpenSession.mock.calls).toEqual([['s-1'], ['s-9']]);
		expect(props.onOpenSpotlight).toHaveBeenCalledTimes(1);
	});

	it('opens with the "Connected to" bar, above the app bar', async () => {
		const { container } = renderLayout();
		const bar = await screen.findByRole('button', { name: 'Connected to DesktopWorker. Switch worker' });
		expect(container.firstElementChild?.contains(bar)).toBe(true);
		expect(bar.compareDocumentPosition(screen.getByTestId('app-top-bar')) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
	});

	// #1816: export moved into the composer's Chat controls; the chat-action row keeps Git.
	it('puts Git right in a second row, outside the session title bar, with no export there', () => {
		mainPanelView.isSessionView = true;
		mainPanelView.embedded = true;
		const { container } = renderLayout();
		const header = screen.getByTestId('app-top-bar');
		const git = screen.getByRole('button', { name: 'Commit' });
		expect(screen.queryByRole('button', { name: 'Download chat' })).toBeNull();
		expect(header.contains(git)).toBe(false);
		const actions = screen.getByRole('banner', { name: 'Chat actions' });
		expect(actions.querySelector('.hdr-right')?.contains(git)).toBe(true);
		expect(header.compareDocumentPosition(actions) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
		expect(container.querySelectorAll('header')).toHaveLength(2);
		expect(header.getAttribute('data-hide-stt')).toBe('true');
	});

	it('draws no empty chat-action row in a subagent chat (no Git or private access there)', () => {
		mainPanelView.isSessionView = true;
		mainPanelView.subagentChat = true;
		const { container } = renderLayout();
		expect(screen.queryByRole('banner', { name: 'Chat actions' })).toBeNull();
		expect(container.querySelectorAll('header')).toHaveLength(1);
	});

	it('keeps one app bar and no chat-action row on non-session pages', () => {
		const { container } = renderLayout();
		expect(container.querySelectorAll('header')).toHaveLength(1);
		expect(screen.getByTestId('app-top-bar').getAttribute('data-hide-stt')).toBe('false');
	});

	it('keeps navigation but hides global voice input for embedded feature panels', () => {
		mainPanelView.embedded = true;
		const { container } = renderLayout();
		expect(container.querySelectorAll('header')).toHaveLength(1);
		expect(screen.getByTestId('app-top-bar').getAttribute('data-hide-stt')).toBe('true');
	});
});
