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
	Object.assign(mainPanelView, { isSessionView: false, embedded: false });
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

	it('puts download and git actions in the FIRST and only shell header on a session view', () => {
		mainPanelView.isSessionView = true;
		mainPanelView.embedded = true;
		const { container } = renderLayout();
		const header = screen.getByTestId('app-top-bar');
		expect(header.contains(screen.getByRole('button', { name: 'Download chat' }))).toBe(true);
		expect(header.contains(screen.getByRole('button', { name: 'Commit' }))).toBe(true);
		expect(container.querySelectorAll('header')).toHaveLength(1);
		expect(header.getAttribute('data-hide-stt')).toBe('true');
	});

	it('keeps one app bar and allows global voice input on non-embedded pages', () => {
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
