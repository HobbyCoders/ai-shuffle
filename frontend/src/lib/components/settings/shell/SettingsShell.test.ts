import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/svelte';
import { afterEach, beforeAll, beforeEach, describe, expect, it, onTestFinished, vi } from 'vitest';
import { tick } from 'svelte';

const mocks = vi.hoisted(() => ({ pushState: vi.fn(), supportCaps: vi.fn() }));

vi.mock('$app/navigation', () => ({ pushState: mocks.pushState, goto: vi.fn() }));
vi.mock('$lib/stores/auth.svelte', async () => {
	const { writable } = await import('svelte/store');
	return { auth: { checkAuth: vi.fn() }, isAdmin: writable(true) };
});
vi.mock('$lib/api/support', async (importOriginal) => ({
	...(await importOriginal<object>()),
	getSupportCapabilities: mocks.supportCaps
}));
vi.mock('$lib/stores/theme.svelte', async () => {
	const { writable } = await import('svelte/store');
	return {
		theme: { setTheme: vi.fn(), setPreset: vi.fn(), current: { preference: 'dark', resolved: 'dark', presetId: null } },
		themePreference: writable('dark'),
		currentPresetId: writable(null),
		themePresets: [{ id: 'mono', label: 'Mono', styles: { light: {}, dark: {} } }]
	};
});
vi.mock('$lib/stores/timezonePreference.svelte', async () => {
	const { writable } = await import('svelte/store');
	return { timezonePreference: { ...writable('local'), load: vi.fn(async () => {}), setTimezone: vi.fn() } };
});

import { page } from '$app/state';
import { isAdmin } from '$lib/stores/auth.svelte';
import { layout } from '$lib/stores/layout.svelte';
import { platform } from '$lib/stores/platform.svelte';
import { registerSearchShortcut } from '$lib/services/search-shortcut';
import { FEATURE_PANEL_CONTEXT } from '$lib/components/layout/feature-panel-context';
import { settingsNav, encodeTarget } from '$lib/settings/navigation.svelte';
import { aiContentPrefill, supportCaps, supportReportDialog } from '$lib/stores/support-report.svelte';
import { loadCollection } from '../pages';
import SettingsShell from './SettingsShell.svelte';
import PanelHost from '../../layout/view/test-fixtures/PanelHost.svelte';

const admin = isAdmin as unknown as { set: (v: boolean) => void };

beforeEach(() => {
	vi.stubGlobal('ResizeObserver', class { observe() {} unobserve() {} disconnect() {} });
	// Shared phone chrome observes the actual viewport breakpoint. jsdom omits this API.
	vi.stubGlobal('matchMedia', vi.fn((media: string) => ({
		media, matches: window.innerWidth <= 600, onchange: null,
		addEventListener: vi.fn(), removeEventListener: vi.fn(),
		addListener: vi.fn(), removeListener: vi.fn(), dispatchEvent: vi.fn()
	})));
	settingsNav.reset();
	sessionStorage.clear();
	mocks.pushState.mockReset();
	admin.set(true);
	page.state = {};
	supportCaps.reset();
	mocks.supportCaps.mockReset();
	mocks.supportCaps.mockRejectedValue(new Error('offline'));
});
afterEach(() => {
	cleanup();
	platform.set('desktop');
	vi.restoreAllMocks();
	vi.unstubAllGlobals();
});

function nav() {
	return screen.getByRole('navigation', { name: 'Settings' });
}

