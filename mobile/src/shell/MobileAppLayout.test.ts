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

	it('draws one bar: no separate worker bar or chat-action row', () => {
		mainPanelView.isSessionView = true;
		const { container } = renderLayout();
		expect(container.querySelectorAll('header')).toHaveLength(1);
		expect(container.querySelector('.conn-chip, .conn-bar')).toBeNull();
		expect(screen.queryByRole('banner', { name: 'Chat actions' })).toBeNull();
	});

	// #1816: export moved into the composer's Chat controls; the bar keeps Git.
	it('puts the chat actions (Git, private access) on the one bar in a chat, with no export there', () => {
		mainPanelView.isSessionView = true;
		mainPanelView.embedded = true;
		renderLayout();
		const header = screen.getByTestId('app-top-bar');
		const git = screen.getByRole('button', { name: 'Commit' });
		expect(screen.queryByRole('button', { name: 'Download chat' })).toBeNull();
		expect(screen.getByTestId('bar-actions').contains(git)).toBe(true);
		expect(header.contains(git)).toBe(true);
		expect(header.getAttribute('data-hide-stt')).toBe('true');
	});

	it('passes no chat actions in a subagent chat (no Git or private access there)', () => {
		mainPanelView.isSessionView = true;
		mainPanelView.subagentChat = true;
		const { container } = renderLayout();
		expect(screen.queryByTestId('bar-actions')).toBeNull();
		expect(screen.queryByRole('button', { name: 'Commit' })).toBeNull();
		expect(container.querySelectorAll('header')).toHaveLength(1);
	});

	it('passes no chat actions on non-session pages and keeps voice input there', () => {
		const { container } = renderLayout();
		expect(container.querySelectorAll('header')).toHaveLength(1);
		expect(screen.queryByTestId('bar-actions')).toBeNull();
		expect(screen.getByTestId('app-top-bar').getAttribute('data-hide-stt')).toBe('false');
	});

	it('keeps navigation but hides global voice input for embedded feature panels', () => {
		mainPanelView.embedded = true;
		const { container } = renderLayout();
		expect(container.querySelectorAll('header')).toHaveLength(1);
		expect(screen.getByTestId('app-top-bar').getAttribute('data-hide-stt')).toBe('true');
	});
});