describe('SettingsShell index', () => {
	it('keeps / focusing search when the index renders under the Sidebar host', async () => {
		render(PanelHost);
		render(SettingsShell);
		await tick();
		const general = within(nav()).getByRole('button', { name: 'General' });
		expect(document.querySelector('[data-test-host]')?.contains(general)).toBe(true);
		general.focus();
		await fireEvent.keyDown(general, { key: '/' });
		await waitFor(() => expect(document.activeElement).toBe(screen.getByRole('combobox', { name: 'Search settings' })));
	});
	it('lists categories in three groups and opens General', async () => {
		render(SettingsShell);
		const index = nav();
		for (const group of ['Personal', 'AI', 'This worker']) expect(within(index).getByText(group)).toBeTruthy();
		const names = within(index)
			.getAllByRole('button')
			.map((b) => b.textContent?.trim());
		expect(names).toEqual([
			'General',
			'Voice',
			'Providers',
			'Default models',
			'Model tuning',
			'Usage',
			'Memory',
			'Integrations',
			'Plugins',
			'MCP servers',
			'Hooks',
			'Devices',
			'Cloud',
			'Security',
			'System',
			'Logs'
		]);
		expect(within(index).getByRole('button', { name: 'General' }).getAttribute('aria-current')).toBe('page');
		expect(screen.getByRole('heading', { level: 1, name: 'General' })).toBeTruthy();
		expect(screen.getByRole('heading', { name: 'Appearance' })).toBeTruthy();
	});

	it('moves between categories with Up and Down', async () => {
		render(SettingsShell);
		const general = within(nav()).getByRole('button', { name: 'General' });
		general.focus();
		await fireEvent.keyDown(general, { key: 'ArrowDown' });
		expect(document.activeElement).toBe(within(nav()).getByRole('button', { name: 'Voice' }));
	});

	it('loads a collection page on first visit and focuses its heading', async () => {
		// Collection readers poll only when their real Settings host is active.
		layout.openFeaturePage('settings');
		onTestFinished(() => layout.openChat(null));
		render(SettingsShell);
		await fireEvent.click(within(nav()).getByRole('button', { name: 'Logs' }));
		// Wait for the real lazy module, not Testing Library's 1s query budget:
		// a cold Vite transform can exceed it when other workers share the host.
		// Keep this after the click so we still exercise first-visit loading.
		await vi.dynamicImportSettled();
		const heading = await screen.findByRole('heading', { level: 1, name: 'Logs' });
		expect(await screen.findByText('No logs found.')).toBeTruthy();
		expect(screen.queryByPlaceholderText('Search logs')).toBeNull();
		expect(screen.getByRole('separator', { name: 'Resize list' })).toBeTruthy();
		await waitFor(() => expect(document.activeElement).toBe(heading));
		expect(within(nav()).getByRole('button', { name: 'Logs' }).getAttribute('aria-current')).toBe('page');
		// The cold lazy import alone has passed 5s on a loaded hosted runner.
	}, 30_000);

	it('still focuses the heading when the page takes seconds to load', async () => {
		vi.useFakeTimers();
		onTestFinished(() => {
			vi.useRealTimers();
			settingsNav.focusRequest = null;
		});
		const { container } = render(SettingsShell);
		settingsNav.focusRequest = { pane: 'detail', n: (settingsNav.focusRequest?.n ?? 0) + 1000 };
		// No heading for 6s: longer than the cold load seen on a hosted runner.
		await vi.advanceTimersByTimeAsync(6_000);
		expect(container.querySelector('[data-settings-heading="detail"]')).toBeNull();
		const late = document.createElement('h2');
		late.tabIndex = -1;
		late.dataset.settingsHeading = 'detail';
		container.querySelector('.settings-shell')!.append(late);
		await vi.advanceTimersByTimeAsync(100);
		expect(document.activeElement).toBe(late);
	});

	it('shows an attention count from a category module', async () => {
		const { MODULES } = await import('$lib/settings/registry');
		const before = MODULES.logs;
		MODULES.logs = { entries: [], attention: () => ({ count: 3, tone: 'err' }) };
		try {
			render(SettingsShell);
			const logs = within(nav()).getByRole('button', { name: /Logs/ });
			expect(logs.textContent).toContain('3');
			expect(within(logs).getByText(/items need attention/)).toBeTruthy();
		} finally {
			MODULES.logs = before;
		}
	});
});

describe('SettingsShell search', () => {
	it('replaces the nav with results and jumps to the row, flashing it', async () => {
		const { container } = render(SettingsShell);
		await fireEvent.click(within(nav()).getByRole('button', { name: 'Logs' }));
		await vi.dynamicImportSettled();
		await screen.findByRole('heading', { level: 1, name: 'Logs' });

		const box = screen.getByRole('combobox', { name: 'Search settings' });
		await fireEvent.input(box, { target: { value: 'timezone' } });
		expect(box.getAttribute('aria-expanded')).toBe('true');
		const list = screen.getByRole('listbox', { name: 'Search results' });
		const option = within(list).getAllByRole('option')[0];
		expect(option.textContent).toContain('Time zone');
		expect(option.textContent).toContain('General, Region');
		expect(box.getAttribute('aria-activedescendant')).toBe(option.id);
		expect(within(nav()).queryByRole('button', { name: 'Voice' })).toBeNull();

		await fireEvent.keyDown(box, { key: 'Enter' });
		expect((box as HTMLInputElement).value).toBe('');
		expect(within(nav()).getByRole('button', { name: 'Voice' })).toBeTruthy();
		await screen.findByRole('heading', { level: 1, name: 'General' });
		await waitFor(() =>
			expect(container.querySelector('[data-setting-anchor="general.timezone"]')?.classList.contains('st-flash')).toBe(true)
		);
	});

	it('moves through results with arrows and clears with Esc', async () => {
		render(SettingsShell);
		const box = screen.getByRole('combobox', { name: 'Search settings' });
		await fireEvent.input(box, { target: { value: 'the' } });
		const options = within(screen.getByRole('listbox')).getAllByRole('option');
		expect(options.length).toBeGreaterThan(1);
		await fireEvent.keyDown(box, { key: 'ArrowDown' });
		expect(options[1].getAttribute('aria-selected')).toBe('true');
		await fireEvent.keyDown(box, { key: 'Escape' });
		expect((box as HTMLInputElement).value).toBe('');
		expect(box.getAttribute('aria-expanded')).toBe('false');
	});

	it('says when nothing matches', async () => {
		render(SettingsShell);
		await fireEvent.input(screen.getByRole('combobox', { name: 'Search settings' }), { target: { value: 'zzzz' } });
		expect(screen.getByText('No setting matches “zzzz”')).toBeTruthy();
	});

	it('focuses search with "/" outside fields', async () => {
		render(SettingsShell);
		const general = within(nav()).getByRole('button', { name: 'General' });
		general.focus();
		await fireEvent.keyDown(general, { key: '/' });
		await waitFor(() => expect(document.activeElement).toBe(screen.getByRole('combobox', { name: 'Search settings' })));
	});
});

/** The app shell's one Ctrl+K owner, with Spotlight faked. */
function appSearchKey() {
	const spotlight = vi.fn();
	const reg = registerSearchShortcut({ isSpotlightOpen: () => false, setSpotlightOpen: spotlight });
	return { spotlight, reg };
}

describe('SettingsShell Ctrl+K', () => {
	it('takes Ctrl+K while mounted and gives it back to Spotlight on unmount', async () => {
		const { spotlight, reg } = appSearchKey();
		try {
			const view = render(SettingsShell);
			await tick();
			await fireEvent.keyDown(window, { key: 'k', ctrlKey: true });
			expect(spotlight).not.toHaveBeenCalled();
			await waitFor(() => expect(document.activeElement).toBe(screen.getByRole('combobox', { name: 'Search settings' })));

			view.unmount();
			await fireEvent.keyDown(window, { key: 'k', ctrlKey: true });
			expect(spotlight).toHaveBeenCalledExactlyOnceWith(true);
		} finally {
			reg.unregister();
		}
	});

	it('focuses Settings search even when Settings mounted before the app registered Ctrl+K', async () => {
		render(SettingsShell);
		await tick();
		const { spotlight, reg } = appSearchKey();
		try {
			await fireEvent.keyDown(window, { key: 'k', ctrlKey: true });
			expect(spotlight).not.toHaveBeenCalled();
			await waitFor(() => expect(document.activeElement).toBe(screen.getByRole('combobox', { name: 'Search settings' })));
		} finally {
			reg.unregister();
		}
	});

	it('works from a field on the Settings page', async () => {
		const { spotlight, reg } = appSearchKey();
		try {
			render(SettingsShell);
			await tick();
			const field = document.body.appendChild(document.createElement('input'));
			field.focus();
			await fireEvent.keyDown(field, { key: 'k', ctrlKey: true });
			expect(spotlight).not.toHaveBeenCalled();
			await waitFor(() => expect(document.activeElement).toBe(screen.getByRole('combobox', { name: 'Search settings' })));
			field.remove();
		} finally {
			reg.unregister();
		}
	});

	it('leaves Ctrl+K to Spotlight while the kept-alive panel is off screen', async () => {
		const { spotlight, reg } = appSearchKey();
		try {
			render(SettingsShell, { context: new Map([[FEATURE_PANEL_CONTEXT, { active: false }]]) });
			await tick();
			await fireEvent.keyDown(window, { key: 'k', ctrlKey: true });
			expect(spotlight).toHaveBeenCalledOnce();
		} finally {
			reg.unregister();
		}
	});
});

describe('SettingsShell read-only', () => {
	// The banner's link opens a lazily imported page. Import it first, so this
	// test measures the link and not how busy the runner is.
	beforeAll(async () => { await loadCollection('devices'); }, 60_000);

	it('shows the approval banner and disables controls', async () => {
		admin.set(false);
		render(SettingsShell);
		const banner = screen.getByText('This device needs approval').closest('[role="status"]')!;
		expect(banner.textContent).toContain('Approve from an approved device');
		const theme = screen.getByRole('group', { name: 'Theme' });
		for (const name of ['Light', 'Dark', 'System']) {
			expect((within(theme).getByRole('button', { name }) as HTMLButtonElement).disabled).toBe(true);
		}
		expect((screen.getByRole('combobox', { name: 'Time zone' }) as HTMLSelectElement).disabled).toBe(true);
		await fireEvent.click(screen.getByRole('button', { name: 'Open Devices' }));
		await screen.findByRole('heading', { level: 1, name: 'Devices' });
	});
});

describe('SettingsShell read-only AI content report', () => {
	beforeEach(() => {
		window.__SHUFFLE_MOBILE__ = {
			apiOrigin: 'https://worker.example',
			token: 'shc_test',
			native: { platform: 'android', bridge: 1, capabilities: [] }
		};
	});
	afterEach(() => {
		delete window.__SHUFFLE_MOBILE__;
		aiContentPrefill.value = null;
		supportReportDialog.close();
	});

	it('lets a non-owner fill in and continue an AI content report while the rest stays read-only', async () => {
		admin.set(false);
		mocks.supportCaps.mockResolvedValue({ basic_report: false, can_report_ai_content: true, ai_diagnosis: false, signed_in: true });
		aiContentPrefill.value = { excerpt: 'Reported reply', sessionId: 'sess-42' };
		const { container } = render(SettingsShell, {
			initialItemId: encodeTarget({ category: 'system', anchor: 'system.reportProblem' })
		});
		expect(screen.getByText('This device needs approval')).toBeTruthy();
		const excerpt = (await screen.findByLabelText('The AI output')) as HTMLTextAreaElement;
		expect(excerpt.value).toBe('Reported reply');
		expect(excerpt.matches(':disabled')).toBe(false);

		// Everything outside the report keeps read-only mode.
		const workspace = container.querySelector('[data-setting-anchor="system.workspace"]')!;
		const controls = [...workspace.querySelectorAll<HTMLInputElement | HTMLButtonElement>('input, button')];
		expect(controls.length).toBeGreaterThan(0);
		for (const control of controls) expect(control.matches(':disabled')).toBe(true);

		const reason = screen.getByLabelText('Why are you reporting it?') as HTMLTextAreaElement;
		expect(reason.matches(':disabled')).toBe(false);
		await fireEvent.input(reason, { target: { value: 'Harmful' } });
		const go = screen.getByRole('button', { name: 'Continue' }) as HTMLButtonElement;
		expect(go.matches(':disabled')).toBe(false);
		await fireEvent.click(go);
		expect(supportReportDialog.request?.source).toEqual({ kind: 'ai_content', excerpt: 'Reported reply', session_id: 'sess-42' });
		expect(supportReportDialog.request?.note).toBe('Harmful');

		// Closed, the report row goes: a non-owner has no problem reports.
		await fireEvent.click(screen.getByRole('button', { name: 'Cancel' }));
		await waitFor(() => expect(container.querySelector('[data-setting-anchor="system.reportProblem"]')).toBeNull());
	});
});

describe('SettingsShell deep links', () => {
	it('applies initialItemId, clears it, and reveals the anchor', async () => {
		const clear = vi.spyOn(layout, 'clearFeatureItemId');
		const { container } = render(SettingsShell, {
			initialItemId: encodeTarget({ category: 'general', anchor: 'general.timezone' })
		});
		expect(clear).toHaveBeenCalled();
		await waitFor(() =>
			expect(container.querySelector('[data-setting-anchor="general.timezone"]')?.classList.contains('st-flash')).toBe(true)
		);
	});

	it('returns to the list when the open section is chosen again', async () => {
		render(SettingsShell);
		settingsNav.selectCategory('hooks');
		settingsNav.selectItem('some-hook', 'hooks');
		await tick();
		await fireEvent.click(within(nav()).getByRole('button', { name: 'Hooks' }));
		expect(settingsNav.selectedItem('hooks')).toBeNull();
	});

	it('keeps the open item when another section is chosen and left again', async () => {
		render(SettingsShell);
		settingsNav.selectCategory('hooks');
		settingsNav.selectItem('some-hook', 'hooks');
		await tick();
		await fireEvent.click(within(nav()).getByRole('button', { name: 'Voice' }));
		expect(settingsNav.selectedItem('hooks')).toBe('some-hook');
	});

	it('lands legacy tab names', async () => {
		settingsNav.selectCategory('voice');
		render(SettingsShell, { initialItemId: 'logs' });
		await screen.findByRole('heading', { level: 1, name: 'Logs' });
	});

	it('opens an expandable row named by the target', async () => {
		render(SettingsShell, {
			initialItemId: encodeTarget({ category: 'general', anchor: 'general.themeStyle', open: 'general.themeStyle' })
		});
		await screen.findByRole('group', { name: 'Theme styles' });
	});
});

describe('SettingsShell stacked mode', () => {
	it('waits for the outgoing phone detail to retire before landing a retained-shell deep link', async () => {
		platform.set('mobile');
		const view = render(SettingsShell);
		await tick();
		page.state = { viewPanelDetail: 'automations' } as App.PageState;
		await tick();
		const target = encodeTarget({ category: 'providers', item: 'provider:claude', anchor: 'providers.claudeMode' });
		await view.rerender({ initialItemId: target });
		await tick();
		expect(mocks.pushState).not.toHaveBeenCalled();
		expect(view.container.querySelector('.settings-shell.awaiting-target')).toBeTruthy();

		// The outgoing ViewShell's asynchronous history.back() has now completed.
		mocks.pushState.mockImplementation((_url, state) => { page.state = state; });
		page.state = {};
		await tick();
		await waitFor(() => expect(settingsNav.view).toBe('detail'));
		expect(mocks.pushState.mock.calls.map(([, state]) => state.settingsView)).toEqual(['page', 'detail']);
		expect(settingsNav.depth).toBe(2);
		expect(settingsNav.selectedItem('providers')).toBe('provider:claude');
		await view.rerender({ initialItemId: undefined });

		page.state = mocks.pushState.mock.calls[0][1];
		await tick();
		expect(settingsNav.view).toBe('page');
		page.state = {};
		await tick();
		expect(settingsNav.view).toBe('index');
	});

	it('builds both Back steps for a cold phone-width deep link after the shell measures', async () => {
		const originalWidth = window.innerWidth;
		onTestFinished(() => { Object.defineProperty(window, 'innerWidth', { configurable: true, value: originalWidth }); });
		Object.defineProperty(window, 'innerWidth', { configurable: true, value: 390 });
		render(SettingsShell, {
			initialItemId: encodeTarget({ category: 'providers', item: 'provider:claude', anchor: 'providers.claudeMode' })
		});
		await waitFor(() => expect(settingsNav.stacked).toBe(true));
		await waitFor(() => expect(settingsNav.view).toBe('detail'));
		expect(mocks.pushState.mock.calls.map(([, state]) => state.settingsView)).toEqual(['page', 'detail']);
	});

	describe('every later narrowing goes through the same rule', () => {
		const provider = encodeTarget({ category: 'providers', item: 'provider:claude' });
		const settings = () => mocks.pushState.mock.calls.map(([, state]) => state);

		beforeEach(() => {
			mocks.pushState.mockImplementation((_url, state) => { page.state = state; });
			platform.set('mobile');
		});

		it('a deep link, then widen, pick another page and narrow: Back reaches the list', async () => {
			const go = vi.spyOn(history, 'go').mockImplementation(() => {});
			const view = render(SettingsShell, { initialItemId: provider });
			await waitFor(() => expect(settingsNav.view).toBe('detail'));
			await view.rerender({ initialItemId: undefined }); // the app clears a target once it lands
			expect(settingsNav.depth).toBe(2);

			platform.set('desktop');
			await waitFor(() => expect(settingsNav.stacked).toBe(false));
			settingsNav.selectCategory('cloud');
			await tick();
			platform.set('mobile');
			await waitFor(() => expect(settingsNav.stacked).toBe(true));
			await tick();

			expect(settingsNav.category).toBe('cloud');
			expect(settingsNav.view).toBe('page');
			expect(settingsNav.depth).toBe(3);
			expect(settings().at(-1)).toMatchObject({ settingsView: 'page', settingsCategory: 'cloud', settingsDepth: 3, settingsStaleBelow: 3 });

			page.state = settings()[1];
			await tick();
			expect(settingsNav.view).toBe('index');
			expect(settingsNav.category).toBe('cloud');
			expect(go).toHaveBeenCalledWith(-2);
		});

		it('rotating with nothing changed keeps the deep link and adds no entry', async () => {
			const view = render(SettingsShell, { initialItemId: provider });
			await waitFor(() => expect(settingsNav.view).toBe('detail'));
			await view.rerender({ initialItemId: undefined }); // the app clears a target once it lands
			const entries = settings().length;

			for (const width of ['desktop', 'mobile', 'desktop', 'mobile'] as const) {
				platform.set(width);
				await tick();
			}
			await waitFor(() => expect(settingsNav.stacked).toBe(true));

			expect(settings()).toHaveLength(entries);
			expect(settingsNav.view).toBe('detail');
			expect(settingsNav.depth).toBe(2);
		});

		it('a deep link that lands before the width is known still gets its Back steps', async () => {
			platform.set('desktop');
			const view = render(SettingsShell, { initialItemId: provider });
			await waitFor(() => expect(settingsNav.view).toBe('detail'));
			await view.rerender({ initialItemId: undefined });
			expect(mocks.pushState).not.toHaveBeenCalled();

			platform.set('mobile');
			await waitFor(() => expect(settingsNav.stacked).toBe(true));
			await tick();

			expect(settings().map((state) => state.settingsView)).toEqual(['page', 'detail']);
			expect(settingsNav.depth).toBe(2);
			expect(settingsNav.view).toBe('detail');
		});

		it('a chat opened behind a hidden Settings leaves the open page alone', async () => {
			const view = render(SettingsShell, { initialItemId: provider });
			await waitFor(() => expect(settingsNav.view).toBe('detail'));
			await view.rerender({ initialItemId: undefined }); // the app clears a target once it lands
			settingsNav.followHistory({ ...settings()[1], chatSession: 'chat-b' }, false);
			expect(settingsNav.view).toBe('detail');
			expect(settingsNav.depth).toBe(0);
		});
	});

	it('shows one pane at a time and pushes a history entry per step', async () => {
		platform.set('mobile');
		const back = vi.spyOn(history, 'back').mockImplementation(() => {});
		const { container } = render(SettingsShell);
		const shell = container.querySelector('.settings-shell')!;
		await tick();
		expect(shell.hasAttribute('data-stacked')).toBe(true);
		expect(shell.getAttribute('data-view')).toBe('index');

		await fireEvent.click(within(nav()).getByRole('button', { name: 'Voice' }));
		expect(shell.getAttribute('data-view')).toBe('page');
		// Page mode hides inner .st-dpane item details, never the shared outer slot.
		expect(shell.querySelector('.view-main-slot')?.classList.contains('st-dpane')).toBe(false);
		expect(shell.querySelector('.view-main-slot')?.hasAttribute('hidden')).toBe(false);
		expect(mocks.pushState).toHaveBeenCalledWith('', expect.objectContaining({ settingsView: 'page', settingsDepth: 1 }));

		await fireEvent.click(screen.getByRole('button', { name: 'Back to Settings' }));
		expect(back).toHaveBeenCalledOnce();
		page.state = {};
		await tick();
		expect(shell.getAttribute('data-view')).toBe('index');
	});

	it('keeps a deep link on its page when a narrow window turns stacked after mounting', async () => {
		// ViewShell follows the window breakpoint, not the docked main pane's width.
		const originalWidth = window.innerWidth;
		const view = render(SettingsShell, {
			initialItemId: encodeTarget({ category: 'system', anchor: 'system.reportProblem' })
		});
		await tick();
		await view.rerender({ initialItemId: undefined });
		Object.defineProperty(window, 'innerWidth', { configurable: true, value: 390 });
		await fireEvent(window, new Event('resize'));
		await tick();
		const shell = view.container.querySelector('.settings-shell')!;
		expect(shell.hasAttribute('data-stacked')).toBe(true);
		expect(shell.getAttribute('data-view')).toBe('page');
		expect(settingsNav.category).toBe('system');
		Object.defineProperty(window, 'innerWidth', { configurable: true, value: originalWidth });
	});

	it('Ctrl+K returns to the index before focusing search', async () => {
		const { reg } = appSearchKey();
		onTestFinished(() => reg.unregister());
		platform.set('mobile');
		const go = vi.spyOn(history, 'go').mockImplementation(() => {});
		const { container } = render(SettingsShell);
		await tick();
		await fireEvent.click(within(nav()).getByRole('button', { name: 'System' }));
		await fireEvent.keyDown(window, { key: 'k', ctrlKey: true });
		expect(go).toHaveBeenCalledWith(-1);
		page.state = {};
		await tick();
		expect(container.querySelector('.settings-shell')?.getAttribute('data-view')).toBe('index');
		await waitFor(() => expect(document.activeElement).toBe(screen.getByRole('combobox', { name: 'Search settings' })));
	});
});
